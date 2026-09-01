"""配置加载：从 .env/环境变量发现并构建 provider 配置。

命名约定：<前缀>_API_KEY / _BASE_URL / _MODEL / _REASONING_LEVELS / 价格变量，
前缀缺省为 openai；DEFAULT_PROVIDER 选默认，每个前缀对应一个模型。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

from wheel_agent.core.meter import infer_model_profile
from wheel_agent.core.reasoning import infer_effort_levels, normalize, parse_levels


@dataclass(frozen=True)
class ProviderConfig:
    """单个 provider 的完整配置（frozen：运行中途不可变，切换靠 replace）。"""

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
    # API 协议：responses = OpenAI Responses API；chat = Chat Completions。
    # 两种协议的工具调用/推理/用量字段布局不同，客户端据此分发。
    api: str = "responses"  # responses | chat


def provider_ready(provider: ProviderConfig) -> bool:
    """provider 可用 = 有 API key，或指向免鉴权的本地端点（Ollama、
    LM Studio、本地 mock 服务器）。五个调用点共用这个判断，避免标准漂移。"""
    return bool(provider.api_key) or "localhost" in (provider.base_url or "")


@dataclass
class AgentConfig:
    """Agent 运行配置：默认 provider、全部 provider 表、轮次上限、运行目录。"""

    provider: ProviderConfig
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    max_turns: int = 0
    runs_dir: Path = field(default_factory=lambda: Path(".wheel_runs"))
    interactive: bool = True
    effort: str = "medium"

    def with_provider(self, name: str) -> "AgentConfig":
        """复制一份配置并换默认 provider（/provider 命令用）；未知名直接报错。"""
        key = name.strip().lower()
        if key not in self.providers:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise KeyError(f"unknown provider {name!r}; configured: {known}")
        return replace(self, provider=self.providers[key])

    def with_effort(self, level: str) -> "AgentConfig":
        return replace(self, effort=normalize(level))

    def with_max_turns(self, n: int) -> "AgentConfig":
        return replace(self, max_turns=int(n))


def _env(name: str, default: str = "") -> str:
    """读环境变量并去首尾空白；未设置返回 default。"""
    value = os.getenv(name)
    return default if value is None else value.strip()


def _discover_provider_names() -> list[str]:
    """发现已配置的 provider：PROVIDERS 显式名单优先，
    否则扫 *_API_KEY / *_BASE_URL；一个都没有时回退 openai。"""
    explicit = [p.strip().lower() for p in _env("PROVIDERS").split(",") if p.strip()]
    if explicit:
        return explicit
    names: list[str] = []
    for key, value in os.environ.items():
        if key.endswith("_API_KEY") and value.strip():
            names.append(key[: -len("_API_KEY")].lower())
        elif key.endswith("_BASE_URL") and value.strip():
            prefix = key[: -len("_BASE_URL")].lower()
            if prefix not in names:
                names.append(prefix)
    if "openai" not in names and (_env("OPENAI_API_KEY") or _env("OPENAI_BASE_URL")):
        names.insert(0, "openai")
    if not names:
        names = ["openai"]
    # 保序去重：.env 里同一前缀的多个变量只算一个 provider。
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def normalize_api(raw: str, base_url: str = "") -> str:
    """把 <前缀>_API 的各种写法归一成 chat|responses；未指定时按 base_url 后缀猜，
    都不匹配则缺省 responses。"""
    key = (raw or "").strip().lower().replace("-", "_")
    if key in {"chat", "chat_completions", "completions", "v1_chat_completions"}:
        return "chat"
    if key in {"responses", "response"}:
        return "responses"
    url = (base_url or "").rstrip("/").lower()
    if url.endswith("/chat/completions"):
        return "chat"
    return "responses"


def strip_endpoint_path(base_url: str) -> str:
    """剥掉 base_url 尾部的端点路径（/chat/completions 等），SDK 会自己补路径。"""
    url = (base_url or "").rstrip("/")
    lowered = url.lower()
    for suffix in ("/chat/completions", "/completions", "/responses"):
        if lowered.endswith(suffix):
            return url[: -len(suffix)]
    return url


def _load_provider(name: str) -> ProviderConfig:
    """按前缀读一个 provider 的全部变量；价格/窗口未配置时按模型名查表，
    推理档位未配置时按模型名猜。"""
    prefix = name.upper()
    api_key = _env(f"{prefix}_API_KEY") or _env("OPENAI_API_KEY")
    raw_url = _env(f"{prefix}_BASE_URL") or "https://api.openai.com/v1"
    api_raw = _env(f"{prefix}_API") or _env(f"{prefix}_API_FORMAT") or _env("WHEEL_API") or _env("API_FORMAT")
    api = normalize_api(api_raw, raw_url)
    base_url = strip_endpoint_path(raw_url)
    model = _env(f"{prefix}_MODEL") or _env("MODEL") or "gpt-4.1-mini"
    levels_raw = _env(f"{prefix}_REASONING_LEVELS") or _env(f"{prefix}_EFFORT_LEVELS")
    levels = parse_levels(levels_raw) if levels_raw else infer_effort_levels(model)
    window, in_price, out_price, cache_read, cache_write = infer_model_profile(model)
    return ProviderConfig(
        name=name,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        effort_levels=levels,
        context_window=int(_env(f"{prefix}_CONTEXT_WINDOW") or window),
        input_price=float(_env(f"{prefix}_INPUT_PRICE") or in_price),
        output_price=float(_env(f"{prefix}_OUTPUT_PRICE") or out_price),
        cache_read_price=float(_env(f"{prefix}_CACHE_READ_PRICE") or cache_read),
        cache_write_price=float(_env(f"{prefix}_CACHE_WRITE_PRICE") or cache_write),
        api=api,
    )


def load_config(env_file: str | Path | None = None, interactive: bool = True) -> AgentConfig:
    """入口：载入 .env（不覆盖已存在的环境变量），发现 provider，组装 AgentConfig。

    不传 env_file 时默认找包根目录和当前目录的 .env。"""
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        here = Path(__file__).resolve().parent.parent.parent / ".env"
        load_dotenv(here, override=False)
        load_dotenv(override=False)

    names = _discover_provider_names()
    providers = {name: _load_provider(name) for name in names}
    default = _env("DEFAULT_PROVIDER", names[0]).lower()
    if default not in providers:
        providers[default] = _load_provider(default)

    runs_dir = Path(_env("WHEEL_RUNS_DIR") or ".wheel_runs")
    # REPL 和 --json 都不限轮次；评测脚本自己读 MAX_TURNS。
    max_turns = 0
    effort = normalize(
        _env(f"{providers[default].name.upper()}_REASONING_EFFORT")
        or _env("REASONING_EFFORT")
        or _env("EFFORT")
        or "medium"
    )
    return AgentConfig(
        provider=providers[default],
        providers=providers,
        max_turns=max_turns,
        runs_dir=runs_dir,
        interactive=interactive,
        effort=effort,
    )
