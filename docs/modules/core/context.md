# `core/context.py` 逐段讲解

> 本篇讲 `core/context.py`：系统提示的素材来源（项目指令文件、skills）与 token 估算。上游是 [core/prompt.md](prompt.md)（拼系统提示）、[core/compact.md](compact.md)（决定压多少）、[ui/app.md](../ui/app.md)（`/skill:` 展开与 Tab 补全），下游只有标准库——本文件不依赖项目内任何模块，是 `core/` 里的叶子。

一句话职责：把工作区里的指令文件和 skills 扫出来渲染成 XML，并用"字符数 ÷ 4"给上下文称一下重量。全部只读工作区。

- 行数：266 行
- 依赖：仅标准库（`json` / `math` / `re` / `dataclasses` / `datetime` / `pathlib`）
- 被谁用：
  - [core/prompt.md](prompt.md) —— `format_project_xml`、`format_skills_xml`、`load_project_files`、`load_skills`、`today`
  - [core/compact.md](compact.md) —— `estimate_item_tokens`、`estimate_items_tokens`、`tag_lines`
  - [ui/app.md](../ui/app.md) —— `expand_skill_command`、`load_skills`、`load_project_files`

## 目录

- [1. 模块定位与导入](#1-模块定位与导入1–12-行)（1–12 行）
- [2. `CONTEXT_NAMES`：上下文文件优先级](#2-context_names上下文文件优先级14–24-行)（14–24 行）
- [3. `Skill` 元数据](#3-skill-元数据27–35-行)（27–35 行）
- [4. token 估算三件套](#4-token-估算三件套37–48-行)（37–48 行）
- [5. `load_project_files`](#5-load_project_files51–77-行)（51–77 行）
- [6. `load_skills`](#6-load_skills80–113-行)（80–113 行）
- [7. `parse_skill`](#7-parse_skill116–126-行)（116–126 行）
- [8. `expand_skill_command`](#8-expand_skill_command129–159-行)（129–159 行）
- [9. 两个 `format_*_xml`](#9-两个-format__xml162–192-行)（162–192 行）
- [10. `today`](#10-today195–197-行)（195–197 行）
- [11. `_context_dirs`](#11-_context_dirs200–215-行)（200–215 行）
- [12. `_context_file`](#12-_context_file218–224-行)（218–224 行）
- [13. frontmatter 解析](#13-frontmatter-解析227–247-行)（227–247 行）
- [14. `_xml_escape`](#14-_xml_escape251–258-行)（251–258 行）
- [15. `tag_lines`](#15-tag_lines261–265-行)（261–265 行）
- [16. 估算值的消费方：与 compact / truncate 的分工](#16-估算值的消费方与-compact--truncate-的分工)

---

## 1. 模块定位与导入（1–12 行）

docstring 点出四件事：项目指令文件（`AGENTS.md` / `CLAUDE.md`）、skills 扫描与 `/skill:` 展开、token 估算、XML 片段渲染。共同约束是**全部只读工作区**——这个模块从不写盘，可以安全地在每次拼系统提示时调用。

导入里只有标准库，这是刻意的：拼提示的素材层不依赖模型、配置、会话，任何地方都能直接 import 它而不会引入环。

## 2. `CONTEXT_NAMES`：上下文文件优先级（14–24 行）

```python
CONTEXT_NAMES = (
    "AGENTS.override.md", "AGENTS.md", "AGENTS.MD",
    "CLAUDE.md", "CLAUDE.MD", "agents.md", "claude.md",
)
```

每个目录里**取第一个存在者**，所以优先级是 `AGENTS.override.md` > `AGENTS.md` > `CLAUDE.md`。

- `override` 的语义是"在本目录里覆盖同目录的其他名字"——`AGENTS.md` 是提交进仓库的团队约定，`AGENTS.override.md` 通常是 gitignore 掉的个人改动，不想改 `AGENTS.md` 又想改指令时用。注意它**不是**覆盖父目录：父目录的文件照样会被收集（见第 5 节）。
- 大小写变体（`AGENTS.MD` / `agents.md`）照顾不区分大小写的文件系统（macOS / Windows）：在那类文件系统上 `AGENTS.md` 与 `agents.md` 是同一个文件，把变体列全才能保证在区分大小写的 Linux 上也找得到。
- 同时支持 `CLAUDE.md` 是为了兼容别的 agent 已有的项目文件，降低迁移成本。

## 3. `Skill` 元数据（27–35 行）

```python
@dataclass
class Skill:
    name: str
    description: str
    location: str
    in_workspace: bool = True
```

`in_workspace` 标记这个 skill 的文件是否落在工作区内。区别很实际：工作区内的 skill 路径模型可以用 `read` 工具直接读（[tools/tools.md](../tools/tools.md) 的 read 有工作区边界检查）；用户级 skill 在 `~/.wheel/skills` 下，`read` 读不到，只能靠 `/skill:` 展开注入。`format_skills_xml` 里那句 "user-level skills load via /skill:name, not the read tool" 就是给模型说这件事。

## 4. token 估算三件套（37–48 行）

```python
def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4)) if text else 0

def estimate_item_tokens(item):
    return estimate_tokens(json.dumps(item, ensure_ascii=False))

def estimate_items_tokens(items):
    return sum(estimate_item_tokens(item) for item in items)
```

**换算比例怎么定的**：`1 token ≈ 4 字符`。这是 OpenAI 给的经验法则（英文散文约 4 字符 / token，等价于 0.75 词 / token），精度只到量级。选它的理由是**零成本**：不需要 `tiktoken`，不需要按 provider 下载词表，也就不会引入第三个运行时依赖（README 反复强调依赖只有 `openai` 和 `python-dotenv`）。docstring 明说"只求量级"。

**为什么是字符数而不是词数**：中文没有空格分词，用词数估会彻底失效；字符数至少对任何语言都有定义。代价见第 17 节。

**三种 item 一视同仁**：这里没有按类型分支——文本、工具调用、工具输出、图片全都走同一条路径：把整条 item `json.dumps` 成字符串再除以 4。差别只来自序列化后的字符数：

- **文本消息**：`{"role":"user","content":"…"}` 的外壳约 20–30 字符，对长正文可忽略，对一句 "ok" 这种短消息外壳占了绝大多数。
- **工具调用**：`arguments` 是 dict，序列化后包含完整参数（比如 `write` 的整份文件内容），天然计满。
- **工具输出**：`function_call_output` 的 `output` 字段是截断后的文本——[core/truncate.md](truncate.md) 已经先按 2000 行 / 50KB 削过一遍，所以这里估的是削完的量。
- **图片**：**没有图片分支**。README 明确写了"没有图像输入"，这是刻意的取舍。

**两个方向性设计**：

1. **序列化整条 item 而非只算 `content`** —— 把 `role` / `type` 字段名、`call_id`、JSON 引号与转义的开销都算进去。真实 API 的计费确实包含这些结构，所以这不算白估，且方向是**高估**。
2. **`max(1, …)` 非空即至少 1 token** —— 空文本返回 0（真的不占位置），非空至少 1。这条看似琐碎，实为正确性的前提：`compact.find_user_cut_index` 从尾部逐条累加 `estimate_item_tokens` 直到够 `KEEP_RECENT_TOKENS` 为止。若短消息被估成 0，累加值可能永远达不到阈值，函数返回 `None`，紧凑就永远不触发，历史一路涨到 API 报超窗。`max(1, …)` 保证每步至少前进 1。

**为什么宁可高估**：估算偏高的代价只是"多压一点、保留的原文少一点"（有损但能跑）；估算偏低的代价是"以为还很宽松"→ 攒到真的超窗 → API 报错 → [`loop.py`](../../loop-explained.md) 的 `_complete_with_overflow` 强制紧凑并重试，白白多一次失败的请求往返。前者是温和降级，后者是用户可见的卡顿，所以整个估算刻意往高里偏。更关键的是：**自动紧凑的触发判据根本不用估算**（见第 16 节）。

## 5. `load_project_files`（51–77 行）

从仓库根到 cwd 逐目录收集上下文文件，返回一个 `(Path, text)` 列表。

```python
for directory in _context_dirs(Path(cwd).resolve()):
    path = _context_file(directory)
    ...
    if text.strip():
        collected.append((path, text))
```

- 顺序是**根 → cwd**：越靠近 cwd 的目录排在列表后面，也就是在系统提示的 `<project_context>` 里越靠后。
- `seen` 按 `Path` 去重：符号链接指向同一文件、或 `_context_dirs` 因 `/` 与 `//` 之类的解析差异给出重复目录时，不会把同一份指令塞两遍。
- `except OSError: continue` —— 权限不足、编码异常（这里只捕 `OSError`，`UnicodeDecodeError` 是 `ValueError` 的子类，会向上抛）、目录被删都会静默跳过。**收集素材不该让 agent 起不来。**
- `text.strip()` 为空则丢弃：避免空文件的标签块污染提示。

末尾把用户级 `~/.wheel/AGENTS.md` **`insert(0, …)` 插在队首**——全局指令优先于任何项目指令。这是"个人习惯压过团队约定"的取向：项目文件是从仓库里来的（别人写的）， home 目录里的才是自己的。`home` 参数可注入，`None` 时取 `Path.home()`。

## 6. `load_skills`（80–113 行）

扫描各层 skills 目录，返回 `Skill` 列表。目录候选按优先级排列：

```python
if trusted:
    for directory in _context_dirs(root):       # 根 → cwd
        dirs.append((directory / ".wheel" / "skills", True))
        dirs.append((directory / "skills", True))
        dirs.append((directory / ".agents" / "skills", True))
dirs.append((user / ".wheel" / "skills", False))
dirs.append((user / ".agents" / "skills", False))
```

- **工作区级三个位置**：`.wheel/skills`（本项目的隐藏目录）、`skills/`（仓库里可见的目录）、`.agents/skills`（兼容别的 agent 的布局）。每层都试三个，层序是根 → cwd，所以**离 cwd 越近的 skill 越先被发现**。
- **用户级两个位置**：`~/.wheel/skills`、`~/.agents/skills`，永远在队尾，`in_workspace=False`。
- **`trusted` 闸门**：不可信工作区（未在 `.wheel/trust.json` 里登记）时，工作区级目录**整个不扫**。理由写在注释里——防提示注入：别人仓库里的 `skills/` 目录可以直接往系统提示里塞指令，而用户只是 `cd` 进去而已。判定逻辑见 [tools/trust.md](../tools/trust.md)。用户级目录不受此限（在 home 下，视为用户自己的）。
- **同名 skill 先发现的胜出**：`key = skill.name.lower()`，先到先得。所以工作区级压过用户级，cwd 级压过仓库根级。`sorted(directory.glob("*/SKILL.md"))` 保证同目录内的顺序是确定的——否则"谁先被发现"会随机，同名 skill 的内容就会随文件系统顺序漂移。
- `glob("*/SKILL.md")` 只扫**一层**子目录，嵌套的 skills 不会递归。

## 7. `parse_skill`（116–126 行）

```python
description = _frontmatter_field(text, "description")
if not description:
    return None
name = _frontmatter_field(text, "name") or path.parent.name
```

**frontmatter 里必须有 `description` 才算有效 skill**，否则返回 `None` 被静默跳过。理由是 `description` 是模型在 `<available_skills>` 里唯一能看到的"我为什么要调它"的信息——没有描述的 skill 等于不可用，与其塞进列表污染选择，不如不出现。

`name` 缺省用父目录名（所以 `skills/pdf/SKILL.md` 默认叫 `pdf`）。读文件失败时 `text = ""` → description 空 → 返回 `None`，同样静默。

## 8. `expand_skill_command`（129–159 行）

把 `/skill:name` 开头的输入展开成完整任务文本：

```python
raw = text.strip()
if not raw.startswith("/skill:"):
    return text
rest = raw[len("/skill:"):]
name, _, extra = rest.partition(" ")
```

- 只认**行首**的 `/skill:`，其余原样返回（普通输入里出现 `/skill:` 不会被误伤）。
- `partition(" ")` 分成 `name` 和 `extra`，`/skill:pdf 合并这两个文件` 里 `extra` 是用户追加的话。
- `s.name == name` 是**大小写敏感的精确匹配**——而 `load_skills` 的去重键是 `name.lower()`。两边规则不一致，见第 17 节。
- 找不到 / 读不了：返回原 `text`，**不报错**。用户看到的就是未展开的 `/skill:xxx` 文本被当成普通输入发出去。

展开块长这样：

```python
block = (
    f'<skill name="{skill.name}" location="{skill.location}">\n'
    f"References are relative to {base}.\n\n{body}\n</skill>"
)
```

`body` 是剥掉 frontmatter 的正文，`base` 是 skill 文件所在目录。那句 `References are relative to {base}` 是关键：SKILL.md 的正文常引用同目录的脚本或模板文件，只报相对路径模型拼不出绝对路径，标注基准目录后模型能自己拼出来。

最后 `f"{block}\n\n{extra}"` —— **skill 正文在前、用户输入在后**，所以模型先读到完整指令再读到具体任务，符合指令跟随的习惯。

注意这里每次调用都重新扫一遍全部 skills 目录并读文件。调用点只在提交输入的时刻（[ui/app.md](../ui/app.md) 的输入处理与 `/skill` 提交），不在 agent 循环里，所以这点开销可以接受。

## 9. 两个 `format_*_xml`（162–192 行）

`format_project_xml`（162–174 行）把第 5 节收集的 `(path, text)` 渲染成：

```xml
<project_context>
Project-specific instructions and guidelines:
<project_instructions path="…">…</project_instructions>
</project_context>
```

每个文件一个块，块上带 `path`——模型（和用户）能看出这条指令来自哪个文件，出错时知道去改哪里。空列表返回 `""`（`prompt.py` 里 `if project:` 才 append，不会留下空标签）。

`format_skills_xml`（176–192 行）只渲染**元数据**（name / description / location），正文靠 `/skill:` 按需加载。这是本模块最重要的一条设计取舍：

- 把所有 skill 全文塞进系统提示会撑爆前缀，而且 skill 文件一改，整个系统提示就变——**前缀变了，prompt 缓存全部失效**。
- 只放元数据的话，前缀里只有一小段稳定文本，改 skill 内容不影响缓存；代价是模型得主动调用一次 `/skill:` 才知道正文。

XML 里硬编码了两行使用说明（"Use /skill:name to load a skill"、"Prefer workspace skills; user-level skills load via /skill:name, not the read tool."）——把使用纪律和清单绑在一起，模型看到清单就看到用法。

`_xml_escape` 对三个字段全部转义：skill 的 name/description/location 都来自用户文件，可能含 `<`、`&`、`"`，不转义会破坏标签结构（第 14 节）。

## 10. `today`（195–197 行）

```python
return datetime.now().astimezone().date().isoformat()
```

`astimezone()` 而非 `utcnow()`：拿本地时区的日期。进 `ephemeral_items` 的 `Current date:` 行。之所以放在每轮临时上下文里而不是系统提示里——日期会变，写进系统提示会让前缀每天都变、缓存每天失效。见 [core/prompt.md](prompt.md)。

## 11. `_context_dirs`（200–215 行）

从 cwd 向上走的目录链，返回**根 → cwd** 的顺序（末尾 `dirs.reverse()`）：

```python
while cur not in seen:
    seen.add(cur)
    dirs.append(cur)
    if (cur / ".git").exists():
        break
    parent = cur.parent
    if parent == cur:
        break
    cur = parent
```

- **`.git` 处停**：仓库根就是收集的上界，不会把仓库外的文件收进来。
- `parent == cur` 处理文件系统根（`/` 的 parent 是自己）。
- `seen` 防符号链接成环导致的死循环。

**没有 `.git` 时的行为**：循环一路走到 `/`，于是 `/Users/<you>/Desktop/wheel-agent`、`/Users/<you>/Desktop`、`/Users/<you>`、`/Users`、`/` 全进列表，每个目录再试 7 个文件名（第 12 节）。既慢，又可能把仓库外某个无关的 `AGENTS.md` 收进提示。见第 17 节。

## 12. `_context_file`（218–224 行）

按 `CONTEXT_NAMES` 顺序在一个目录里找第一个存在的文件，`is_file()` 而非 `exists()`（排除同名目录）。全都没有返回 `None`。

## 13. frontmatter 解析（227–247 行）

`_frontmatter_field`（227–238 行）读一个键的值：

- 必须以 `---` 开头才算有 frontmatter，否则返回 `""`。
- `text.find("\n---", 3)` 找闭合标记；找不到（frontmatter 未闭合）返回 `""`。
- 只在 `text[3:end]` 范围内逐行找 `key:` 前缀，**大小写不敏感**（`line.strip().lower().startswith(key)`，其中 `key` 已小写）。
- 值用 `line.split(":", 1)[1].strip().strip("\"'")` —— 只支持**无引号或单双引号**的值，不支持 YAML 多行块、列表、嵌套结构。够用即可：skill 的 frontmatter 只有 `name` 和 `description` 两个扁平字符串。

`_strip_frontmatter`（241–247 行）剥掉开头的 `---` 块返回正文，`end + 4` 跳过 `\n---`。未闭合时原样返回（整个文件当正文）。

## 14. `_xml_escape`（251–258 行）

```python
text.replace("&", "&amp;")   # 必须第一个
   .replace("<", "&lt;")
   .replace(">", "&gt;")
   .replace('"', "&quot;")
```

`&` 必须最先替换，否则后面生成的 `&lt;` 会被二次转义成 `&amp;lt;`。四个字符覆盖了标签结构和双引号属性的全部风险（反引号 / 单引号不转义，但属性用双引号包着，`'` 无害）。

## 15. `tag_lines`（261–265 行）

```python
match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S)
if not match:
    return []
return [line.strip() for line in match.group(1).splitlines() if line.strip()]
```

从文本里抓一对 `<tag>…</tag>` 之间的行，返回 strip 后的非空行列表。

- `re.S` 让 `.` 跨行匹配（标签内容必然是多行的）。
- `.*?` **非贪婪**：只取第一对标签，且只取最内层（`<a><a>x</a></a>` 取到 `x`）。
- **不闭合就返回 `[]`，静默**。这是有意的宽松：摘要里没有 `<read-files>` 块时不该报错，但代价是"清单丢了"这件事没有任何告警。

**实际用途**：docstring 写的是"harness 解析 `<memories>`/`<rules>` 用"，但全仓库 grep 下来唯一调用方是 `compact.py:155-156`——从**上一轮的旧摘要**里把 `<read-files>` / `<modified-files>` 捞回来，让文件清单跨多次紧凑不丢（`compact.collect_file_ops` 只扫当前历史里的 `function_call`，被压掉的历史里的路径只能靠旧摘要里的标签救回来）。harness 那条用途没有落地。

## 16. 估算值的消费方：与 compact / truncate 的分工

三个模块都要"控制上下文大小"，但用的是**三套完全不同的量**，互不重叠：

| 环节 | 用什么量 | 谁提供 | 精度要求 |
|---|---|---|---|
| **削工具输出**（一条输出留多少） | 行/字节预算：2000 行 或 50KB，先到先截 | [core/truncate.md](truncate.md) 自己的常量 | 确定性、可复现 |
| **切历史**（压多少、切在哪） | 字符数 ÷ 4 的 token 估算 | 本文件的 `estimate_*` | 只要量级 |
| **触发紧凑**（压不压） | provider 报告的**真实** `input_tokens` | [core/model.md](model.md) 的 `Usage` | 精确 |

**truncate 不 import context**（grep 确认：`truncate.py` 里没有任何 token 估算）。它是纯粹的字节/行裁剪，好处是**确定性**——同样输入永远得到同样输出，replay 时不会因为估算漂移而对不上。它在最前面就把工具输出削到 50KB 以内，等于先帮后面的 token 估算把历史变小了一圈。

**compact 的两种用法**（[core/compact.md](compact.md)）：

1. **触发**用真实用量：`should_compact(input_tokens, context_window)` 判断 `input_tokens > context_window - RESERVE_TOKENS(16384)`，`input_tokens` 来自 `loop.py` 传进来的 `last_usage.input_tokens`。**估算不参与触发**——这是整个设计的安全阀，估算哪怕偏了几倍也不会误触发或漏触发，漏了也有 `_complete_with_overflow` 的溢出兜底。
2. **选切点**用估算：`find_user_cut_index` 从尾部累加到 `KEEP_RECENT_TOKENS = 20_000` 估出保留后缀的起点，再对齐到下一个 `user` 边界；`compact_items` 里若保留部分仍超窗口，就把 `keep_recent_tokens` 逐次折半（20000 → 10000 → 5000 → 2000）重试。这里估算偏高的后果只是"保留的原文比预期少"，偏低的后果是"压完还是太大"——而后者由折半重试兜住。
3. **统计**用估算：`CompactStats.before_tokens` / `after_tokens` 纯展示（页脚计量表、`compact` 事件），错了不影响行为。

一句话：**估算只用来决定"切在哪"，不用来决定"切不切"。** 决定切不切的是 provider 报回来的真实数字。
