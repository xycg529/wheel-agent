# `core/model.py` 逐段讲解

> 本篇讲模型客户端。上游是 [core/loop.py](../../../core/loop.py)（每次回合调 `complete()`）和 [ui/app](../../../ui/app/__init__.py)（构造客户端），下游是 `openai` SDK 和 [core/config.py](config.md)（`ProviderConfig`）。

把 OpenAI 的 **Responses** 和 **Chat Completions** 两套协议，外加一个录制脚本替身，归一成同一份 `output` item 列表和同一个 `Usage`，并附带流式增量、可中断等待、指数退避重试、错误归类。

- 行数：981 行
- 依赖：
  - [core/config.py](config.md) —— `ProviderConfig`（endpoint、模型、档位列表）
  - [core/reasoning.py](reasoning.md) —— `reasoning_payload()`（按 API 组装推理档位字段）
  - [core/types.py](types.md) —— `APIError` / `Item` / `ModelResponse` / `Usage`
  - `openai` SDK（唯一运行时依赖之一），延迟到 `_OpenAIBase.__init__` 里 import
- 被谁用：
  - [core/loop.py](../../../core/loop.py) —— 主循环通过 `ModelClient` 协议调用，不关心具体协议
  - [ui/app](../../../ui/app/__init__.py) —— `make_client()` 建客户端，并挂上 `abort` / `on_retry`
  - [ui/replay.py](../../../ui/replay.py) —— `ScriptedModel` 代替真模型回放录制响应
  - [core/compact.py](compact.md) —— 用同一个客户端生成压缩摘要
  - [ui/app/live.py](../../../ui/app/live.py) / [ui/graph.py](../../../ui/graph.py) / [core/session.py](session.md) —— `extract_text` / `extract_thinking` / `item_text` 展示用

## 目录

- [0. 速查表](#0-速查表)
- [1. 协议常量与 ModelClient 抽象](#1-协议常量与-modelclient-抽象1–42-行)
- [2. item 序列化与文本提取](#2-item-序列化与文本提取44–118-行)
- [3. 字段访问的小工具](#3-字段访问的小工具120–158-行)
- [4. usage_from_response：两种协议的用量归一](#4-usage_from_response两种协议的用量归一161–185-行)
- [5. 流事件读取三件套](#5-流事件读取三件套187–234-行)
- [6. item_text 与工具/消息格式转换](#6-item_text-与工具消息格式转换236–357-行)
- [7. Chat 协议的消息构造](#7-chat-协议的消息构造359–412-行)
- [8. _ChatAssembler：把 Chat 流拼回一条消息](#8-_chatassembler把-chat-流拼回一条消息414–494-行)
- [9. _OpenAIBase：两个客户端的公共底座](#9-_openaibase两个客户端的公共底座497–607-行)
- [10. ResponsesClient](#10-responsesclient610–664-行)
- [11. ChatCompletionsClient](#11-chatcompletionsclient667–730-行)
- [12. 错误归类：参数错误 vs 流式不支持 vs 瞬时错误](#12-错误归类参数错误-vs-流式不支持-vs-瞬时错误733–831-行)
- [13. _to_api_error 与 _await_abortable](#13-_to_api_error-与-_await_abortable833–876-行)
- [14. call_with_retry：指数退避与可中断等待](#14-call_with_retry指数退避与可中断等待878–924-行)
- [15. ScriptedModel：确定性替身](#15-scriptedmodel确定性替身926–956-行)
- [16. item 构造函数与 make_client 工厂](#16-item-构造函数与-make_client-工厂958–981-行)
- [17. parse_function_calls 的调用关系](#17-parse_function_calls-的调用关系)

---

## 0. 速查表

| 名字 | 行号 | 职责 |
|---|---|---|
| `ModelClient` | 32–42 | 客户端 Protocol：只有一个 `complete()` |
| `item_to_dict` | 44–53 | SDK 对象 / dict 统一转 dict |
| `extract_thinking` | 55–99 | 从响应 items 里拼出全部思考文本 |
| `extract_text` | 101–118 | 从响应 items 里拼出可见文本（不含思考） |
| `_nested` / `_fget` / `_int` / `_looks_like_usage` | 120–158 | 跨 SDK 版本的容错字段访问 |
| `usage_from_response` | 161–185 | 两种协议的 usage → 统一 `Usage` |
| `event_type` / `event_delta` / `event_response` | 187–209 | 读流事件的三个 accessor |
| `consume_stream_event` | 211–234 | 消费一个 Responses 流事件，喂 `on_delta` |
| `item_text` | 236–251 | 取一条 item 的文本（`content` 可能是 str 或分片列表） |
| `tools_to_chat` | 253–271 | Responses 工具声明 → Chat `function` 包装 |
| `items_to_chat_messages` | 273–357 | 统一 item 列表 → Chat `messages` |
| `_delta_str` / `_call_parts` | 359–377 | Chat 流里 tool_call 片段的字段提取 |
| `chat_message_to_output` | 379–412 | Chat `message` → 统一 `output` items |
| `_ChatAssembler` | 414–494 | 把 Chat 流式 chunk 累积成一条完整消息 |
| `_OpenAIBase` | 497–607 | 公共底座：客户端构造、cancel、重试包装、丢参数降级、drain 流 |
| `ResponsesClient` | 610–664 | Responses 协议实现 |
| `ChatCompletionsClient` | 667–730 | Chat Completions 协议实现 |
| `_is_param_error` | 733–744 | 是否 400 参数错误（该丢参数，而非整次重试） |
| `_is_stream_unsupported` | 746–753 | 端点不支持流式 → 退回非流式 |
| `TRANSIENT_MARKERS` | 755–780 | 瞬时错误关键字表 |
| `_status_code` | 782–795 | 从异常里抠 HTTP 状态码 |
| `is_transient_error` | 797–810 | 是否值得重试 |
| `brief_api_error` | 812–831 | 异常压成一行可读文本 |
| `_to_api_error` | 833–841 | 异常 → `APIError`（带 provider/model/transient/status） |
| `_await_abortable` | 843–876 | 后台线程跑请求，主线程轮询 abort |
| `call_with_retry` | 878–910 | 指数退避重试 |
| `_sleep_abortable` | 912–924 | 可中断 sleep（0.1s 切片） |
| `ScriptedModel` | 926–956 | 按脚本返回预设输出的确定性替身 |
| `function_call_item` / `assistant_text` | 958–971 | 构造统一格式的 item |
| `make_client` | 973–981 | 按 `provider.api` 选客户端的工厂 |

---

## 1. 协议常量与 ModelClient 抽象（1–42 行）

模块 docstring 点明职责边界：**适配 + 归一 + 重试 + 错误归类**。

两个流式增量类型集合（23–30 行）是"各家代理命名略有差异"的直接产物，`TEXT_DELTA_TYPES` 和 `THINKING_DELTA_TYPES` 各列全集而非精确匹配。这不是防御性冗余——兼容端点的事件名确实不统一，硬编码单一名字会在换 provider 时静默丢掉整个流式输出。

```python
class ModelClient(Protocol):
    def complete(self, input_items, tools, instructions, on_delta=None) -> ModelResponse: ...
```

用 `Protocol` 而不是 ABC（32–42 行），是**结构化类型**：三个实现（`ResponsesClient`、`ChatCompletionsClient`、`ScriptedModel`）不需要继承同一个基类，只要签名对得上就能替换。replay 时把真客户端换成 `ScriptedModel`，主循环一行代码都不用改。

`DeltaFn = Callable[[str, str], None]`（21 行）：第一个参数是 `"text"` 或 `"thinking"`，第二个是片段。UI 层据此决定用亮色还是暗色渲染，见 [ui/app/live.py](../../../ui/app/live.py)。

## 2. item 序列化与文本提取（44–118 行）

`item_to_dict`（44–53 行）按 `dict` → `model_dump(mode="json")` → `to_dict()` 三级降级。openai SDK 各版本、各兼容代理返回的对象类型不一致，归一化到裸 `dict` 后，下游（[events.py](events.md) 落盘、[session.py](session.md) 存 JSONL、replay 比对）只需要处理一种形态。

`extract_thinking`（55–99 行）是本模块最啰嗦但必要的函数。思考文本在三种协议形态里可能藏在四个位置：

- item 顶层 `thinking` 字段；
- `summary` 列表（元素可能是 `{"text":…}`、`{"summary_text":…}` 或裸 str）；
- `content` 列表里的 `reasoning_text` / `summary_text` / `thinking` 分片；
- `type == "message"` 的 item 里混着的 `reasoning_text` 分片（94–98 行单独处理）。

四种全收，是因为**不同 provider 各用一种**。这里宁可重复收集也不漏，函数只用于 UI 展示，重复内容的代价远小于漏掉思考过程。

`extract_text`（101–118 行）反过来：只取 `message` 里的 `output_text` / `text`，**不含思考**。末尾 `strip()` 而 `extract_thinking` 用 `"\n".join`（99 行）——文本要拼成连续一段，思考段落之间要保留换行。

## 3. 字段访问的小工具（120–158 行）

`_nested`（按路径逐层取嵌套字段）、`_fget`（dict 和对象都能读）、`_int`（`None`/非数字当 0）、`_looks_like_usage`（判断一个 blob 是不是用量对象）。

四个函数都是为"响应结构不可控"服务。`_looks_like_usage` 存在的理由写在 161–169 行：**有些响应把 usage 放在顶层**而不是 `response.usage` 下，得先探测再取。

## 4. `usage_from_response`：两种协议的用量归一（161–185 行）

设计意图是一行：**不管哪个协议、哪个 SDK 版本，出来就是同一个 `Usage`**。

字段名差异的映射关系：

| 统一字段 | Responses | Chat |
|---|---|---|
| `input_tokens` | `input_tokens` | `prompt_tokens` |
| `output_tokens` | `output_tokens` | `completion_tokens` |
| `cached_tokens` | `input_tokens_details.cached_tokens` | `cache_read_input_tokens` / `cached_tokens` |
| `cache_write_tokens` | — | `cache_creation_input_tokens` |
| `reasoning_tokens` | `output_tokens_details.reasoning_tokens` | — |

```python
details = _fget(usage, "input_tokens_details") or _fget(usage, "prompt_tokens_details") or {}
cached_tokens=_int(
    _nested(details, "cached_tokens")
    or _fget(usage, "cache_read_input_tokens")
    or _fget(usage, "cached_tokens")
),
```

三级 `or` 链（178–184 行）不是冗余：Anthropic 风格报 `cache_read_input_tokens`，OpenAI 风格报 `details.cached_tokens`，某些代理直接摊平到顶层。取到哪个用哪个。

`cache_write_tokens` 单独记一路（183 行），因为 **provider 对缓存写入是按更高价计费的**，[ui/app/live.py](../../../ui/app/live.py) 的计量表要能分开显示。缓存策略见 [core/compact.py](compact.md)：压缩会在用户消息边界切一刀，切点之前的前缀换成摘要、切点之后逐字节不动，就是为了保住这里报出来的 `cached_tokens`。

## 5. 流事件读取三件套（187–234 行）

`event_type` / `event_delta` / `event_response` 三个 accessor，各自处理 dict 和对象两种形态。`event_delta`（192–204 行）额外处理"delta 可能是 str 也可能是 `{"text":…}` dict"。

`consume_stream_event`（211–234 行）是 Responses 流的单个事件处理器：

```python
if on_delta and kind in TEXT_DELTA_TYPES:
    on_delta("text", chunk)
elif on_delta and kind in THINKING_DELTA_TYPES:
    on_delta("thinking", chunk)
elif on_delta and kind == "response.output_item.added":
    ...
    if typ in {"reasoning", "thinking"}:
        on_delta("thinking", "")   # 空串 = “进入思考块”信号
```

两个设计点：

1. **空字符串是信号不是数据**（232 行）。进入思考块时发一个空片段，UI 据此切换区块样式，随后真正的思考内容才陆续到达。这让 UI 能在第一段思考文本到达**之前**就把渲染模式切好，避免第一帧闪一下亮色。
2. 函数返回 `response.completed` 事件的最终响应对象（233–234 行），其余返回 `None`——返回值即"流结束"标志，drain 循环见 `_iter_stream`（586–607 行）。

## 6. `item_text` 与工具/消息格式转换（236–357 行）

`item_text`（236–251 行）：`content` 可能是 str、分片列表、`None`，统一拼成 str。`part.get("type") in {...} or part.get("text")` 的 `or` 是兜底——有些代理的分片不带 `type` 只有 `text`。

`tools_to_chat`（253–271 行）：Responses 的工具声明是 `{type: "function", function: {...}}`，本身已经是 Chat 格式，所以先判断"已经是就原样放进"（256–258 行），否则从扁平的 `name`/`description`/`parameters` 包装一层。这样 [tools/tools.py](../../../tools/tools.py) 里的 `tool_schemas()` 只需产出一版声明，两个协议共用。

`items_to_chat_messages`（273–357 行）是整个模块最微妙的函数，docstring 点出难点：**Responses 里一次模型输出是 "message + N 个 function_call" 的并列 items，Chat 要求合并进一条 assistant 消息的 `tool_calls` 字段。**

转换规则按 item 类型分派（286–302 行）：

- `function_call_output` → `{"role": "tool", "tool_call_id": …}`；
- `role == "user"` / `"system"` → 对应角色的消息；
- `reasoning` / `thinking` → **直接跳过**（299–301 行）。Chat 协议的思考内容要么走 `reasoning_content` 字段，要么不该回传；把 Responses 的 reasoning item 塞进 messages 会污染序列。

合并逻辑（303–352 行）用 while 循环向后扫描连续的 `message` / `function_call` / `reasoning`，攒成一组：

```python
body = "\n".join(texts)
message = {"role": "assistant", "content": body or None}
if calls:
    message["tool_calls"] = calls
if message["content"] or calls:
    messages.append(message)
```

三个细节：

1. **`content` 用 `None` 而非 `""`**（344–348 行）：只有工具调用没有文本时，Chat 协议要求 `content` 为 `null`（部分严格端点会拒空字符串）。
2. 整组为空（既无文本也无调用）就不发消息（347 行），避免发空 assistant 消息导致某些端点报 400。
3. `function_call` 的 `arguments` 必须是 **JSON 字符串**（317–319 行），dict 就 `json.dumps`。这是 Responses 和 Chat 的共同要求。

## 7. Chat 协议的消息构造（359–412 行）

`_call_parts`（363–377 行）从 Chat 的 tool_call 里抠 `id` / `name` / `arguments`，dict 和对象两条分支，缺失时用 `default_id` / `default_args` 兜底。

`chat_message_to_output`（379–412 行）反向转换：Chat `message` → 统一 `output` items。顺序固定为 **reasoning → text → function_call**：

```python
reasoning = message.get("reasoning_content") or message.get("reasoning")
if isinstance(reasoning, str) and reasoning.strip():
    output.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": reasoning}]})
```

思考内容被包装成 Responses 形态的 `reasoning` item（391–397 行），`summary` 用列表包 `summary_text`——这是 `extract_thinking` 认可的第二、三种形态之一，两个方向的转换因此闭环。

工具调用转成 `function_call_item`（407–409 行），`call_id` 缺失时用 `call_{index}` 补（397、408 行的 `default_id`）。**这保证了下游 `parse_function_calls` 永远能拿到非空的 `call_id`**——工具结果的配对全靠它。

## 8. `_ChatAssembler`：把 Chat 流拼回一条消息（414–494 行）

Chat 流式返回的 tool_call 是**分片**的：第一个 chunk 给 `id` 和 `name`，后续每个 chunk 给一小段 `arguments` 字符串。必须按 `index` 累积拼接。

```python
slot = self.tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
if call_id: slot["id"] = call_id
if name: slot["name"] = name
if arguments: slot["arguments"] += str(arguments)
```

`self.tools` 是 `dict[int, dict]`（420 行），按 index 分槽（462–468 行）。`arguments` 用 `+=` 而非 `=`——这是分片累积的核心。`output()` 时按 `sorted(self.tools)` 遍历（489 行），保证顺序稳定，**replay 比对时工具顺序不会因 dict 遍历而抖动**。

`feed()`（423–457 行）同样分 dict / 对象两条分支，顺手记下 `id` 和 `usage`。`usage` 单独存（429、441 行）是因为 Chat 流的最后一个 chunk 才带 usage（需 `stream_options.include_usage`），而 `output()` 需要构造一个"假 message"喂给 `chat_message_to_output`（470–494 行）——复用同一套转换逻辑，流式和非流式产出的 item 形态完全一致。

## 9. `_OpenAIBase`：两个客户端的公共底座（497–607 行）

构造（500–512 行）：

```python
timeout = float(os.getenv("WHEEL_TIMEOUT") or "180")
self.client = OpenAI(api_key=provider.api_key or "sk-none", base_url=..., timeout=timeout, max_retries=0)
```

- `max_retries=0`（511 行）：**关掉 SDK 自带重试**，重试策略由本模块的 `call_with_retry` 统一管。否则会出现"SDK 内部重试 2 次 × 外层重试 4 次 = 8 次"的乘法爆炸。
- `api_key or "sk-none"`（509 行）：本地端点（Ollama、vLLM）不需要真 key，但 SDK 会校验 key 非空。
- `from openai import OpenAI` 放在函数内（504 行）：延迟 import，`--json` 等不需要模型的路径不必付 import 开销。

**`cancel()`**（514–537 行）值得单独说。注释解释了为什么关的是 **HTTP client 而不只是 stream**：

> Closing the HTTP client on purpose, not just the stream: it is the only reliable way to abort a *stuck* non-streaming request (the socket close propagates to the request thread).

只关 stream 对"卡死的非流式请求"无效——socket 关了请求线程才会退出。代价是 client 实例报废，所以注释里补一句 "Safe because a client instance is per-task and rebuilt after a cancel"：**每个任务一个新 client，取消即弃**。

**`_call()`**（539–552 行）是三层包装的最外层：

```
_await_abortable(            # ③ 后台线程跑，主线程轮询 abort
  call_with_retry(...)       # ② 指数退避重试
) → 失败则 _to_api_error     # ① 异常归一成 APIError
```

`KeyboardInterrupt` 和 `APIError` 直接上抛不包装（548–551 行）——前者要交给 [loop.py](../../../core/loop.py) 的 abort 处理，后者已经是归一过的。

**`_once()`**（554–566 行）决定走流式还是非流式：

```python
if on_delta:
    try:
        return self._stream(kwargs, on_delta)
    except Exception as exc:
        if flag is not None and flag.is_set():
            raise KeyboardInterrupt from exc
        if not (_is_param_error(exc) or _is_stream_unsupported(exc)):
            raise
        return self._create(kwargs)
```

流式失败时**先查 abort**（561–562 行）：abort 已置位说明是用户中断，转成 `KeyboardInterrupt` 而不是降级重试。否则只有参数错误或"端点不支持流式"才降级到非流式（563–565 行）——其他错误照常上抛给重试层。

**`_drop_create()`**（568–583 行）是"丢参数降级"：按 `drop_order` 顺序每次丢一个 kwarg 重试，直到成功或丢无可丢。

```python
for key in drop_order:
    if key in pending:
        pending.pop(key)
        break
else:
    raise
```

`for...else` 的 `else` 分支（578–579 行）在**循环没 break** 时执行，即所有可丢参数都丢了仍失败——此时原样上抛。设计意图写在类 docstring：**proxies vary**——兼容端点支持的扩展参数各不相同，逐个降级比一次性全丢更能保住功能（先丢次要的 `include`，`reasoning` 留到最后）。

**`_iter_stream()`**（585–607 行）drain 流，每个事件前查 abort：

```python
for event in stream:
    if abort.is_set(): raise KeyboardInterrupt
    value = on_event(event)
    if value is not None: final = value
except KeyboardInterrupt:
    self.cancel()   # 中断必须关掉底层连接
```

`finally` 里清 `_stream_obj`（605–606 行），保证 `cancel()` 不会重复关已结束的流。返回值是**最后一个非 None 的事件结果**（596 行、607 行），即 `consume_stream_event` 在 `response.completed` 时吐出的最终响应。

## 10. `ResponsesClient`（610–664 行）

`complete()`（611–641 行）组装 kwargs：

```python
kwargs = {"model": ..., "input": input_items, "instructions": instructions, "store": False}
payload = reasoning_payload(self.effort, self.provider.effort_levels)
if payload:
    kwargs["reasoning"] = payload
    kwargs["include"] = ["reasoning.encrypted_content"]
if tools:
    kwargs["tools"] = tools
if self.cache_key:
    kwargs["prompt_cache_key"] = self.cache_key
    kwargs["prompt_cache_retention"] = "24h"
```

- **`store=False`**（618 行）：不让 provider 存这份输入。本项目的会话已经落在本地 JSONL 里（[session.py](session.md)），没必要让 provider 再存一份，也避免敏感代码上传后留存。
- **`include=["reasoning.encrypted_content"]`**（624 行）：多轮对话时，思考内容要能回传给 provider 才能保住推理连续性；加密形态保证中间人看不到。
- **prompt cache 两件套**（631–633 行）：`cache_key` 来自 session 的 `cache_epoch`（见 [loop.py](../../../core/loop.py) 的 `_sync_cache_key`），`retention="24h"` 固定。缓存键只在压缩时递增——**普通回合前缀没变、键没变，缓存持续命中**。上一段报出来的 `cached_tokens` 就是这里命中的量。

输出归一（635–641 行）：`[item_to_dict(item) for item in response.output]`，配 `usage_from_response` 和 `raw_id`。

`_create()`（643–652 行）的丢参数顺序是 `include` → `prompt_cache_retention` → `prompt_cache_options` → `prompt_cache_key` → `reasoning`：文档字符串写明"扩展参数先丢，reasoning 最后丢"——**推理档位是用户明确要求的，缓存和 include 是优化项**，先牺牲优化项。

`_stream()`（654–664 行）拿到流后交给 `_iter_stream`；若事件里没出现 `response.completed`（`final is None`），退而调 `get_final_response()`（660–662 行）——有些代理不发 completed 事件。两者都没有才抛 `RuntimeError`（663 行）。

## 11. `ChatCompletionsClient`（667–730 行）

`complete()`（668–702 行）的 kwargs 走 Chat 形态：`messages=items_to_chat_messages(input_items, instructions)`（673 行）。

推理档位字段不同（675–677 行）：

```python
payload = reasoning_payload(self.effort, self.provider.effort_levels)
if payload and payload.get("effort") not in {None, "none"}:
    kwargs["reasoning_effort"] = payload["effort"]
```

Chat 用扁平的 `reasoning_effort` 字符串，Responses 用 `reasoning` 对象——差异封在 [core/reasoning.py](reasoning.md) 的 `reasoning_payload()` 里，这里只做 `not in {None, "none"}` 的过滤（677 行）：非推理模型完全不发这个字段。

工具要 `tool_choice="auto"`（679–680 行）。

响应解析（683–702 行）兼容对象和 dict 两种形态取 `choices[0].message`，再交给 `chat_message_to_output`。注意 `if isinstance(response, ModelResponse): return response`（684–685 行）——**流式路径已经返回 `ModelResponse` 了**（见 719–728 行），这里直接透传。

`_create()` 丢参数顺序（704–712 行）：`reasoning_effort` → `reasoning` → `tool_choice`，与 Responses 相反——Chat 端点常见的问题是"不认 reasoning 字段"，而 `tool_choice` 是工具调用必需，放最后。

`_stream()`（714–728 行）额外加 `stream_options={"include_usage": True}`（717 行）。**没有它，Chat 流的最后一个 chunk 不带 usage**，计量表就永远显示 0。这一项也在丢参数列表末尾（721 行）——拿不到 usage 比拿不到流式更可接受。

## 12. 错误归类：参数错误 vs 流式不支持 vs 瞬时错误（733–831 行）

三个判定函数各有明确用途：

**`_is_param_error`**（733–744 行）——决定"丢参数重试"而非"整次重试"：

```python
if is_transient_error(exc): return False      # 瞬时错误不是参数问题
status = _status_code(exc)
if status == 400: return True
if status is not None: return False           # 其他明确状态码都不是
text = str(exc).lower()
return any(token in text for token in ("unknown", "invalid", "unrecognized", "include", "reasoning"))
```

先排除瞬时错误（735–736 行），再看状态码，最后退回**文本关键字匹配**（742–744 行）。文本兜底的理由：有些代理报 400 但异常对象是自定义类型，抠不出 `status_code`。关键字里带 `include` 和 `reasoning` 是因为这两个正是本模块会额外加的扩展参数。

**`_is_stream_unsupported`**（746–753 行）——同样的三段式，关键字是 `"stream not supported"` / `"streaming is not"` / `"stream=true"`。命中则 `_once` 降级到非流式。

**`is_transient_error`**（797–810 行）——决定"值不值得重试"：

```python
if status in {408, 409, 425, 429, 500, 502, 503, 504}: return True
if status is not None and 400 <= status < 500: return False
```

docstring 点明原则：**4xx 中只有 408/409/425/429 重试，其余 4xx 是永久性错误**。401（key 错）、404（模型不存在）重试一百次也是错。

`TRANSIENT_MARKERS`（755–780 行）是 25 个关键字，覆盖 429/5xx 状态码、网关错误页措辞、超时、断连。其中一个特殊分支在 806–807 行：

```python
if "<html" in text and any(code in text for code in ("502", "503", "504")):
    return True
```

**网关返回 HTML 错误页但状态码拿不到时**，从 HTML 里找状态码数字。这是真实场景：nginx 反代的 502 页面。

`_status_code`（782–795 行）三级提取：异常属性（`status_code` / `status` / `http_status`）→ `exc.response.status_code` → 正则从异常文本里抠 `429|500|502|503|504`。

`brief_api_error`（812–831 行）把异常压成一行给 UI 用：HTML 页面抠 `<title>`（820–825 行），否则取首行、压空白、截断到 180 字符（827–830 行）。截断是必要的——SDK 异常常常带完整 request body，直接打印会刷屏。

## 13. `_to_api_error` 与 `_await_abortable`（833–876 行）

`_to_api_error`（833–841 行）归一成 [types.py](types.md) 的 `APIError`，消息格式 `provider/model: brief`，带 `transient` 和 `status`。前者让 UI 能提示"可以直接重发"，后者让 [loop.py](../../../core/loop.py) 把它记进 `error` 事件（含 `transient` / `status` 字段）。

`_await_abortable`（843–876 行）解决一个具体问题：**卡死的 HTTP 请求无法用轮询打断**。

```python
thread = threading.Thread(target=run, name="wheel-http", daemon=True)
thread.start()
while thread.is_alive():
    if abort.is_set():
        if cancel: cancel()
        raise KeyboardInterrupt
    thread.join(0.05)
```

把请求丢进后台线程（865–866 行），主线程以 50ms 为粒度轮询 `abort`（867–871 行）。abort 一到就调 `cancel()` 关掉 HTTP client。**线程是 daemon**（865 行）：即使没被回收也不阻塞进程退出。

`abort is None` 时直接同步执行（853–854 行）——非交互场景（`--json` 一次性任务）不付线程开销。

## 14. `call_with_retry`：指数退避与可中断等待（878–924 行）

```python
tries = int(os.getenv("WHEEL_API_RETRIES") or "4")     # 默认 4 次
delay = float(os.getenv("WHEEL_API_RETRY_BASE") or "1")  # 默认 1 秒
...
if attempt >= tries or not is_transient_error(exc): raise
if on_retry: on_retry(attempt, brief_api_error(exc))
_sleep_abortable(delay * (2 ** (attempt - 1)), sleep, abort)
```

退避序列：1s、2s、4s（`delay * 2^(attempt-1)`，907 行），第四次失败后抛出。总等待上限 7 秒——**重试次数和退避基数都从环境变量读**（880–882 行），因为不同用户的网络环境差异大。

四处 abort 检查（888–889、899–900、905 行、循环外 891–892 行）：进入 attempt 前、捕获异常后、sleep 中。`on_retry` 回调（906 行）让 UI 打印"第 N 次重试：原因"，用户能看到卡在哪。

`_sleep_abortable`（912–924 行）按 0.1 秒切片轮询（919–923 行），**避免重试等待期间按 `/stop` 无响应**——4 秒退避期间用户按 Ctrl+C 应该立刻生效。

## 15. `ScriptedModel`：确定性替身（926–956 行）

```python
def complete(self, input_items, tools, instructions, on_delta=None) -> ModelResponse:
    del tools, instructions, on_delta
    self.calls.append(input_items)
    if self.index >= len(self.scripts):
        output = [assistant_text("Done.")]
    else:
        output = self.scripts[self.index]
        self.index += 1
    return ModelResponse(output=output, usage=Usage(input_tokens=1, output_tokens=1))
```

设计要点：

1. **满足 `ModelClient` Protocol 但不继承任何基类**——结构化类型的收益在这里兑现：[loop.py](../../../core/loop.py) 拿到它照常跑，不知道自己在回放。
2. `self.calls.append(input_items)`（941 行）记录每次收到的输入，测试里可断言"喂给模型的上下文长什么样"。
3. **脚本耗尽后返回 "Done."**（943–949 行）而非越界报错。回放时如果录制轮数少于实际轮数（非确定性导致多跑了一轮），宁可让循环自然结束也不要崩。
4. `usage` 固定 `input_tokens=1, output_tokens=1`（955 行）——回放不产生真实 token，给个非零值避免下游除零。

[ui/replay.py](../../../ui/replay.py) 用 `responses.jsonl` 里的录制输出构造 `scripts`，替换掉真客户端重跑，实现"不调 API、不花钱、可复现"。

## 16. item 构造函数与 `make_client` 工厂（958–981 行）

`function_call_item`（958–962 行）把 `arguments` 统一存为 **JSON 字符串**（dict 就 `json.dumps`）。这是整个归一化的基石：**两种协议、流式非流式，产出的 `function_call` item 形态完全一致**，下游 `parse_function_calls` 和 [session.py](session.md) 的持久化只需处理一种。

`assistant_text`（964–971 行）构造 `message` item，content 用 `[{"type": "output_text", "text": ...}]`——`extract_text` 认可的第一形态。

`make_client`（973–981 行）二选一：`provider.api == "chat"` 走 Chat，否则 Responses。默认 Responses（980 行）——它是 OpenAI 的新协议，功能更全（加密思考内容、prompt cache key）。base URL 以 `/chat/completions` 结尾时 [config.py](config.md) 会自动把 `api` 置为 `chat`。

## 17. `parse_function_calls` 的调用关系

**`parse_function_calls` 不在本模块**，它在 [tools/tools.py](../../../tools/tools.py)（625–649 行）。调用链是：

```
loop.py: parse_function_calls(response.output)   ← response.output 由本模块产出
  → 遍历 items，取 type == "function_call" 的
  → arguments 字符串 json.loads 成 dict
  → FunctionCall(call_id, name, arguments, raw_arguments)
```

分工很清楚：

- **本模块负责产出**：把两种协议、流式非流式的输出都转成 `{type: "function_call", call_id, name, arguments: "<JSON 字符串>"}`。
- **tools/tools.py 负责消费**：把 `arguments` 字符串解析成 dict（供工具函数用），并记录 `raw_arguments` 供审计。

关键点：`parse_function_calls` 里 **JSON 解析失败不抛异常**（639–640 行），而是把错误塞进 `args = {"_parse_error": ..., "_raw": raw}`，交给工具的 prepare 阶段统一报错。这样模型吐出畸形 JSON 时，agent 会看到一条清晰的错误消息并自行修正，而不是整个 run 崩掉。

参数解析失败之所以可能发生，正是因为本模块把 `arguments` 存成了字符串——**流式分片拼接时模型输出被截断就会产生残缺 JSON**，`_ChatAssembler` 的 `+=` 累积无法保证 JSON 完整。
