# `tools/trust.py` 逐段讲解

> 本篇讲工作区信任机制。上游是 [ui/app.md](../ui/app.md) 的启动流程和 [loop-explained.md](../../loop-explained.md) 的系统提示组装；下游是 [core/context.md](../core/context.md) 的 skill 扫描。

一句话职责：决定"这个工作区里的项目级 `SKILL.md` 能不能进系统提示"，并把用户的 y/N 决策记在工作区之外。

- 行数：105 行
- 依赖：只依赖标准库 `json` / `pathlib`；无锁、无 fsync、无第三方包
- 被谁用：
  - [core/loop.py](../../../core/loop.py)（`is_trusted` + `project_skill_dirs`，93-95 行）
  - [ui/app/__init__.py](../../../ui/app/__init__.py)（`ensure_project_trust`，305 行；`trusted` 再传给 skill 补全与 `/skill:` 展开）
  - [core/prompt.py](../../../core/prompt.py)（`system_prompt(trusted=...)` → [core/context.md](../core/context.md) 的 `load_skills`）

## 目录

- [1. 模块 docstring 与导入（1-7 行）](#1-模块-docstring-与导入1-7-行)
- [2. trust_file（11-14 行）](#2-trust_file11-14-行)
- [3. project_skill_dirs（17-35 行）](#3-project_skill_dirs17-35-行)
- [4. load_map（38-50 行）](#4-load_map38-50-行)
- [5. decision_for（53-68 行）](#5-decision_for53-68-行)
- [6. remember（71-77 行）](#6-remember71-77-行)
- [7. is_trusted（80-82 行）](#7-is_trusted80-82-行)
- [8. ensure_project_trust（85-105 行）](#8-ensure_project_trust85-105-行)

## 1. 模块 docstring 与导入（1-7 行）

docstring 两句就是整个模块的威胁模型与设计选择：

- 威胁：项目级 skill（`SKILL.md`）**可能来自不受控的代码仓库**（`git clone` 下来的第三方项目），它的文本会被拼进系统提示——等于让仓库作者给 agent 下指令。
- 对策：信任后才注入；决策存 `~/.wheel/trust.json`；按目录树**就近匹配**。

"存 home 而不是工作区"是这里最关键的一步：信任标记如果放在工作区里（比如 `.wheel/trust.json`），它会随仓库分发、甚至被提交进去，等于攻击者自己给自己发通行证。放在 home 下，标记始终归本机用户所有。

## 2. `trust_file`（11-14 行）

```python
root = Path(home).expanduser() if home is not None else Path.home()
return root / ".wheel" / "trust.json"
```

一行路径拼装。`home` 参数可注入，测试时能指向临时目录；缺省用 `Path.home()`。

## 3. `project_skill_dirs`（17-35 行）

从 `resolve()` 后的 workspace **向上**走，逐层检查三个相对目录名：

```python
for rel in (".wheel/skills", "skills", ".agents/skills"):
    path = cur / rel
    if path.is_dir() and any(path.glob("*/SKILL.md")):
        found.append(path)
```

三个要点：

- **存在性的标准是"目录里有 `*/SKILL.md`"**，不是"目录存在"。空 `skills/` 目录不算，避免对没有实际 skill 的工作区弹窗打扰。
- **到 `.git` 停**（含该层），和 [core/context.md](../core/context.md) 里 `_context_dirs` 的仓库根边界一致——skill 归属于仓库。
- **返回顺序是 cwd → 上层**（不像 `_context_dirs` 那样 `reverse()` 成根 → cwd）。调用方只取真假值，顺序无影响。

遍历的兜底：`seen` 集合防符号链接成环，`parent == cur` 防走到文件系统根无限循环。

这三个目录名必须和 `core/context.py:93-96` 里 `load_skills` 扫描的位置**保持一致**——信任门要覆盖的正是"实际会被注入的那些目录"。

注意它**只查 skill 目录**：项目指令文件（`AGENTS.md` / `CLAUDE.md`）不经过这道门。

## 4. `load_map`（38-50 行）

读信任表，返回 `{目录绝对路径: "allow"|"deny"}`。结构就一层：

```json
{"directories": {"/Users/u/src/proj": "allow", "/tmp/sketchy": "deny"}}
```

容错是这一段的全部内容：文件不存在、读失败、`json.JSONDecodeError`、顶层不是 dict、`directories` 不是 dict——**一律返回空表**，即"当作没记录"。取舍很清楚：宁可让用户重新答一次，也不能让一个坏文件把启动路径炸掉（信任门在 REPL 起来的最前面）。

没有版本号、没有 migrate 逻辑。

## 5. `decision_for`（53-68 行）

从 workspace 向上逐级查表，**命中第一个 `"allow"` 或 `"deny"` 就返回**——就近匹配，子目录的记录覆盖父目录的。

```python
hit = mapping.get(str(cur))
if hit in {"allow", "deny"}:
    return hit
```

只认这两个字面值。手改 `trust.json` 写成 `"yes"` / `"true"` 会被当没记录（回退到再问一次），不会意外放行。

一路都没有就返回 `None`——**未知不等于 deny**，但两者在 `is_trusted` 眼里都是不可信。

两个和 `project_skill_dirs` 不一样的地方：

- 这里**不停在 `.git`**，会一直走到文件系统根。所以信任可以按更大范围授予（在 `~/src` 记一次 allow，下面所有项目都不再问）。
- 键用 `str(Path)`（绝对路径字符串），查表前 `resolve()`，两侧一致才能命中。

## 6. `remember`（71-77 行）

```python
mapping[str(Path(workspace).resolve())] = "allow" if allow else "deny"
path.write_text(json.dumps({"directories": mapping}, ensure_ascii=False, indent=2) + "\n", ...)
```

记的是**启动时那个 cwd 的绝对路径**，不是仓库根。`indent=2` + 末尾换行是为了让人能手改——目前没有管理信任的斜杠命令，改这个文件是唯一的撤销途径。

**拒绝也会被记住**：答 N 写的是 `"deny"`，下次启动不再打扰。

写盘策略是整体覆盖（`write_text`），没有临时文件 + `rename`、没有 `fsync`、没有文件锁——和 [core/session.py](../../../core/session.py) 的 flush + fsync 路线相反。这是有意的取舍：信任表不是运行数据，丢一条最坏结果是重新问一次。

## 7. `is_trusted`（80-82 行）

```python
return decision_for(workspace, home) == "allow"
```

只有**显式 allow** 才算可信；`None`（未知）和 `"deny"` 都落到 `False`——默认拒绝。

它**不含**"没有项目 skill 就无所谓"这个短路，那段组合逻辑在消费方 `core/loop.py:94`：

```python
trusted = is_trusted(ws.root) or not project_skill_dirs(ws.root)
```

语义是：工作区里根本没有项目级 skill 时，可信与否不重要——**没有东西可注入**。副作用是，带 skill 的工作区不会因为 `is_trusted` 而被反复问（问不问由 `ensure_project_trust` 单独决定）。

## 8. `ensure_project_trust`（85-105 行）

模块里唯一有副作用的入口，启动时调用一次，四个分支按序：

| 条件 | 返回 | 说明 |
|---|---|---|
| `not project_skill_dirs(...)` | `True` | 无 skill 可注入，直接放行，连记录都不查 |
| 已有 `allow` | `True` | 之前批准过 |
| 已有 `deny` | `False` | 之前拒绝过，不再问 |
| 无记录 且（`not interactive` 或 `ask is None`） | `False` | 非交互（`--json`）：默认拒绝 |
| 无记录 且交互 | `ask(...)` 的结果 | 问完 `remember()` 落盘 |

默认拒绝贯穿始终：拿不到用户答案就当不可信。

问句里点明授权内容，不让人盲签：

```python
ask(f"Trust project skills in {Path(workspace).resolve()}? (loads local SKILL.md)")
```

调用方 `ui/app/__init__.py:305` 给了双重保险——`interactive=not json_mode` 且 `ask=ask_yes_no if not json_mode else None`，所以 `--json` 一次性的任务永远不会弹窗，也永远不会加载项目 skill。

`trusted` 结果的流向：

- `core/loop.py:95` → `system_prompt(trusted=...)` → `core/prompt.py:62` → `load_skills(trusted=...)`，决定工作区级 skill 是否出现在系统提示的 `<available_skills>` 块里；
- UI 侧还用它做 `/skill:` 的补全词表与展开（`ui/app/__init__.py:149/494/552/600/726`），所以不信任时 `/skill:name` 也找不到项目 skill。

用户级 skill（`~/.wheel/skills`、`~/.agents/skills`）**不经过这道门**，`load_skills` 始终加载它们——它们在你自己的 home 下，不是 clone 来的。
