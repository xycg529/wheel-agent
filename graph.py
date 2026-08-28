from __future__ import annotations

import atexit
import html
import json
import threading
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from wheel_agent.audit import redact_tool_args, redact_tool_output
from wheel_agent.compact import is_summary_item
from wheel_agent.model import item_text
from wheel_agent.session import Session, preview_user_text
from wheel_agent.tools import parse_function_calls

PARALLEL_TOOLS = {
    "read",
    "ls",
    "grep",
    "glob",
    "web_search",
    "web_fetch",
    "bash_poll",
}


@dataclass
class GraphNode:
    kind: str
    title: str
    detail: str = ""
    result: str = ""
    status: str = ""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    on_path: bool = True


@dataclass
class GraphLayer:
    nodes: list[GraphNode]


@dataclass
class GraphBlock:
    layers: list[GraphLayer] = field(default_factory=list)
    branches: list[GraphBlock] = field(default_factory=list)

    @property
    def on_path(self) -> bool:
        if any(node.on_path for layer in self.layers for node in layer.nodes):
            return True
        return any(branch.on_path for branch in self.branches)

    def empty(self) -> bool:
        return not self.layers and not self.branches


@dataclass
class SessionGraph:
    session_id: str
    layers: list[GraphLayer]
    runs: list[str] = field(default_factory=list)
    tree: GraphBlock = field(default_factory=GraphBlock)
    leaf_id: str | None = None


def build_session_graph(session: Session, runs_dir: str | Path | None = None) -> SessionGraph:
    entries = session.entries
    kids: dict[str | None, list[str]] = {}
    for eid in session.order:
        node = entries.get(eid)
        if node is None:
            continue
        kids.setdefault(node.parent_id, []).append(eid)
    path = set(session.path_ids())
    outputs: dict[str, str] = {}
    errors: dict[str, bool] = {}
    user_n: dict[str, int] = {}
    n_user = 0
    for eid in session.order:
        item = entries[eid].item
        if item.get("type") == "function_call_output":
            cid = str(item.get("call_id") or "")
            outputs[cid] = str(item.get("output") or "")
            errors[cid] = bool(item.get("is_error"))
        if _is_user(item):
            n_user += 1
            user_n[eid] = n_user

    def passthrough(item: dict[str, Any]) -> bool:
        kind = item.get("type")
        if kind in {"function_call_output", "reasoning", "thinking"}:
            return True
        if kind == "function_call" or _is_user(item):
            return False
        if _is_assistant(item):
            return not item_text(item).strip()
        return True

    def visible_kids(eid: str) -> list[str]:
        found: list[str] = []
        for cid in kids.get(eid, []):
            item = entries[cid].item
            if passthrough(item):
                found.extend(visible_kids(cid))
            else:
                found.append(cid)
        return found

    def gather_tools(start: str) -> tuple[list[str], str]:
        batch = [start]
        cur = start
        while True:
            direct = kids.get(cur, [])
            if len(direct) == 1 and entries[direct[0]].item.get("type") == "function_call":
                cur = direct[0]
                batch.append(cur)
                continue
            return batch, cur

    def build_from(eid: str) -> GraphBlock:
        layers: list[GraphLayer] = []
        cur: str | None = eid
        while cur:
            item = entries[cur].item
            if item.get("type") == "function_call":
                batch, last = gather_tools(cur)
                nodes = [_tool_node(i, entries[i].item, outputs, errors, i in path) for i in batch]
                for group in _tool_groups(nodes):
                    layers.append(GraphLayer(group))
                nxt = visible_kids(last)
            else:
                node = _entry_node(cur, entries[cur].item, user_n, outputs, errors, cur in path)
                if node is not None:
                    layers.append(GraphLayer([node]))
                nxt = visible_kids(cur)
            if not nxt:
                return GraphBlock(layers=layers)
            if len(nxt) == 1:
                cur = nxt[0]
                continue
            return GraphBlock(layers=layers, branches=[build_from(x) for x in nxt])
        return GraphBlock(layers=layers)

    raw_roots = kids.get(None, [])
    visible_roots: list[str] = []
    for rid in raw_roots:
        if passthrough(entries[rid].item):
            visible_roots.extend(visible_kids(rid))
        else:
            visible_roots.append(rid)
    if len(visible_roots) == 1:
        tree = build_from(visible_roots[0])
    elif visible_roots:
        tree = GraphBlock(branches=[build_from(rid) for rid in visible_roots])
    else:
        tree = GraphBlock()
    return SessionGraph(
        session_id=session.session_id,
        layers=_flatten_path(tree),
        runs=list_session_runs(session.session_id, runs_dir),
        tree=tree,
        leaf_id=session.leaf_id,
    )


def _flatten_path(block: GraphBlock) -> list[GraphLayer]:
    out: list[GraphLayer] = []
    for layer in block.layers:
        nodes = [node for node in layer.nodes if node.on_path]
        if nodes:
            out.append(GraphLayer(nodes))
    on = [branch for branch in block.branches if branch.on_path]
    if len(on) == 1:
        out.extend(_flatten_path(on[0]))
    else:
        for branch in on:
            out.extend(_flatten_path(branch))
    return out


def _entry_node(
    eid: str,
    item: dict[str, Any],
    user_n: dict[str, int],
    outputs: dict[str, str],
    errors: dict[str, bool],
    on_path: bool,
) -> GraphNode | None:
    if _is_user(item):
        return _user_node(user_n.get(eid, 0), item, on_path=on_path)
    if _is_assistant(item):
        text = item_text(item).strip()
        if not text:
            return None
        return _assistant_node(eid, text, on_path=on_path)
    return None


def _tool_node(eid: str, item: dict[str, Any], outputs: dict[str, str], errors: dict[str, bool], on_path: bool) -> GraphNode:
    calls = parse_function_calls([item])
    if not calls:
        return GraphNode(kind="tool", title="tool", on_path=on_path)
    call = calls[0]
    args = redact_tool_args(call.name, call.arguments)
    raw = outputs.get(call.call_id, "")
    result = redact_tool_output(call.name, call.arguments, raw)
    return GraphNode(
        kind="tool",
        title=call.name,
        name=call.name,
        detail=_args_preview(args),
        result=result,
        status=_status(result, errors.get(call.call_id, False)),
        args=args,
        on_path=on_path,
    )


def render_ascii(graph: SessionGraph, *, width: int = 56) -> str:
    block = graph.tree if not graph.tree.empty() else GraphBlock(layers=graph.layers)
    mark = _tree_has_off(block)
    n_branch = max(1, _count_leaves(block)) if not block.empty() else 0
    header = f"session {graph.session_id}  layers={len(graph.layers)}  branches={n_branch}"
    if graph.leaf_id:
        header += f"  now {graph.leaf_id}"
    lines = [header]
    if graph.runs:
        lines.append("runs  " + ", ".join(graph.runs))
    lines.append("")
    lines.extend(_render_block(block, width, mark_path=mark))
    return "\n".join(lines).rstrip() + "\n"


def _count_leaves(block: GraphBlock) -> int:
    if not block.branches:
        return 1 if block.layers else 0
    return sum(_count_leaves(branch) for branch in block.branches)


def _tree_has_off(block: GraphBlock) -> bool:
    if any(not node.on_path for layer in block.layers for node in layer.nodes):
        return True
    return any(_tree_has_off(branch) for branch in block.branches)


def _render_block(block: GraphBlock, width: int, *, mark_path: bool) -> list[str]:
    lines: list[str] = []
    for i, layer in enumerate(block.layers):
        if i:
            lines.append(_connector(layer.nodes))
        if len(layer.nodes) == 1:
            lines.extend(_box(layer.nodes[0], width, mark_path=mark_path))
        else:
            lines.extend(_row(layer.nodes, width, mark_path=mark_path))
    if not block.branches:
        return lines
    n = len(block.branches)
    col_w = max(16, (width - 2 * (n - 1)) // n)
    cols = [_render_block(branch, col_w, mark_path=mark_path) for branch in block.branches]
    if lines:
        lines.append(_fork_bar(n, col_w))
    height = max((len(col) for col in cols), default=0)
    padded = [col + [" " * col_w] * (height - len(col)) for col in cols]
    for row in zip(*padded):
        lines.append("  ".join(row))
    return lines


def _fork_bar(n: int, col_w: int) -> str:
    caps = []
    for _ in range(n):
        pad = max(0, col_w // 2)
        caps.append((" " * pad) + "|" + (" " * max(0, col_w - pad - 1)))
    return "  ".join(caps)


def render_html(graph: SessionGraph) -> str:
    block = graph.tree if not graph.tree.empty() else GraphBlock(layers=graph.layers)
    cards = _html_block(block)
    runs = ", ".join(html.escape(r) for r in graph.runs) or "(none)"
    n_branch = max(1, _count_leaves(block)) if not block.empty() else 0
    now = html.escape(graph.leaf_id or "")
    meta = f"runs {runs} · branches {n_branch}"
    if now:
        meta += f" · now {now}"
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wheel session {html.escape(graph.session_id)}</title>
<style>
  :root {{
    --steel: #14120e;
    --ticket: #211b14;
    --rule: #3c3228;
    --brass: #c4a15a;
    --paper: #ead9b6;
    --ink: #d4c6a8;
    --user: #d08a4c;
    --say: #7d9a6e;
    --tool: #c9a227;
    --bad: #c45c48;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--steel);
    color: var(--ink);
    font: 15px/1.45 "Avenir Next", "Segoe UI", sans-serif;
  }}
  header {{
    padding: 28px 28px 12px;
    border-bottom: 1px solid var(--rule);
  }}
  h1 {{
    margin: 0;
    font: 500 13px/1.2 ui-monospace, "IBM Plex Mono", Menlo, monospace;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--brass);
  }}
  .meta {{ margin-top: 8px; color: #9a8b72; font-size: 13px; }}
  main {{ padding: 28px; display: flex; flex-direction: column; align-items: stretch; gap: 0; }}
  .layer {{ display: flex; gap: 14px; justify-content: center; flex-wrap: wrap; }}
  .edge {{
    height: 28px;
    margin: 4px auto;
    width: 1px;
    background: repeating-linear-gradient(to bottom, var(--brass) 0 4px, transparent 4px 8px);
  }}
  .card {{
    width: min(640px, 100%);
    background: var(--ticket);
    border: 1px solid var(--rule);
    border-left: 3px solid var(--brass);
    padding: 12px 14px 14px;
  }}
  .card.user {{ border-left-color: var(--user); }}
  .card.assistant {{ border-left-color: var(--say); }}
  .card.tool {{ border-left-color: var(--tool); }}
  .card.error {{ border-left-color: var(--bad); }}
  .card.off-path {{ opacity: 0.48; }}
  .split {{ display: flex; gap: 16px; align-items: flex-start; justify-content: center; flex-wrap: wrap; }}
  .branch {{ flex: 1 1 260px; min-width: 200px; display: flex; flex-direction: column; align-items: stretch; }}
  .k {{
    font: 600 10px/1 ui-monospace, "IBM Plex Mono", Menlo, monospace;
    letter-spacing: .18em;
    text-transform: uppercase;
    color: var(--brass);
  }}
  .t {{
    font-weight: 650;
    color: var(--paper);
    margin: 4px 0 10px;
  }}
  dl.kv {{ margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 6px 12px; align-items: start; }}
  dt {{
    font: 600 10px/1.6 ui-monospace, "IBM Plex Mono", Menlo, monospace;
    letter-spacing: .14em;
    text-transform: uppercase;
    color: var(--brass);
    padding-top: 2px;
  }}
  dd {{ margin: 0; color: var(--paper); }}
  code, pre code {{
    font: 12.5px/1.45 ui-monospace, "IBM Plex Mono", Menlo, monospace;
    color: var(--paper);
  }}
  pre {{
    margin: 0;
    padding: 8px 10px;
    background: #160f0a;
    border: 1px solid #2c241c;
    white-space: pre-wrap;
    word-break: break-word;
  }}
  .body {{ white-space: pre-wrap; word-break: break-word; color: var(--paper); }}
  details {{ margin-top: 10px; }}
  summary {{ cursor: pointer; color: #9a8b72; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }}
</style>
<header>
  <h1>session {html.escape(graph.session_id)}</h1>
  <div class="meta">{meta}</div>
</header>
<main>
{cards}
</main>
"""


def write_html(graph: SessionGraph, workspace: str | Path) -> Path:
    root = Path(workspace).resolve() / ".wheel" / "graphs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{graph.session_id}.html"
    path.write_text(render_html(graph), encoding="utf-8")
    return path


def _is_user(item: dict[str, Any]) -> bool:
    return item.get("role") == "user" and not is_summary_item(item)


def _is_assistant(item: dict[str, Any]) -> bool:
    if item.get("type") in {"function_call", "function_call_output", "reasoning", "thinking"}:
        return False
    return item.get("role") == "assistant" or item.get("type") == "message"


def _user_node(
    n: int, item: dict[str, Any], *, on_path: bool = True
) -> GraphNode:
    text = item_text(item)
    return GraphNode(
        kind="user",
        title=f"user {n}",
        detail=preview_user_text(text, 120),
        body=text,
        on_path=on_path,
    )


def _assistant_node(eid: str, text: str, *, on_path: bool = True) -> GraphNode:
    return GraphNode(
        kind="assistant",
        title="say",
        detail=preview_user_text(text, 160),
        body=text,
        on_path=on_path,
    )


def _tool_groups(nodes: list[GraphNode]) -> list[list[GraphNode]]:
    groups: list[list[GraphNode]] = []
    current: list[GraphNode] = []
    parallel: bool | None = None
    for node in nodes:
        is_parallel = node.name in PARALLEL_TOOLS
        if parallel is None:
            current = [node]
            parallel = is_parallel
            if not is_parallel:
                groups.append(current)
                current = []
                parallel = None
            continue
        if not is_parallel:
            if current:
                groups.append(current)
            groups.append([node])
            current = []
            parallel = None
            continue
        current.append(node)
    if current:
        groups.append(current)
    return groups


def _status(result: str, is_error: bool = False) -> str:
    low = result.lower()
    if "blocked by safety" in low:
        return "blocked"
    # Trust the structured is_error the loop persists on tool results; sniffing
    # "error" in the output text mislabeled legit results like `read error.log`
    # or `grep error` as errors.
    return "error" if is_error else "ok"


def _args_preview(args: dict[str, Any]) -> str:
    if not args:
        return "{}"
    parts = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = " ".join(str(text).split())
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{key}: {text}")
    return "\n".join(parts)


def _connector(nodes: list[GraphNode]) -> str:
    return "          |"


def _box(node: GraphNode, width: int, *, mark_path: bool = False) -> list[str]:
    label = _label(node, mark_path=mark_path)
    body = [label, *node.detail.splitlines()]
    if node.result:
        preview = preview_user_text(node.result, width - 4)
        body.append("→ " + preview)
    inner = width - 2
    out = ["┌" + "─" * inner + "┐"]
    for line in body:
        line = preview_user_text(line, inner)
        out.append("│" + line.ljust(inner)[:inner] + "│")
    out.append("└" + "─" * inner + "┘")
    return out


def _row(nodes: list[GraphNode], width: int, *, mark_path: bool = False) -> list[str]:
    col = max(18, min(36, (width - 2) // max(1, len(nodes))))
    boxes = [_box(node, col, mark_path=mark_path) for node in nodes]
    height = max(len(box) for box in boxes)
    for box in boxes:
        box.extend([" " * col] * (height - len(box)))
    lines = []
    for row in zip(*boxes):
        lines.append("  ".join(row))
    return lines


def _label(node: GraphNode, *, mark_path: bool = False) -> str:
    star = " *" if mark_path and node.on_path else ""
    if node.kind == "tool":
        flag = f" [{node.status}]" if node.status and node.status != "ok" else ""
        return f"tool {node.title}{flag}{star}"
    return f"{node.title}{star}"


CODE_ARG_KEYS = {"content", "old_string", "new_string", "command", "query"}


def _html_args(args: dict[str, Any]) -> str:
    if not args:
        return ""
    rows: list[str] = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if key in CODE_ARG_KEYS or "\n" in text:
            val = f"<dd><pre><code>{html.escape(text)}</code></pre></dd>"
        else:
            val = f"<dd><code>{html.escape(text)}</code></dd>"
        rows.append(f"<dt>{html.escape(key)}</dt>{val}")
    return '<dl class="kv">' + "".join(rows) + "</dl>"


def _html_card(node: GraphNode) -> str:
    kind = "error" if node.status in {"error", "blocked"} else node.kind
    if node.kind == "tool":
        inner = _html_args(node.args)
    elif node.body.strip():
        inner = f'<div class="body">{html.escape(node.body)}</div>'
    else:
        inner = f'<div class="body">{html.escape(node.detail)}</div>' if node.detail else ""
    result = ""
    if node.result:
        label = "result" + (f" · {node.status}" if node.status else "")
        result = (
            f"<details open><summary>{html.escape(label)}</summary>"
            f"<pre><code>{html.escape(node.result)}</code></pre></details>"
        )
    path_cls = " on-path" if node.on_path else " off-path"
    return (
        f'<article class="card {kind}{path_cls}">'
        f'<div class="k">{html.escape(node.kind)}</div>'
        f'<div class="t">{html.escape(node.title)}</div>'
        f"{inner}{result}"
        "</article>"
    )


def _html_block(block: GraphBlock) -> str:
    parts: list[str] = []
    for i, layer in enumerate(block.layers):
        if i:
            parts.append('<div class="edge" aria-hidden="true"></div>')
        cells = "".join(_html_card(node) for node in layer.nodes)
        parts.append(f'<div class="layer">{cells}</div>')
    if not block.branches:
        return "\n".join(parts)
    if parts:
        parts.append('<div class="edge" aria-hidden="true"></div>')
    cols = []
    for branch in block.branches:
        cls = "branch on-path" if branch.on_path else "branch off-path"
        cols.append(f'<div class="{cls}">{_html_block(branch)}</div>')
    parts.append(f'<div class="split">{chr(10).join(cols)}</div>')
    return "\n".join(parts)


def list_session_runs(session_id: str, runs_dir: str | Path | None) -> list[str]:
    if not runs_dir:
        return []
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    found: list[tuple[int, str]] = []
    for child in root.iterdir():
        meta = child / "meta.json"
        if not child.is_dir() or not meta.is_file():
            continue
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(payload.get("session_id") or "") != session_id:
            continue
        try:
            stamp = child.stat().st_mtime_ns
        except OSError:
            stamp = 0
        found.append((stamp, child.name))
    found.sort()
    return [name for _stamp, name in found]


_http: ThreadingHTTPServer | None = None
_http_root: Path | None = None


def serve_graphs(directory: str | Path) -> str:
    global _http, _http_root
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if _http is not None:
        if _http_root == root:
            port = int(_http.server_address[1])
            return f"http://127.0.0.1:{port}/"
        # The handler's __init__ closure captured the *first* directory; a
        # different root would 404 on the old server, so restart it.
        stop_graph_server()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    _http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    _http_root = root
    threading.Thread(target=_http.serve_forever, name="wheel-graph-http", daemon=True).start()
    atexit.register(stop_graph_server)
    port = int(_http.server_address[1])
    return f"http://127.0.0.1:{port}/"


def stop_graph_server() -> None:
    global _http, _http_root
    server = _http
    _http = None
    _http_root = None
    if server is None:
        return
    server.shutdown()
    server.server_close()
