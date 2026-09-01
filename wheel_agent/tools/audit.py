"""审计与指纹：对输入/输出/工作区做规范化哈希，供 replay 对比与事件记录。

关键是把 workspace 绝对路径替换成 <workspace> 占位符，使指纹可移植。
敏感路径的内容会被脱敏，不进入日志/事件。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from wheel_agent.tools.safety import is_sensitive_path

# 工作区清单里跳过的目录（工具产物 / VCS / 缓存）。
SKIP_PARTS = {".wheel", ".wheel_runs", ".git", "__pycache__"}


def canonical(value: Any) -> str:
    """规范化 JSON（键排序、紧凑分隔符）：同样的数据必得同样的串，才能算稳定哈希。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_value(value: Any) -> str:
    """任意可 JSON 化值的 SHA-256 十六进制。"""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _normalize_for_audit(value: Any, workspace: str | Path | None) -> Any:
    """递归把值里的 workspace 绝对路径换成 <workspace>，使指纹与工作区位置无关。"""
    if isinstance(value, dict):
        return {key: _normalize_for_audit(item, workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_for_audit(item, workspace) for item in value]
    if isinstance(value, str) and workspace:
        # 同时替换解析后和未解析的 workspace 串：macOS 上 /tmp 是指向
        # /private/tmp 的符号链接，不替换原始串会让未解析路径逃过哈希。
        root = str(Path(workspace).resolve())
        out = value.replace(root, "<workspace>")
        raw = str(workspace)
        if raw != root:
            out = out.replace(raw, "<workspace>")
        return out
    return value


def item_audit(items: list[dict[str, Any]], workspace: str | Path | None = None) -> dict[str, Any]:
    """一列对话 item 的审计摘要：规范化哈希 + 条数 + 每条形别。"""
    normalized = _normalize_for_audit(items, workspace)
    return {
        "sha256": sha256_value(normalized),
        "count": len(items),
        "types": [str(item.get("type") or item.get("role") or "") for item in items],
    }


def environment_snapshot() -> dict[str, str]:
    """环境快照（python 版本/平台/机器/shell），用于 replay 时对比运行环境。"""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable": os.path.basename(sys.executable),
        "shell": os.path.basename(os.environ.get("SHELL") or ""),
    }


def environment_fingerprint() -> str:
    """环境快照的哈希（比完整快照更紧凑，放事件里）。"""
    return sha256_value(environment_snapshot())


def workspace_manifest(root: str | Path) -> dict[str, str]:
    """工作区清单：每个文件一条 {相对路径: file:大小:sha256}，敏感路径与符号链接特殊处理。"""
    root = Path(root).resolve()
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        rel_text = rel.as_posix()
        if is_sensitive_path(rel_text):
            continue
        try:
            if path.is_symlink():
                result[rel_text] = "symlink:" + os.readlink(path)
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[rel_text] = f"file:{path.stat().st_size}:{digest}"
        except (OSError, UnicodeError):
            result[rel_text] = "unreadable"
    return result


def workspace_fingerprint(manifest: dict[str, str]) -> str:
    """工作区清单的哈希：一次调用前后对比即知工作区是否被改。"""
    return sha256_value(manifest)


def workspace_changes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """两份清单的差异：added/deleted/modified 三个排序列表。"""
    before_keys = set(before)
    after_keys = set(after)
    return {
        "added": sorted(after_keys - before_keys),
        "deleted": sorted(before_keys - after_keys),
        "modified": sorted(key for key in before_keys & after_keys if before[key] != after[key]),
    }


def redact_tool_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """脱敏工具参数：对敏感路径的 write/edit，把内容字段换成 <redacted>。"""
    out = dict(args)
    path = str(out.get("path") or "")
    if name in {"write", "edit"} and is_sensitive_path(path):
        for key in ("content", "old_string", "new_string"):
            if key in out:
                out[key] = "<redacted>"
    return out


def redact_tool_output(name: str, args: dict[str, Any], output: str) -> str:
    """脱敏工具输出：对敏感路径的 read/web_fetch，整个输出不外泄。"""
    if name in {"read", "web_fetch"} and is_sensitive_path(str(args.get("path") or args.get("url") or "")):
        return "<redacted sensitive tool output>"
    return output


def tool_audit(call: Any, result: Any) -> dict[str, Any]:
    """一次工具调用的审计记录：ID/名称/参数哈希/安全裁决/是否出错。"""
    return {
        "tool_call_id": str(call.call_id),
        "tool_name": str(call.name),
        "args_sha256": sha256_value(call.arguments),
        "decision": getattr(result, "safety_decision", "") or "",
        "decision_reason": getattr(result, "safety_reason", ""),
        "decision_source": getattr(result, "safety_source", ""),
        "is_error": bool(result.is_error),
        "blocked": bool(result.blocked),
    }
