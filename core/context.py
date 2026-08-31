"""上下文装配：项目指令文件（AGENTS.md/CLAUDE.md）、skills 扫描与
/skill: 展开、token 估算、XML 片段渲染。全部只读工作区。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# 上下文文件优先级：同目录里第一个存在者生效——
# AGENTS.override.md（按目录覆盖）> AGENTS.md > CLAUDE.md；大小写变体照顾不区分大小写的文件系统。
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
    """一个 SKILL.md 的元数据；in_workspace 标记能否被 read 工具直接读。"""

    name: str
    description: str
    location: str
    in_workspace: bool = True


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：1 token ≈ 4 字符。紧凑判定与截断都用它，只求量级。"""
    return max(1, math.ceil(len(text) / 4)) if text else 0


def estimate_item_tokens(item: dict[str, Any]) -> int:
    return estimate_tokens(json.dumps(item, ensure_ascii=False))


def estimate_items_tokens(items: list[dict[str, Any]]) -> int:
    """一列消息的估算 token 总量（compact 触发判定用）。"""
    return sum(estimate_item_tokens(item) for item in items)


def load_project_files(cwd: str | Path, home: str | Path | None = None) -> list[tuple[Path, str]]:
    """收集项目指令文件：从仓库根到 cwd 逐目录取第一个存在的上下文文件，
    用户级 ~/.wheel/AGENTS.md 插在队首（全局优先）。"""
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
    # 用户级文件插在队首：全局指令优先于项目目录级指令。
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
    """扫描各层 skills 目录（工作区各级 → 用户级），同名 skill 先发现的胜出。"""
    root = Path(cwd).resolve()
    user = Path(home).expanduser() if home is not None else Path.home()
    dirs: list[tuple[Path, bool]] = []
    # 工作区级 skills 需要 trusted：不可信工作区的 skill 不注入系统提示，
    # 防提示注入；用户级始终加载。
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
    """解析 SKILL.md：frontmatter 里必须有 description 才算有效 skill。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    description = _frontmatter_field(text, "description")
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
    """把 /skill:name 开头的输入展开成完整任务文本：读入 SKILL.md 正文，
    包进 <skill> 标签并标注参考路径，后面跟着用户额外输入。"""
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
    # 正文可能引用同目录文件，标注基准路径让模型能拼出绝对路径。
    block = (
        f'<skill name="{skill.name}" location="{skill.location}">\n'
        f"References are relative to {base}.\n\n{body}\n</skill>"
    )
    return f"{block}\n\n{extra}" if extra else block


def format_project_xml(files: list[tuple[Path, str]]) -> str:
    """把项目指令文件渲染成 <project_context> XML，插入系统提示。"""
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
    """把 skill 列表渲染成 <available_skills> XML（只放元数据，正文靠 /skill: 按需加载）。"""
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
    """本地日期 ISO 串（ephemeral 上下文里的 Current date）。"""
    return datetime.now().astimezone().date().isoformat()


def _context_dirs(cwd: Path) -> list[Path]:
    """从 cwd 向上走到仓库根（.git 处停）的目录链，返回根到 cwd 的顺序。"""
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
    """目录里按优先级找上下文文件；都不存在返回 None。"""
    for name in CONTEXT_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _frontmatter_field(text: str, name: str) -> str:
    """读 frontmatter 里一个键的值（无引号）；没有 frontmatter 返回空。"""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end < 0:
        return ""
    key = name.lower() + ":"
    for line in text[3:end].splitlines():
        if line.strip().lower().startswith(key):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def _strip_frontmatter(text: str) -> str:
    """剥掉开头的 --- frontmatter 块，只留正文。"""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    return text[end + 4 :]


def _xml_escape(text: str) -> str:
    """XML 转义：skill 名/描述来自用户文件，插入标签前必须转义。"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def tag_lines(text: str, tag: str) -> list[str]:
    """从文本里抓 <tag>...</tag> 内的行（harness 解析 <memories>/<rules> 用）。"""
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]
