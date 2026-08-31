# `core/config.py` 逐段讲解

> 本篇讲配置加载。上游是 CLI 入口（ui/app），下游是 [core/loop.py](../../loop-explained.md) 和 [core/model.py](model.md)。

一句话职责：从 `.env` / 环境变量发现 provider，组装出 `AgentConfig`（默认 provider + 全部 provider 表 + 运行参数）。

- 行数：188 行
- 依赖：[core/meter.py](meter.md)（`infer_model_profile` 查价格表）、[core/reasoning.py](reasoning.md)（`infer_effort_levels` / `normalize` / `parse_levels`）、`python-dotenv`
- 被谁用：
  - [ui/app/__init__.py](../ui/app.md) —— 启动时 `load_config()`
  - [ui/replay.py](../ui/replay.md) —— 直接构造 `AgentConfig` / `ProviderConfig`
  - [tools/safety.py](../tools/safety.md)、[core/loop.py](../../loop-explained.md) —— 用 `config.interactive` / `config.runs_dir` / `config.effort`

## 目录

- [1. `ProviderConfig`：单个 provider 的完整配置](#1-providerconfig单个-provider-的完整配置-18-34-行)
- [2. `provider_ready`：可用性判断](#2-provider_ready可用性判断-37-40-行)
- [3. `AgentConfig`：运行配置](#3-agentconfig运行配置-43-66-行)
- [4. `_env` 与 `_discover_provider_names`](#4-_env-与-_discover_provider_names-69-100-行)
- [5. `normalize_api` 与 `strip_endpoint_path`](#5-normalize_api-与-strip_endpoint_path-103-124-行)
- [6. `_load_provider`：按前缀读一个 provider](#6-_load_provider按前缀读一个-provider-127-152-行)
- [7. `load_config`：入口](#7-load_config入口-155-188-行)

---

## 1. `ProviderConfig`：单个 provider 的完整配置（18–34 行）

```python
@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str
    effort_levels: tuple[str, ...] = ()
    context_window: int = 128_000
    input_price: float = 0.0
    output_price: float = 0.0
    cache_read_price: float = 0.0
    cache_write_price: float = 0.0
    api: str = "responses"  # responses | chat
```

`frozen=True` 是关键：运行中途配置不可变，**切换 provider 靠 `dataclasses.replace` 复制一份**（见 `AgentConfig.with_provider`）。可变配置在多线程 REPL 里是事故源——主线程读配置、工作线程也在读，复制语义比加锁简单且正确。

字段分四组：

- **连接**：`api_key` / `base_url` / `model`。
- **能力**：`effort_levels`（支持的推理档位元组）、`context_window`（默认 128k）。
- **价格**：四个维度——输入、输出、缓存读、缓存写。默认全 0（算不出钱就不算，见 [core/meter.py](meter.md)）。
- **协议**：`api` —— `responses`（OpenAI Responses API）或 `chat`（Chat Completions）。两者工具调用、推理、用量字段的布局不同，[core/model.py](model.md) 的客户端按这个字段分发到两套解析逻辑。

## 2. `provider_ready`：可用性判断（37–40 行）

```python
return bool(provider.api_key) or "localhost" in (provider.base_url or "")
```

provider 可用 = 有 key，**或** base_url 里含 `localhost`（Ollama、LM Studio、本地 mock 这类免鉴权端点）。

注释里写明「五个调用点共用这个判断，避免标准漂移」——散落在 REPL 提示、`/provider` 切换、`--json` 启动检查、模型构造等处的可用性判断，如果各处自己写一个 `if provider.api_key`，将来加免鉴权规则就会漏改。集中成一个函数就是防这个。

## 3. `AgentConfig`：运行配置（43–66 行）

```python
@dataclass
class AgentConfig:
    provider: ProviderConfig
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    max_turns: int = 0
    runs_dir: Path = field(default_factory=lambda: Path(".wheel_runs"))
    interactive: bool = True
    effort: str = "medium"
```

和 `ProviderConfig` 不同，这个是**可变**的——但变更全走三个 `with_*` 方法，返回新对象：

| 方法 | 用途 |
|---|---|
| `with_provider(name)` | `/provider` 命令切换默认 provider；未知名抛 `KeyError`，报错信息里带上已知名单 |
| `with_effort(level)` | `/effort` 命令；经 `normalize()` 归一（`"high"`/`"high effort"` 都收） |
| `with_max_turns(n)` | 轮次上限 |

`interactive` 一路传到 [tools/safety.py](../tools/safety.md) 的 `SafetyGate`（非交互模式下 `ask` 直接拒绝，不弹窗）和 [core/session.py](session.md) 的 `PlanStore`。

## 4. `_env` 与 `_discover_provider_names`（69–100 行）

`_env`（69–72 行）：读环境变量并 `strip()`，未设置返回 default。注意 `value.strip()`——`.env` 里 `KEY=sk-xxx ` 尾部空格不会进配置。

`_discover_provider_names`（75–100 行）的三级发现策略：

1. **`PROVIDERS` 显式名单**（逗号分隔，如 `PROVIDERS=openai,anthropic`）——最高优先级，完全跳过扫描。
2. **扫环境变量**：所有 `*_API_KEY` 和 `*_BASE_URL`，前缀去掉后缀小写化。一个 provider 只要有任意一个变量就算存在（有 URL 没 KEY 也进来，`provider_ready` 后面会判它不可用）。
3. **兜底**：都没有时回退 `["openai"]`——保证 `AgentConfig.providers` 永不为空，`DEFAULT_PROVIDER` 永远有指向。

末尾 `seen` / `ordered` 保序去重：`OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 会推出同一个前缀两次，去重后只算一个 provider。**顺序保留**，因为 `names[0]` 就是默认 provider 的兜底值——用户 `.env` 里写变量的顺序决定了谁排第一。

## 5. `normalize_api` 与 `strip_endpoint_path`（103–124 行）

`normalize_api`（103–114 行）把 `<前缀>_API` 的各种写法归一成 `chat` | `responses`：

- 显式值：`chat` / `chat_completions` / `completions` / `v1_chat_completions` → `chat`；`responses` / `response` → `responses`。
- 未指定时**按 base_url 后缀猜**：以 `/chat/completions` 结尾 → `chat`。
- 都不匹配 → 缺省 `responses`。

`strip_endpoint_path`（117–124 行）：剥掉 base_url 尾部的端点路径（`/chat/completions`、`/completions`、`/responses`）。因为 SDK（`openai` 包）自己在 base_url 后补路径，用户填完整端点 URL 会导致 `/v1/chat/completions/chat/completions` 这种双路径。

这两个函数一起解决「用户把完整端点 URL 填进 `BASE_URL`」这一最常见的配置错误——既识别出协议，又把路径修掉。

## 6. `_load_provider`：按前缀读一个 provider（127–152 行）

每个字段的取值优先级（`or` 链，从具体到通用）：

| 字段 | 优先级 |
|---|---|
| `api_key` | `<前缀>_API_KEY` → `OPENAI_API_KEY` |
| `base_url` | `<前缀>_BASE_URL` → `https://api.openai.com/v1` |
| `api` | `<前缀>_API` → `<前缀>_API_FORMAT` → `WHEEL_API` → `API_FORMAT`（再走 `normalize_api` 猜） |
| `model` | `<前缀>_MODEL` → `MODEL` → `gpt-4.1-mini` |
| `effort_levels` | `<前缀>_REASONING_LEVELS` / `_EFFORT_LEVELS` → 按模型名 `infer_effort_levels` 猜 |
| `context_window` / 四个价格 | `<前缀>_CONTEXT_WINDOW` / `_INPUT_PRICE` 等 → 按模型名 `infer_model_profile` 查表 |

两个通用回退值得注意：

- **api_key 回退到 `OPENAI_API_KEY`**：用户只配了一个 key 就想跑多个兼容端点时，不用重复写 key。
- **窗口和价格按模型名查表**（[core/meter.py](meter.md) 的 `infer_model_profile`）：没配也尽量给对——算成本和判断溢出都需要这些数，查不到才兜 0 / 128k。

推理档位同理走 [core/reasoning.py](reasoning.md) 的 `infer_effort_levels(model)`：按模型名猜它支持哪几档（比如 `gpt-4.1` 系列没有推理档位，返回空元组）。

## 7. `load_config`：入口（155–188 行）

```python
if env_file:
    load_dotenv(env_file, override=False)
else:
    here = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(here, override=False)
    load_dotenv(override=False)
```

**`override=False` 是安全设计**：`.env` 里的值永远不覆盖已存在的环境变量。shell 里 `export OPENAI_API_KEY=...` 临时切 key，不会被子目录 `.env` 里的旧值盖掉。不传 `env_file` 时找两处：包根目录的 `.env`（`Path(__file__).parent.parent`）和**当前工作目录**的 `.env`（`load_dotenv()` 无参，按 python-dotenv 的查找规则走）——工作区级配置优先于仓库级，但都让位于 shell 环境变量。

```python
names = _discover_provider_names()
providers = {name: _load_provider(name) for name in names}
default = _env("DEFAULT_PROVIDER", names[0]).lower()
if default not in providers:
    providers[default] = _load_provider(default)
```

`DEFAULT_PROVIDER` 指定了但不在发现名单里时，**现场加载**而不是报错——用户 `DEFAULT_PROVIDER=anthropic` 但 `ANTHROPIC_*` 变量写在 shell 里没进 `.env` 的情况也能跑（前提是 `provider_ready` 能过，调用方会查）。

两个值得注意的默认：

```python
runs_dir = Path(_env("WHEEL_RUNS_DIR") or ".wheel_runs")
max_turns = 0
```

- `runs_dir` 缺省 `.wheel_runs`——相对路径，落在**工作区**里，replay 时跟着工作区走。
- `max_turns = 0` 表示不限轮次。注释写明「REPL 和 --json 都不限轮次；评测脚本自己读 `MAX_TURNS`」——轮次限制是评测方的事，不是 agent 的事。agent 自己靠「模型不再调工具」收场。

`effort` 的取值链：`<默认 provider 前缀>_REASONING_EFFORT` → `REASONING_EFFORT` → `EFFORT` → `"medium"`，经 `normalize()` 归一后存进配置。注意它取的是**默认 provider 的前缀**——每个 provider 可以有自己的推理档位偏好。
