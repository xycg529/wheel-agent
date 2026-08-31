# `core/reasoning.py` 逐段讲解

> 本篇讲推理档位（effort）的归一化层。上游是 [`config.md`](config.md)（载入档位名单）和 [`model.md`](model.md)（把档位塞进 API 请求）、[`loop.md`](../../loop-explained.md)（页脚展示与审计）；下游无依赖。

把各家 provider 互不相同的推理档位（effort）名字归一到一套统一刻度，并在"配的档位模型不支持"时钳制到最近的可用档，最后按 API 协议组装成 `reasoning` / `reasoning_effort` 字段。

- 行数：94 行
- 依赖：无（纯字符串处理，只 import `typing.Iterable`）
- 被谁用：
  - [`config.md`](config.md) —— `parse_levels` 解析 `.env` 的 `<前缀>_REASONING_LEVELS`，`infer_effort_levels` 按模型名猜，`normalize` 归一一 `/effort` 输入
  - [`model.md`](model.md) —— `reasoning_payload` 在 Responses / Chat Completions 两个客户端里组装请求字段
  - [`loop.md`](../../loop-explained.md) —— `clamp_effort` 记进 `agent_start` 事件，`reasoning_payload` 进 meta（replay 对比用）
  - [`../ui/app.md`](../ui/app.md) —— `/effort` 命令的档位选择器和页脚显示

## 目录

- [1. 模块 docstring 与三张表（1–22 行）](#1-模块-docstring-与三张表1–22-行)
- [2. `normalize`（25–29 行）](#2-normalize25–29-行)
- [3. `parse_levels`（31–45 行）](#3-parse_levels31–45-行)
- [4. `infer_effort_levels`（47–63 行）](#4-infer_effort_levels47–63-行)
- [5. `clamp_effort`（65–87 行）](#5-clamp_effort65–87-行)
- [6. `reasoning_payload`（89–94 行）](#6-reasoning_payload89–94-行)

---

## 1. 模块 docstring 与三张表（1–22 行）

```python
LEVELS    = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
ALIASES   = {"none": "off", "x-high": "xhigh", "extra-high": "xhigh"}
API_EFFORT = {"off": "none", "minimal": "minimal", "low": "low",
              "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
```

设计要点：

- **统一刻度跨 provider 复用**：`LEVELS` 是一个有序元组，顺序即"从弱到强"。这个顺序是后面 `clamp_effort` 向上/向下找最近档的唯一依据。所有 provider 的档位名都先归一到这套刻度，比较、钳制、排序才有统一基准。
- **`off` 与 `none` 分开存**：OpenAI Responses API 把"关闭推理"拼作 `none`（因为 `off` 可能与其他布尔语义混淆），代码内部统一用 `off`，只在 `API_EFFORT` 这一层翻译成 `none`。这样"用户写法"和"API 写法"只在这一张表里耦合。
- **`max` 与 `xhigh` 分开**：`max` 是 GLM/Zhipu 的最高档。注释明说这是为了避免 `.env` 名单被"改写成" `xhigh`——即如果不把 `max` 作为独立档位，钳制逻辑会把用户配的 `max` 悄悄降级成 `xhigh`，用户配了最高档却拿到次高，且不报错。
- **`ALIASES` 只收写法差异**（`x-high` / `extra-high` → `xhigh`），不收语义差异。大小写由 `normalize` 统一处理，不必进表。

## 2. `normalize`（25–29 行）

```python
key = level.strip().lower()
return ALIASES.get(key, key)
```

去空白 + 转小写 + 查别名表。注意它**不校验档位是否合法**——不在 `LEVELS` 里的值原样返回，校验交给下游（`parse_levels` 直接报错，`clamp_effort` 兜底成 `medium`）。这样 `/effort X-HIGH ` 这种带空格的写法能容错，而非法档位在合适的层级被处理。

`normalize` 是唯一的大小写/别名入口，被 [`config.md`](config.md) 的 `AgentConfig.with_effort` 和 `load_config` 共用，保证"配置里的 effort"和"运行时改的 effort"走同一套归一化。

## 3. `parse_levels`（31–45 行）

解析 `.env` 里逗号分隔的档位名单，如 `P1_REASONING_LEVELS=low,high,max`。

```python
if level not in LEVELS:
    raise ValueError(f"unknown reasoning level {part!r}; use {', '.join(LEVELS)}")
```

设计要点：

- **未知档位直接报错，而不是静默丢弃**。注释写"配置错误越早暴露越好"：档位名单错了会导致后续每一轮钳制都走偏，静默降级的话用户只会觉得"模型不太聪明"，很难定位。宁可启动失败。
- 空串返回 `()`，含义是"非推理模型"（见第 4、6 节），不是"没配置"——区分这两者是靠 [`config.md`](config.md) 里的 `if levels_raw else`，本函数不关心。
- 保序去重（同一个档写两遍只保留一次），顺序保留 `.env` 里的书写顺序，供 `/effort` 选择器展示。
- 返回 `tuple` 而非 `list`：名单进 `ProviderConfig.effort_levels`，而 `ProviderConfig` 是 `frozen=True` 的 dataclass（见 [`config.md`](config.md)），可变容器会破坏"运行中途不可变"的约定。

## 4. `infer_effort_levels`（47–63 行）

用户没配 `*_REASONING_LEVELS` 时，按模型名猜可用档位。**返回空元组 = 非推理模型，完全不发 reasoning 参数。**

判定是一串 `any(tag in name for tag in (...))` 的前缀匹配，按顺序短路：

| 匹配 | 返回档位 | 代表模型 |
|---|---|---|
| `gpt-4o` / `gpt-4.1` / `gpt-4-turbo` / `gpt-3.5` / `deepseek-chat` / `glm-4-flash` | `()` 非推理 | 常规对话模型 |
| `o1-mini` / `o1-preview` | `low, medium, high` | 早期推理模型（无 `minimal`/`xhigh`） |
| `o1` / `o3` / `o4` | `low, medium, high` | 推理模型 |
| `gpt-5` / `codex` | `off, minimal, low, medium, high, xhigh` | 全刻度（含 `off`） |
| `grok` | `low, medium, high, xhigh` | xAI |
| `reason` / `think` / `r1` | `low, medium, high` | 名字里带推理暗示的模型（DeepSeek-R1 等） |
| 都不匹配 | `()` | 保守缺省 |

设计要点：

- **注意 `o1-mini` / `o1-preview` 那一段其实是冗余的**：它返回的结果和下面 `o1/o3/o4` 那一段完全一样。单独列出来大概率是历史遗留（早期这两个模型档位不同），保留了显式表达的意图。无副作用。
- 关键词命中 `reason` / `think` / `r1` 是**兜底启发式**：新模型只要名字里带推理标识就能拿到三档，不必改代码。代价是模型名里恰好含 "think" 的非推理模型会被误判，此时用户用 `*_REASONING_LEVELS` 显式覆盖即可。
- **猜错的最坏后果只是钳制方向不对**（比如真支持 `xhigh` 但只猜到 `high`），不会报错。想精确控制就显式配 `*_REASONING_LEVELS`，这也正是该变量的用途。

## 5. `clamp_effort`（65–87 行）

核心：**请求的档位不支持时先向上找、再向下找；返回 `None` = 省略该参数。**

```python
allowed = tuple(normalize(item) for item in supported)
allowed = tuple(item for item in allowed if item in LEVELS)   # 先剔除名单外的项
if not allowed:
    return None
want = normalize(requested) if requested else "medium"
if want not in LEVELS:
    want = "medium"
if want in allowed:
    return want
```

四步降级链：

1. **`supported` 里不在 `LEVELS` 的项先剔除**。名单来自 `.env`（用户手写）或 `infer_effort_levels`（代码生成），前者可能有作者没见过的档位名。剔除后钳制才有可靠的 `LEVELS.index()` 可用。
2. **空名单 → `None`**。非推理模型（`effort_levels=()`）走这条，下游据此完全不发 reasoning 字段。
3. **请求值缺失或非法 → 兜底 `medium`**。空字符串、拼错的名字都落到这里，不会因为一个配置 typo 就让整个 run 失败。
4. **命中就直接返回**，不做任何翻译。

真正请求/名单不匹配时：

```python
for candidate in LEVELS[index + 1:]:        # 先向上找更强的档
    if candidate in allowed:
        return candidate
for candidate in reversed(LEVELS[:index]):  # 再向下找最接近的弱档
    if candidate in allowed:
        return candidate
return None
```

**为什么先向上？** 用户要 `high` 但模型只有 `low, medium` → 给 `medium`（向下找）；用户要 `low` 但模型只有 `high, xhigh` → 给 `high`（向上找）。向上优先的取舍是：**宁可多花 token 让模型想久一点，也不愿悄悄降智**。配了 `high` 但拿到 `low` 会让人以为模型变笨了，而多想一会儿只是慢一点、贵一点。两个方向都找不到就返回 `None`（不发参数）——只在名单非但空却全部被 `LEVELS` 剔除的极端情况下发生。

`clamp_effort` 的两个用处：

- [`model.md`](model.md)：经 `reasoning_payload` 间接调用，决定实际发什么。
- [`../ui/app.md`](../ui/app.md) 的 `/effort` 命令：`current = clamp_effort(config.effort, levels) or levels[0]`，用来算选择器默认高亮哪一项（配置里存的档位可能已被钳制过）。

## 6. `reasoning_payload`（89–94 行）

```python
clamped = clamp_effort(requested, supported)
if clamped is None or clamped == "off":   # 非推理模型或显式 off：不发，兼容所有端点
    return None
return {"effort": API_EFFORT[clamped], "summary": "detailed"}
```

返回 `dict` 或 `None`——**`None` 统一表示"这个请求不带 reasoning 字段"**，两种来源合并成一个出口：

- 非推理模型（`clamp_effort` 返回 `None`）；
- 用户显式选了 `off`。

第二种很关键：显式 `off` 也走"不发字段"，而不是发 `{"effort": "none"}`。注释写明理由是"兼容所有端点"——`off`/`none` 在各家 API 里拼写不一（有的用 `none`，有的用字符串 `"off"`，有的根本不认），不发字段是唯一所有端点都接受的做法。代价是 `off` 档位拿不到 `summary` 文本。

返回体除了 `effort` 还带 `summary: "detailed"`，要求 API 回传推理摘要文本，供 UI 显示"思考过程"（流式事件 `response.reasoning_summary_text.delta`，见 [`model.md`](model.md)）。

**两个 API 协议怎么消费这同一个 payload：**

- **Responses API**（`ResponsesClient.complete`，`core/model.py` 624–627 行）：`payload` 整体塞进 `reasoning` 键，并追加 `include=["reasoning.encrypted_content"]`（多轮时要回传加密推理内容才能维持缓存前缀）。
- **Chat Completions**（`ChatCompletionsClient.complete`，679–681 行）：只取 `payload["effort"]` 塞进**扁平的** `reasoning_effort` 键，且额外过滤 `not in {None, "none"}`——Chat 协议没有 `summary` 概念，`reasoning_effort` 也不接受 `none`。

也就是说：**协议差异（嵌套 vs 扁平、要不要 summary）由 model.py 处理，本模块只负责产出统一刻度的 `effort` 值。** 这是分层的意义——加一个新协议只需在 model.py 加一个分支。

**不支持 reasoning 的 provider 怎么处理（三层防线）**：

1. `infer_effort_levels` 认不出就返回 `()` → `reasoning_payload` 返回 `None` → 字段根本不进请求。
2. 误判或名单过宽导致字段照样发出时，[`model.md`](model.md) 的 `_drop_create` 兜底：端点报"不认识这个参数"（`_is_param_error`）就把参数逐个丢掉重试，两个客户端的 `drop_order` 都把 reasoning 系排在**最后丢**（Responses：`include, prompt_cache_retention, prompt_cache_options, prompt_cache_key, reasoning`；Chat：`reasoning_effort, reasoning, tool_choice`）——先牺牲缓存这类优化参数，尽量保住推理档位。
3. 流式请求还有一层：`_once` 遇到参数错误会退回非流式 `_create` 再试（见 [`model.md`](model.md)）。

---
