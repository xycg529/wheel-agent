# `core/meter.py` 逐段讲解

> 本篇讲计量表（meter）。上游是 [`ui/app/live.py`](../ui/app-live.md) 的页脚渲染，下游是 [`core/types.py`](./types.md)（`Usage`）和 [`core/config.py`](./config.md)（provider 定价字段）。

把一次运行的 token 用量折算成"人看得懂"的一行文本：数字缩写、缓存命中率、美元成本、上下文占用百分比。

- 行数：103 行
- 依赖：
  - [`core/types.py`](./types.md) —— `Usage` 是全部计算的唯一入参（五个 token 计数）
- 被谁用：
  - [`ui/app/live.py`](../ui/app-live.md)（`_meter_text()`，192–204 行）—— 包一层从 `config.provider` 取价格，组装页脚字符串
  - [`ui/app/__init__.py`](../ui/app.md) —— `compact_count()` 单独用于启动横幅的上下文窗口显示（326 行）
  - [`ui/app/commands.py`](../ui/app-commands.md) —— `/compact` 后打印 `~12.3k → ~8.1k tok`（190 行）
  - [`core/config.py`](./config.md) —— `infer_model_profile()` 用于 provider 未配价格/窗口时按模型名推断（`_load_provider()`，139 行）

设计上的定位：**这个模块只做纯计算和格式化，不碰 I/O、不认识 `Session`、不认识 `EventBus`。** 价格从外部当参数传进来（而不是自己读 config），所以同一套算法对任何 provider 都成立。

## 目录

- [1. `compact_count()`：数字缩写](#1-compact_count数字缩写--821-行)
- [2. `infer_model_profile()`：模型价格表](#2-infer_model_profile模型价格表--2240-行)
- [3. `cache_hit_pct()`：缓存命中率](#3-cache_hit_pct缓存命中率--4256-行)
- [4. `cost_usd()`：美元成本](#4-cost_usd美元成本--5870-行)
- [5. `format_meter()`：页脚那一行](#5-format_meter页脚那一行--72103-行)

---

## 1. `compact_count()`：数字缩写 （8–21 行）

把 token 数压成 `12.3k` / `1.2M` 样式。四档：

| 量级 | 输出 | 例子 |
|---|---|---|
| ≥ 1,000,000 | `x.xM`，`.0M` 缩成 `M` | `1_200_000` → `1.2M` |
| ≥ 10,000 | 整数 `Nk`（不保留小数） | `12_345` → `12k` |
| ≥ 1,000 | `x.xk`，`.0k` 缩成 `k` | `1_200` → `1.2k` |
| < 1,000 | 原样整数 | `847` → `847` |

两处细节：

- **10k 以上砍掉小数**。页脚横向空间按字符计价，五位数再带一位小数（如 `12.3k`）比 `12k` 多两个字符，而在这个量级上 0.3k 的精度没有意义。
- `.0M` / `.0k` 的 replace 是纯显示优化：`1_000_000` 打 `1.0M` 不如打 `1M`。注意 replace 只匹配 `.0M`/`.0k` 全串，`10.0M` 不会被误伤成 `10M`（它匹配的是 `".0M"` 子串，这里正好是后缀，安全）。

## 2. `infer_model_profile()`：模型价格表 （22–40 行）

返回 5 元组 `(context_window, input_price, output_price, cache_read_price, cache_write_price)`，价格单位**美元 / 每百万 token**。

```python
if "gpt-5.6" in name:  return 272_000, 5.0, 30.0, 0.5, 6.25
if "gpt-5" in name or "codex" in name:  return 256_000, 1.25, 10.0, 0.125, 0.0
```

匹配方式是**模型名小写后的子串包含**，且**顺序敏感**：`gpt-5.6` 必须排在 `gpt-5` 前面，否则 `gpt-5.6` 会被更宽的 `gpt-5` 规则截走。这是这段唯一容易改坏的地方——新加规则时要检查是否会被前面的规则吃掉。

覆盖的模型族：`gpt-5.6`、`gpt-5`/`codex`、`grok-4`、`gpt-4.1`、`gpt-4o`、`deepseek`。

**兜底返回 128_000 窗口 + 全零价**（40 行）。零价意味着成本恒为 0，UI 显示 `$0.000`——未知模型、自托管、本地 mock 都走这条路，宁可少报也不瞎编价格。`128_000` 的兜底窗口也是保守值：偏小会提前触发 auto-compact（安全），偏大会撑爆上下文（报错）。

**这张表只是默认值，不是权威定价。** 真正的取值在 [`config.py`](./config.md) 的 `_load_provider()`（139–146 行）：

```python
window, in_price, out_price, cache_read, cache_write = infer_model_profile(model)
...
context_window=int(_env(f"{prefix}_CONTEXT_WINDOW") or window),
input_price=float(_env(f"{prefix}_INPUT_PRICE") or in_price),
```

即：**先查表，再用 `<前缀>_INPUT_PRICE` 等环境变量覆盖**。所以 `.env` 里配了 `OPENAI_INPUT_PRICE` 就以它为准；表只是让"不配也能跑"且数字大致靠谱。价格表会过期，这是刻意的取舍——把定价权留给用户，代码里只放一份能用的近似值。

## 3. `cache_hit_pct()`：缓存命中率 （42–56 行）

返回 0–100 的百分比。核心是**兼容两种 provider 的记账口径**（这也是全模块最值得一看的设计）：

| 口径 | 表现 | 命中率公式 |
|---|---|---|
| OpenAI 风格（cached ⊆ input） | `cached <= input`，input **包含**缓存部分 | `cached / input` |
| 部分代理（input 只报未命中部分） | `cached > input` | `cached / (cached + input)` |

```python
if cached <= inp:  return cached / inp * 100.0
return cached / (cached + inp) * 100.0
```

两种口径下分母都是"这次请求的总输入量"，只是总输入量在两种协议里落在不同字段上。不区分的话，代理场景会算出 >100% 的命中率。

三个守卫：`cached <= 0` 返回 0（无命中）；`inp <= 0` 而 `cached > 0` 返回 100（input 全 0 说明报上来的输入全部来自缓存）。函数保证返回值恒定落在 0–100，不做 clamp——因为每个分支的数学结果本身就在区间内。

## 4. `cost_usd()`：美元成本 （58–70 行）

四档价目加权求和，除以 1M 换算：

```python
billed_in = inp - cached if cached <= inp else inp   # input 含缓存时，命中部分剥出来单算
return (billed_in / 1_000_000 * input_price
        + cached / 1_000_000 * cache_read_price
        + usage.output_tokens / 1_000_000 * output_price
        + usage.cache_write_tokens / 1_000_000 * cache_write_price)
```

**关键在 `billed_in` 这一行**：OpenAI 的 `input_tokens` 包含缓存命中的部分，而命中部分不按 `input_price` 收费，只按更便宜的 `cache_read_price` 收。所以要从 input 里把 cached 剥掉，否则命中部分被按原价重复计算——前缀缓存的省钱效果在成本表上就完全看不出来了。

`cached > inp` 时（第 3 节的代理口径）input 本身就是未命中量，`billed_in = inp`，不剥。

`cache_write_tokens` 单独计费：写入缓存比普通输入贵（表上 `gpt-5.6` 的 6.25 vs 输入 5.0），这正是 README 里"provider 对缓存写入计费"在成本表上的体现——压缩会重写前缀、触发一轮新的缓存写入，这笔钱会被如实记进来。

输出 token 不做任何折算，`usage.output_tokens` 直接计价。

## 5. `format_meter()`：页脚那一行 （72–103 行）

签名 **5 个必填关键字参数**（价格四件套 + 上下文窗口）+ `compact_runs`。全部走 keyword-only，因为五个连续 float 位置传参极易对错顺序，而价格传错在 UI 上完全看不出来。

**`total` vs `last` 的双用量设计**（79 行）：

```python
req = last if (last.input_tokens or last.output_tokens or last.cached_tokens) else total
```

- `req`（"本次"）：优先展示**最近一次模型调用**的量——页脚回答的是"现在这一轮多大"，而不是"历史累计多大"。
- `total`（"累计"）：只在最后两个字段出现（`Σ↑` 汇总、成本 `$`）。

**成本用 `total` 算，其余量用 `req` 算**（81 行）。这是对的：成本是累计消费，必须按整个会话的用量算；而输入/输出/R 展示的是当前状态的切片。首次调用前 `last` 还是空 `Usage`，`req` 退回 `total`，两个视角重合。

产出字段按顺序（83–103 行）：

一次实测输出（total=45600/8100/1200，last=12300/8100/1200，gpt-5.6 价目，窗口 128k，压缩 3 次）：

```
↑12k ↓1.2k R8.1k CH65.9% $0.228 9.6%/128k Σ↑46k C3
```

| 字段 | 来源 | 含义 | 出现条件 |
|---|---|---|---|
| `↑12k` | `req.input_tokens` | 输入 token | 总是 |
| `↓1.2k` | `req.output_tokens` | 输出 token | 总是 |
| `R8.1k` | `req.cached_tokens` | 缓存命中（Read）token | 总是（0 就显示 `R0`） |
| `CH65.9%` | `cache_hit_pct(req)` | 缓存命中率 | 总是 |
| `$0.228` | `cost_usd(total, ...)` | 累计成本 | 总是 |
| `9.6%/128k` | `occupied / context_window` | 上下文占用 | `context_window > 0` |
| `Σ↑46k` | `total.input_tokens` | 累计输入 | 总量 ≠ 本次量时 |
| `C3` | `compact_runs` | 压缩次数 | 非 0 时 |

空用量下的输出，可见条件字段全部省略：`↑0 ↓0 R0 CH0.0% $0.000 0.0%/128k`。

后两个是**条件字段**——页脚只有一行，无信息时不如不占地方。上下文占用百分比是 README 里"`auto-compact` 看真实用量触发"的那个数的可视化来源：UI 上看到逼近 100%，就知道下一轮要触发压缩了。

`occupied` 的取法（82 行）：`req.input_tokens` 非空用它，否则退 `total.input_tokens`。和 `req` 的选取逻辑一致，保证"百分比"和"↑"两个数来自同一份用量。

调用方 [`ui/app/live.py:_meter_text()`](../ui/app-live.md)（192–204 行）做的就是把 `config.provider` 上的四个价格和 `context_window` 摊平传进来，再把 `session.compactions` 塞给 `compact_runs`。
