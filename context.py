from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Same precedence as Pi: override wins in that directory, then AGENTS.md, then CLAUDE.md.
CONTEXT_NAMES = (
    "AGENTS.override.md",
    "AGENTS.md",
    "AGENTS.MD",
    "CLAUDE.md",
    "CLAUDE.MD",
    "agents.md",
    "claude.md",
)


@dataclass
class Skill:
    name: str
    description: str
    location: str
    in_workspace: bool = True


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4)) if text else 0


def estimate_item_tokens(item: dict[str, Any]) -> int:
    return estimate_tokens(json.dumps(item, ensure_ascii=False))


def estimate_items_tokens(items: list[dict[str, Any]]) -> int:
    return sum(estimate_item_tokens(item) for item in items)


def load_project_files(cwd: str | Path, home: str | Path | None = None) -> list[tuple[Path, str]]:
    collected: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for directory in _context_dirs(Path(cwd).resolve()):
        path = _context_file(directory)
        if path is None or path in seen:
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            collected.append((path, text))
    user = Path(home).expanduser() if home is not None else Path.home()
    user_file = _context_file(user / ".wheel")
    if user_file is not None and user_file not in seen:
        try:
            text = user_file.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text.strip():
            collected.insert(0, (user_file, text))
    return collected


def load_skills(
    cwd: str | Path,
    home: str | Path | None = None,
    *,
    trusted: bool = True,
) -> list[Skill]:
    root = Path(cwd).resolve()
    user = Path(home).expanduser() if home is not None else Path.home()
    dirs: list[tuple[Path, bool]] = []
    if trusted:
        for directory in _context_dirs(root):
            dirs.append((directory / ".wheel" / "skills", True))
            dirs.append((directory / "skills", True))
            dirs.append((directory / ".agents" / "skills", True))
    dirs.append((user / ".wheel" / "skills", False))
    dirs.append((user / ".agents" / "skills", False))
    skills: list[Skill] = []
    seen: set[str] = set()
    for directory, in_workspace in dirs:
        if not directory.is_dir():
            continue
        for skill_file in sorted(directory.glob("*/SKILL.md")):
            skill = parse_skill(skill_file, in_workspace=in_workspace)
            if skill is None:
                continue
            key = skill.name.lower()
            if key in seen:
                continue
            seen.add(key)
            skills.append(skill)
    return skills


def parse_skill(path: Path, *, in_workspace: bool = True) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    description = _frontmatter_description(text)
    if not description:
        return None
    name = _frontmatter_field(text, "name") or path.parent.name
    return Skill(name=name, description=description, location=str(path), in_workspace=in_workspace)


def expand_skill_command(
    text: str,
    cwd: str | Path,
    home: str | Path | None = None,
    *,
    trusted: bool = True,
) -> str:
    raw = text.strip()
    if not raw.startswith("/skill:"):
        return text
    rest = raw[len("/skill:") :]
    name, _, extra = rest.partition(" ")
    name = name.strip()
    extra = extra.strip()
    skill = next((s for s in load_skills(cwd, home=home, trusted=trusted) if s.name == name), None)
    if skill is None:
        return text
    try:
        content = Path(skill.location).read_text(encoding="utf-8")
    except OSError:
        return text
    body = _strip_frontmatter(content).strip()
    base = str(Path(skill.location).parent)
    block = (
        f'<skill name="{skill.name}" location="{skill.location}">\n'
        f"References are relative to {base}.\n\n{body}\n</skill>"
    )
    return f"{block}\n\n{extra}" if extra else block


def format_project_xml(files: list[tuple[Path, str]]) -> str:
    if not files:
        return ""
    parts = ["<project_context>", "", "Project-specific instructions and guidelines:", ""]
    for path, text in files:
        parts.append(f'<project_instructions path="{path}">')
        parts.append(text.rstrip())
        parts.append("</project_instructions>")
        parts.append("")
    parts.append("</project_context>")
    return "\n".join(parts).rstrip()


def format_skills_xml(skills: list[Skill]) -> str:
    if not skills:
        return ""
    parts = [
        "<available_skills>",
        "Use /skill:name to load a skill (harness injects the file). Workspace skills can also be read.",
        "Prefer workspace skills; user-level skills load via /skill:name, not the read tool.",
    ]
    for skill in skills:
        parts.append("  <skill>")
        parts.append(f"    <name>{_xml_escape(skill.name)}</name>")
        parts.append(f"    <description>{_xml_escape(skill.description)}</description>")
        parts.append(f"    <location>{_xml_escape(skill.location)}</location>")
        parts.append("  </skill>")
    parts.append("</available_skills>")
    return "\n".join(parts)


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _context_dirs(cwd: Path) -> list[Path]:
    dirs: list[Path] = []
    cur = cwd
    seen: set[Path] = set()
    while cur not in seen:
        seen.add(cur)
        dirs.append(cur)
        if (cur / ".git").exists():
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    dirs.reverse()
    return dirs


def _context_file(directory: Path) -> Path | None:
    for name in CONTEXT_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _frontmatter_block(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end < 0:
        return ""
    return text[3:end]


def _frontmatter_field(text: str, name: str) -> str:
    key = name.lower() + ":"
    for line in _frontmatter_block(text).splitlines():
        if line.strip().lower().startswith(key):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def _frontmatter_description(text: str) -> str:
    return _frontmatter_field(text, "description")


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    return text[end + 4 :]


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def tag_lines(text: str, tag: str) -> list[str]:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]
