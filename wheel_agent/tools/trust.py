"""工作区信任机制：项目级 skill（SKILL.md）可能来自不受控的代码仓库，
信任后才注入系统提示。信任决策存 ~/.wheel/trust.json，按目录树就近匹配。"""

from __future__ import annotations

import json
from pathlib import Path



def trust_file(home: str | Path | None = None) -> Path:
    """信任表路径：~/.wheel/trust.json。"""
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / ".wheel" / "trust.json"


def project_skill_dirs(workspace: str | Path) -> list[Path]:
    """从 workspace 向上（到仓库根）找所有含 SKILL.md 的项目 skill 目录。"""
    root = Path(workspace).resolve()
    found: list[Path] = []
    cur = root
    seen: set[Path] = set()
    while cur not in seen:
        seen.add(cur)
        for rel in (".wheel/skills", "skills", ".agents/skills"):
            path = cur / rel
            if path.is_dir() and any(path.glob("*/SKILL.md")):
                found.append(path)
        if (cur / ".git").exists():
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return found


def load_map(home: str | Path | None = None) -> dict[str, str]:
    """读信任表：{目录绝对路径: "allow"|"deny"}；文件坏了当空表处理。"""
    path = trust_file(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    dirs = data.get("directories") if isinstance(data, dict) else None
    if not isinstance(dirs, dict):
        return {}
    return {str(k): str(v) for k, v in dirs.items()}


def decision_for(workspace: str | Path, home: str | Path | None = None) -> str | None:
    """工作区的信任决策：从 workspace 向上找最近的显式 allow/deny 记录。"""
    root = Path(workspace).resolve()
    mapping = load_map(home)
    cur = root
    seen: set[Path] = set()
    while cur not in seen:
        seen.add(cur)
        hit = mapping.get(str(cur))
        if hit in {"allow", "deny"}:
            return hit
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def remember(workspace: str | Path, allow: bool, home: str | Path | None = None) -> None:
    """把本次 y/N 的结果记进信任表，下次同一工作区不再问。"""
    path = trust_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping = load_map(home)
    mapping[str(Path(workspace).resolve())] = "allow" if allow else "deny"
    path.write_text(json.dumps({"directories": mapping}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_trusted(workspace: str | Path, home: str | Path | None = None) -> bool:
    """工作区是否被信任（显式 allow）。"""
    return decision_for(workspace, home) == "allow"


def ensure_project_trust(
    workspace: str | Path,
    *,
    interactive: bool,
    ask=None,
    home: str | Path | None = None,
) -> bool:
    """启动时的信任门：有项目 skill 且无记录时交互询问；
    无 skill 的工作区直接放行（没有可注入的东西）。"""
    if not project_skill_dirs(workspace):
        return True
    existing = decision_for(workspace, home)
    if existing == "allow":
        return True
    if existing == "deny":
        return False
    if not interactive or ask is None:
        return False
    allowed = bool(ask(f"Trust project skills in {Path(workspace).resolve()}? (loads local SKILL.md)"))
    remember(workspace, allowed, home=home)
    return allowed
