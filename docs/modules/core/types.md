# `core/types.py` 逐段讲解

> 本篇讲全项目共享的类型定义。上游是所有人（每个模块都 import 它），下游是谁都不依赖（它是依赖图的叶子）。

一句话职责：定义穿过主循环的数据形状——消息、用量、工具调用、安全裁决、运行结果。

- 行数：132 行，全项目最短的核心模块之一
- 依赖：只依赖标准库（`dataclasses`、`typing`），**不 import 项目内任何模块**
- 被谁用：
  - [core/loop.py](../../../core/loop.py)（[docs/loop-explained.md](../../loop-explained.md)）—— 主循环里流通的全是这里的类型
  - [core/model.py](model.md) —— 协议归一化产出 `ModelResponse`
  - [core/compact.py](compact.md) 与 [tools/safety.py](../tools/safety.md) —— 共用 `unique()`
  - [core/session.py](session.md)、[core/events.py](events.md) —— 序列化/反序列化 `Usage`
  - [ui/repl.py](../ui/repl.md)、[ui/app](../ui/app.md) —— 消费 `RunResult` 渲染终局

## 目录

- [1. Item 与 Decision：两个类型别名](#1-item-与-decision两个类型别名-10-14-行)
- [2. Usage：token 计量](#2-usagetoken-计量-17-49-行)
- [3. APIError：带重试语义的异常](#3-apierror带重试语义的异常-52-59-行)
- [4. ModelResponse：归一化的模型响应](#4-modelresponse归一化的模型响应-62-68-行)
- [5. FunctionCall：模型要求的工具调用](#5-functioncall模型要求的工具调用-71-78-行)
- [6. ToolResult：工具执行结果](#6-toolresult工具执行结果-81-92-行)
- [7. SafetyVerdict：安全裁决](#7-safetyverdict安全裁决-95-101-行)
- [8. RunResult：一次 run 的收尾产物](#8-runresult一次-run-的收尾产物-104-120-行)
- [9. unique：保序去重](#9-unique保序去重-123-131-行)

---

## 1. Item 与 Decision：两个类型别名（10–14 行）

```python
Item = dict[str, Any]
Decision = Literal["allow", "ask", "deny"]
```

**`Item` 是整个项目最重要的一次取舍**：一条对话消息用 `dict` 而不是 dataclass。

理由写在注释里——要和 OpenAI 的 item 结构互转。OpenAI 的 Responses 协议里，一条 item 可能是 `{type: "message", role, content}`、`{type: "function_call", ...}`、`{type: "reasoning", ...}`，字段随类型变。用 dict 可以按需增删字段、直接 `json.dumps` 落盘、原样回传 API，不需要为每种 item 建类再写转换器。

代价是**没有类型检查**——`item["content"]` 拼错了要到运行时才发现。项目接受了这一点，换来的是序列化层几乎不存在。

`Decision` 用 `Literal` 而不是 `Enum`：值要直接进 JSON 事件流和日志，`"allow"` 比 `Decision.ALLOW.value` 省事，读日志时也是人眼可读的字符串。

---

## 2. Usage：token 计量（17–49 行）

五个维度：

| 字段 | 含义 |
|---|---|
| `input_tokens` | 输入 token |
| `output_tokens` | 输出 token |
| `cached_tokens` | **命中**前缀缓存的输入 token（省钱的部分） |
| `cache_write_tokens` | **写入**前缀缓存的 token（多花钱的部分） |
| `reasoning_tokens` | 推理模型的思考 token |

三个方法的取舍：

- **`add(other)`**：整个 run 的总量 = 逐次调用求和。主循环里 `usage.add(response.usage)` 每轮调一次，收尾时再 `add` 紧凑那次调用的用量。
- **`as_dict()`**：直接 `asdict(self)`，不手写字段清单。手写清单在加字段时会和 dataclass 脱节——这是个防呆设计。
- **`from_dict(data)`**：从会话 meta / 事件 JSON 恢复，每个字段都 `int(data.get(x) or 0)`。`or 0` 是为了同时吃掉 `None` 和缺字段两种情况，旧版本的会话文件没有后加的字段也能正常读。

---

## 3. APIError：带重试语义的异常（52–59 行）

```python
class APIError(RuntimeError):
    def __init__(self, message, *, transient=False, status=None):
```

两个附加字段决定了上层怎么处理：

- **`transient=True`**：4xx/5xx 临时故障。语义是「会话保留已完成回合，用户可直接重发」——不需要回滚什么。
- **`status`**：HTTP 状态码，供 UI 显示和判定是否值得重试。

主循环里 `APIError` 是单独一个 `except` 分支（见 [loop-explained.md 第 12 节](../../loop-explained.md)），转成 `stop_reason="api_error"`，而不是和兜底异常混在一起——因为它是**可预期的**失败，用户能看懂发生了什么。

---

## 4. ModelResponse：归一化的模型响应（62–68 行）

```python
@dataclass
class ModelResponse:
    output: list[Item]
    usage: Usage = field(default_factory=Usage)
    raw_id: str = ""
```

这是 [core/model.py](model.md) 的核心契约：**Responses 和 Chat Completions 两套协议，上层只看到这一种结构。**

- `output`：统一的 item 列表（可能是文本、思考、工具调用的混合）。
- `raw_id`：provider 返回的响应 ID，进事件流供追溯。
- `usage`：默认空 `Usage`——调用失败或流式中断时也不会 `AttributeError`。

上层（loop.py）从不关心底层走的是哪个协议，只管 `response.output` 和 `response.usage`。协议差异全被封在 model.py 里。

---

## 5. FunctionCall：模型要求的工具调用（71–78 行）

四个字段，注意 `raw_arguments`：

```python
call_id: str
name: str
arguments: dict[str, Any]
raw_arguments: str = ""      # 保留原始 JSON 串
```

**为什么要同时存解析后的 dict 和原始字符串？** 审计和 replay 要用到。解析后的 dict 给执行用；原始串供哈希（`args_sha256`，见 [tools/audit.py](../tools/audit.md)）——模型每次生成的 JSON 字符串可能有空格差异，但解析后是同一个 dict。存原始串才能算出「这次调用和上次是不是字面上一样」。

`call_id` 是配对的钥匙：调工具用它，结果回灌上下文也用它（`function_call_output.call_id`），模型靠它知道哪个结果对应哪个调用。

---

## 6. ToolResult：工具执行结果（81–92 行）

八个字段，重点是三个 `safety_*`：

```python
output: str
is_error: bool = False
blocked: bool = False
safety_decision: str = ""
safety_reason: str = ""
safety_source: str = ""
```

**工具的错误是返回值，不是异常。** `is_error=True` 的结果照样进上下文、照样喂给模型——模型看到报错可以自己换个参数重试。只有真正的程序异常才抛。这是 agent 设计里的关键选择：工具层不做错误处理决策，把「怎么办」留给模型。

`blocked` 和 `is_error` 分开：

- `is_error`：工具跑了，但失败了（文件不存在、命令返回非零）。
- `blocked`：安全门拦下来了，**根本没跑**。

UI 对两者的呈现不同（blocked 要标红并提示"这条被安全策略拦了"），模型看到也要区分——「权限不够」和「命令写错了」是完全不同的后续动作。

`SafetyVerdict` 的三个字段被原样带回进 `ToolResult`，最终进事件流（见 [loop-explained.md 第 18 节](../../loop-explained.md)）。裁决信息一路带到最外层，replay 时才能对比「两次运行的安全裁决是否一致」。

---

## 7. SafetyVerdict：安全裁决（95–101 行）

```python
decision: Decision        # allow / ask / deny
reason: str
source: str = "rules"     # 规则 / 记忆 / 用户
```

`source` 三种取值讲清了裁决的**来源**，用户在被弹窗询问时能知道「为什么问这个」：

- `rules`：命中规则表（比如 `rm -rf`）。
- `memory`：记忆里有过类似的批准——但这次不完全一样，还是要问。
- `user`：用户当场回答的。

判定逻辑在 [tools/safety.py](../tools/safety.md)，这里只是数据形状。

---

## 8. RunResult：一次 run 的收尾产物（104–120 行）

`run_agent` 的返回值，也是 CLI `--json` 模式的输出源。字段分组：

**身份与结果**：`run_id`、`task_id`、`text`（最后一段模型文本）、`turns`、`stop_reason`。

**计量**：`usage`（整个 run 累计）、`last_usage`（最后一次调用的用量——收尾紧凑时用它当输入量参考）、`changed_files`。

**续接与追溯**：`items`（完整上下文，传给下一次 `run_agent` 就能接着聊）、`events_path`（事件文件路径）、`tool_results`（全部工具结果，供 UI 统计和展示）。

**replay 专用**：`replay_status`、`replay_details`——只在 replay 模式下被填，正常 run 是空值。把 replay 的字段放在 `RunResult` 里而不是单独返回，是因为 [ui/replay.py](../ui/replay.md) 的重跑流程和正常 run 走**完全相同的代码路径**，返回值结构必须一致。

---

## 9. unique：保序去重（123–131 行）

```python
def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen: continue
        seen.add(value); out.append(value)
    return out
```

不用 `list(set(values))`，因为**集合会打乱顺序**。两个调用方都依赖首次出现的顺序：

- [core/compact.py](compact.md)：合并多轮的文件清单，顺序决定了模型看到的上下文顺序——顺序一变，前缀缓存全失效。
- [tools/safety.py](../tools/safety.md)：路径列表去重后展示给用户。

七行手写循环，换一个确定性的顺序。
