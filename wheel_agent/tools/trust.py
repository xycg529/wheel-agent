from __future__ import annotations

import json
from pathlib import Path



def trust_file(home: str | Path | None = None) -> Path:
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / ".wheel" / "trust.json"


def project_skill_dirs(workspace: str | Path) -> list[Path]:
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
    path = trust_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping = load_map(home)
    mapping[str(Path(workspace).resolve())] = "allow" if allow else "deny"
    path.write_text(json.dumps({"directories": mapping}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_trusted(workspace: str | Path, home: str | Path | None = None) -> bool:
    return decision_for(workspace, home) == "allow"


def ensure_project_trust(
    workspace: str | Path,
    *,
    interactive: bool,
    ask=None,
    home: str | Path | None = None,
) -> bool:
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
