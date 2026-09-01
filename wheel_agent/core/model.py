"""模型客户端：OpenAI Responses 与 Chat Completions 两种协议的适配，
统一成同一份 output item 列表；另有重试、可中断等待、流式增量、
用量解析、错误归类（瞬时/参数/其他）。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from wheel_agent.core.config import ProviderConfig
from wheel_agent.core.reasoning import reasoning_payload
from wheel_agent.core.types import APIError, Item, ModelResponse, Usage

# 流式增量回调：（"text"|"thinking", 片段）。
DeltaFn = Callable[[str, str], None]

# Responses 流里文本/思考增量的事件类型（各家代理命名略有差异，列全集）。
TEXT_DELTA_TYPES = {"response.output_text.delta", "response.text.delta"}
THINKING_DELTA_TYPES = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.thinking.delta",
    "response.reasoning.delta",
}


class ModelClient(Protocol):
    """客户端抽象：两种协议 + 录制脚本（replay）都实现这一个 complete()。"""

    def complete(
        self,
        input_items: list[Item],
        tools: list[dict[str, Any]],
        instructions: str,
        on_delta: DeltaFn | None = None,
    ) -> ModelResponse: ...


def item_to_dict(item: Any) -> Item:
    """把 SDK 对象/dict 统一转成 dict（不同 SDK 版本返回类型不一样）。"""
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    if hasattr(item, "to_dict"):
        return item.to_dict()
    raise TypeError(f"cannot serialize model item: {type(item)!r}")


def extract_thinking(output: list[Item]) -> str:
    """从响应 items 里把所有思考/推理文本按序拼出来（UI 展示用）。"""
    chunks: list[str] = []
    for item in output:
        kind = item.get("type")
        if kind in {"reasoning", "thinking"}:
            # 思考文本可能在 thinking 字段、summary 列表、或 content 的
            # reasoning_text/summary_text 分片里，三种都收。
            if item.get("thinking"):
                chunks.append(str(item["thinking"]))
            summary = item.get("summary") or []
            if isinstance(summary, str) and summary.strip():
                chunks.append(summary)
            elif isinstance(summary, list):
                for part in summary:
                    if isinstance(part, dict):
                        text = part.get("text") or part.get("summary_text") or ""
                        if text:
                            chunks.append(str(text))
                    elif isinstance(part, str) and part.strip():
                        chunks.append(part)
            content = item.get("content") or []
            if isinstance(content, str) and content.strip():
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {
                        "reasoning_text",
                        "summary_text",
                        "thinking",
                        "text",
                    }:
                        text = part.get("text") or ""
                        if text:
                            chunks.append(str(text))
                    elif isinstance(part, str) and part.strip():
                        chunks.append(part)
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {"reasoning_text", "thinking"}:
                    text = part.get("text") or ""
                    if text:
                        chunks.append(str(text))
    return "\n".join(chunk.strip() for chunk in chunks if str(chunk).strip())


def extract_text(output: list[Item]) -> str:
    """从响应 items 里拼出模型可见文本（不含思考）。"""
    chunks: list[str] = []
    for item in output:
        if item.get("type") == "message":
            content = item.get("content") or []
            if isinstance(content, str):
                chunks.append(content)
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    chunks.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    chunks.append(part)
        elif item.get("type") == "output_text" and item.get("text"):
            chunks.append(str(item["text"]))
    return "".join(chunks).strip()


def _nested(obj: Any, *names: str) -> Any:
    """按路径逐层取 dict/对象嵌套字段，中途断了返回 None。"""
    cur = obj
    for name in names:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(name)
        else:
            cur = getattr(cur, name, None)
    return cur


def _fget(obj: Any, name: str, default: Any = None) -> Any:
    """dict 或对象都能读的字段访问（openai 各版本 SDK 返回类型不同）。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(value: Any) -> int:
    """容错转 int：None/非数字一律当 0。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _looks_like_usage(blob: Any) -> bool:
    """看一个 blob 是不是用量对象（有些响应把 usage 放顶层）。"""
    if isinstance(blob, dict):
        return any(
            key in blob
            for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens")
        )
    return any(
        getattr(blob, name, None) is not None
        for name in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens")
    )


def usage_from_response(response: Any) -> Usage:
    """把两种协议的响应统一成 Usage。

    兼容字段名差异：Responses 用 input_tokens，Chat 用 prompt_tokens；
    缓存命中在 details.cached_tokens 或 cache_read_input_tokens 里。"""
    if response is None:
        return Usage()
    usage = _fget(response, "usage")
    if usage is None and _looks_like_usage(response):
        usage = response
    if usage is None:
        return Usage()
    details = _fget(usage, "input_tokens_details") or _fget(usage, "prompt_tokens_details") or {}
    return Usage(
        input_tokens=_int(_fget(usage, "input_tokens") or _fget(usage, "prompt_tokens")),
        output_tokens=_int(_fget(usage, "output_tokens") or _fget(usage, "completion_tokens")),
        cached_tokens=_int(
            _nested(details, "cached_tokens")
            or _fget(usage, "cache_read_input_tokens")
            or _fget(usage, "cached_tokens")
        ),
        cache_write_tokens=_int(_fget(usage, "cache_creation_input_tokens") or _fget(usage, "cache_write_tokens")),
        reasoning_tokens=_int(_nested(_fget(usage, "output_tokens_details") or {}, "reasoning_tokens")),
    )


def event_type(event: Any) -> str:
    """流事件类型（dict/对象都能读）。"""
    return str(_fget(event, "type", "") or "")


def event_delta(event: Any) -> str:
    """取流事件里的增量文本；delta 可能是 str 也可能是 {text: …} dict。"""
    raw = _fget(event, "delta")
    if raw is None:
        raw = _fget(event, "text")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("text") or raw.get("delta") or "")
    return str(raw) if raw else ""


def event_response(event: Any) -> Any:
    """completed 事件里的最终响应对象。"""
    return _fget(event, "response")


def consume_stream_event(event: Any, on_delta: DeltaFn | None) -> Any | None:
    """逐个消费流事件：增量文本/思考喂给 on_delta；completed 时返回最终响应。"""
    kind = event_type(event)
    if on_delta and kind in TEXT_DELTA_TYPES:
        chunk = event_delta(event)
        if chunk:
            on_delta("text", chunk)
    elif on_delta and kind in THINKING_DELTA_TYPES:
        chunk = event_delta(event)
        if chunk:
            on_delta("thinking", chunk)
    elif on_delta and kind == "response.output_item.added":
        item = event.get("item") if isinstance(event, dict) else getattr(event, "item", None)
        typ = ""
        if isinstance(item, dict):
            typ = str(item.get("type") or "")
        elif item is not None:
            typ = str(getattr(item, "type", "") or "")
        if typ in {"reasoning", "thinking"}:
            on_delta("thinking", "")   # 空字符串作“进入思考块”信号，UI 切换区块样式
    if kind == "response.completed":
        return event_response(event)
    return None


def item_text(item: Item) -> str:
    """取一条消息的文本内容（content 可能是 str 或分片列表）。"""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") in {"output_text", "text", "input_text"} or part.get("text"):
                    parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def tools_to_chat(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Responses 风格的工具声明转成 Chat Completions 的 function 包装。"""
    converted: list[dict[str, Any]] = []
    for spec in tools:
        if spec.get("type") == "function" and isinstance(spec.get("function"), dict):
            converted.append(spec)
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": str(spec.get("name") or ""),
                    "description": str(spec.get("description") or ""),
                    "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
                },
            }
        )
    return converted


def items_to_chat_messages(items: list[Item], instructions: str = "") -> list[dict[str, Any]]:
    """统一 item 列表转成 Chat 的 messages。

    难点：Responses 里一次模型输出是“message + N 个 function_call”的并列
    items，Chat 要求合并进一条 assistant 消息的 tool_calls 字段。"""
    messages: list[dict[str, Any]] = []
    if instructions.strip():
        messages.append({"role": "system", "content": instructions})
    index = 0
    while index < len(items):
        item = items[index]
        if not isinstance(item, dict):
            index += 1
            continue
        kind = item.get("type")
        role = item.get("role")
        if kind == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or item.get("id") or ""),
                    "content": str(item.get("output") or ""),
                }
            )
            index += 1
            continue
        if role == "user":
            messages.append({"role": "user", "content": item_text(item)})
            index += 1
            continue
        if role == "system":
            messages.append({"role": "system", "content": item_text(item)})
            index += 1
            continue
        if kind in {"reasoning", "thinking"}:
            index += 1
            continue
        if kind in {"message", "function_call"} or role == "assistant":
            texts: list[str] = []
            calls: list[dict[str, Any]] = []
            # 把连续同属一次输出的 message + function_call 扫成一个组。
            while index < len(items):
                cur = items[index]
                if not isinstance(cur, dict):
                    break
                cur_kind = cur.get("type")
                cur_role = cur.get("role")
                if cur_kind in {"reasoning", "thinking"}:
                    index += 1
                    continue
                if cur_kind == "function_call":
                    raw = cur.get("arguments") or "{}"
                    if not isinstance(raw, str):
                        raw = json.dumps(raw, ensure_ascii=False)
                    calls.append(
                        {
                            "id": str(cur.get("call_id") or cur.get("id") or f"call_{len(calls)}"),
                            "type": "function",
                            "function": {
                                "name": str(cur.get("name") or ""),
                                "arguments": raw,
                            },
                        }
                    )
                    index += 1
                    continue
                if cur_kind == "message" or cur_role == "assistant":
                    text = item_text(cur)
                    if text:
                        texts.append(text)
                    index += 1
                    continue
                break
            body = "\n".join(texts)
            message: dict[str, Any] = {"role": "assistant", "content": body or None}
            if calls:
                message["tool_calls"] = calls
            if message["content"] or calls:
                if not message["content"] and calls:
                    message["content"] = None
                messages.append(message)
            continue
        index += 1
    return messages


def _delta_str(value: Any) -> str:
    return "" if value is None else str(value)


def _call_parts(call: Any, default_id: str, default_args: str) -> tuple[str, str, Any]:
    if isinstance(call, dict):
        fn = call.get("function") or {}
        call_id = str(call.get("id") or default_id)
        name = _delta_str(fn.get("name"))
        arguments = fn.get("arguments") or default_args
    else:
        fn = getattr(call, "function", None)
        call_id = str(getattr(call, "id", None) or default_id)
        name = _delta_str(getattr(fn, "name", None) if fn is not None else None)
        arguments = getattr(fn, "arguments", None) if fn is not None else None
        if arguments is None:
            arguments = default_args
    return call_id, name, arguments


def chat_message_to_output(message: Any) -> list[Item]:
    output: list[Item] = []
    if isinstance(message, dict):
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        content = message.get("content")
        tool_calls = message.get("tool_calls") or []
    else:
        reasoning = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)
        content = getattr(message, "content", None)
        tool_calls = getattr(message, "tool_calls", None) or []
    if isinstance(reasoning, dict):
        reasoning = reasoning.get("text") or reasoning.get("content")
    if isinstance(reasoning, str) and reasoning.strip():
        output.append(
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    else:
        text = item_text({"content": content})
    if text:
        output.append(assistant_text(text))
    for index, call in enumerate(tool_calls):
        call_id, name, arguments = _call_parts(call, f"call_{index}", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        output.append(function_call_item(call_id, name, arguments))
    return output


class _ChatAssembler:
    def __init__(self) -> None:
        self.content = ""
        self.reasoning = ""
        self.tools: dict[int, dict[str, str]] = {}
        self.usage: Any = None
        self.id = ""

    def feed(self, chunk: Any, on_delta: DeltaFn | None) -> None:
        if isinstance(chunk, dict):
            if chunk.get("id"):
                self.id = str(chunk["id"])
            if chunk.get("usage") is not None:
                self.usage = chunk["usage"]
            choices = chunk.get("choices") or []
            delta = (choices[0] or {}).get("delta") if choices else None
            if not isinstance(delta, dict):
                return
            self._apply_delta(
                content=delta.get("content"),
                reasoning=delta.get("reasoning_content") or delta.get("reasoning"),
                tool_calls=delta.get("tool_calls") or [],
                on_delta=on_delta,
            )
            return
        if getattr(chunk, "id", None):
            self.id = str(chunk.id)
        if getattr(chunk, "usage", None) is not None:
            self.usage = chunk.usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            return
        self._apply_delta(
            content=getattr(delta, "content", None),
            reasoning=getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None),
            tool_calls=getattr(delta, "tool_calls", None) or [],
            on_delta=on_delta,
        )

    def _apply_delta(self, *, content: Any, reasoning: Any, tool_calls: Any, on_delta: DeltaFn | None) -> None:
        if isinstance(reasoning, dict):
            reasoning = reasoning.get("text") or reasoning.get("content")
        if isinstance(reasoning, str) and reasoning:
            self.reasoning += reasoning
            if on_delta:
                on_delta("thinking", reasoning)
        if isinstance(content, str) and content:
            self.content += content
            if on_delta:
                on_delta("text", content)
        for call in tool_calls or []:
            index = int((call.get("index") if isinstance(call, dict) else getattr(call, "index", 0)) or 0)
            call_id, name, arguments = _call_parts(call, "", "")
            arguments = arguments or ""
            slot = self.tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if call_id:
                slot["id"] = call_id
            if name:
                slot["name"] = name
            if arguments:
                slot["arguments"] += str(arguments)

    def output(self) -> list[Item]:
        fake = {
            "reasoning_content": self.reasoning or None,
            "content": self.content or None,
            "tool_calls": [
                {
                    "id": self.tools[index]["id"] or f"call_{index}",
                    "function": {
                        "name": self.tools[index]["name"],
                        "arguments": self.tools[index]["arguments"] or "{}",
                    },
                }
                for index in sorted(self.tools)
            ],
        }
        return chat_message_to_output(fake)


class _OpenAIBase:
    """Shared by both API clients: client setup, cancel, the retry/abort
    wrapper, and drop-one-unsupported-param retries on 400s (proxies vary)."""

    def __init__(self, provider: ProviderConfig, effort: str = "medium", cache_key: str | None = None):
        from openai import OpenAI

        self.provider = provider
        self.effort = effort
        self.cache_key = cache_key
        self.abort = None
        self.on_retry = None
        self._stream_obj: Any = None
        timeout = float(os.getenv("WHEEL_TIMEOUT") or "180")
        self.client = OpenAI(
            api_key=provider.api_key or "sk-none",
            base_url=provider.base_url,
            timeout=timeout,
            max_retries=0,
        )

    def cancel(self) -> None:
        # Closing the HTTP client on purpose, not just the stream: it is the
        # only reliable way to abort a *stuck* non-streaming request (the
        # socket close propagates to the request thread). Safe because a
        # client instance is per-task and rebuilt after a cancel.
        stream = self._stream_obj
        self._stream_obj = None
        if stream is not None:
            for closer in (getattr(stream, "close", None), getattr(getattr(stream, "response", None), "close", None)):
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass
        closer = getattr(getattr(self, "client", None), "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    def _call(self, once: Callable[[], Any]) -> Any:
        try:
            return _await_abortable(
                lambda: call_with_retry(
                    once,
                    on_retry=getattr(self, "on_retry", None),
                    abort=getattr(self, "abort", None),
                ),
                getattr(self, "abort", None),
                cancel=self.cancel,
            )
        except KeyboardInterrupt:
            raise
        except APIError:
            raise
        except Exception as exc:
            raise _to_api_error(self.provider, exc) from exc

    def _once(self, kwargs: dict[str, Any], on_delta: DeltaFn | None) -> Any:
        if on_delta:
            try:
                return self._stream(kwargs, on_delta)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                flag = getattr(self, "abort", None)
                if flag is not None and flag.is_set():
                    raise KeyboardInterrupt from exc
                if not (_is_param_error(exc) or _is_stream_unsupported(exc)):
                    raise
                return self._create(kwargs)
        return self._create(kwargs)

    def _drop_create(self, fn: Callable[..., Any], drop_order: tuple[str, ...], **kwargs: Any) -> Any:
        """fn(**kwargs); on a param error drop one kwarg (in drop_order) and retry."""
        pending = dict(kwargs)
        while True:
            try:
                return fn(**pending)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if not _is_param_error(exc):
                    raise
                for key in drop_order:
                    if key in pending:
                        pending.pop(key)
                        break
                else:
                    raise

    def _iter_stream(self, stream: Any, on_event: Callable[[Any], Any]) -> Any:
        """Drain a stream with abort checks; returns the last non-None event value."""
        final: Any = None
        try:
            for event in stream:
                abort = getattr(self, "abort", None)
                if abort is not None and abort.is_set():
                    raise KeyboardInterrupt
                value = on_event(event)
                if value is not None:
                    final = value
        except KeyboardInterrupt:
            self.cancel()
            raise
        finally:
            if self._stream_obj is stream:
                self._stream_obj = None
        return final


class ResponsesClient(_OpenAIBase):
    def complete(
        self,
        input_items: list[Item],
        tools: list[dict[str, Any]],
        instructions: str,
        on_delta: DeltaFn | None = None,
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self.provider.model,
            "input": input_items,
            "instructions": instructions,
            "store": False,
        }
        payload = reasoning_payload(self.effort, self.provider.effort_levels)
        if payload:
            kwargs["reasoning"] = payload
            kwargs["include"] = ["reasoning.encrypted_content"]
        if tools:
            kwargs["tools"] = tools
        if self.cache_key:
            kwargs["prompt_cache_key"] = self.cache_key
            kwargs["prompt_cache_retention"] = "24h"

        response = self._call(lambda: self._once(kwargs, on_delta))
        output = [item_to_dict(item) for item in (getattr(response, "output", None) or [])]
        return ModelResponse(
            output=output,
            usage=usage_from_response(response),
            raw_id=str(getattr(response, "id", "") or ""),
        )

    def _create(self, kwargs: dict[str, Any]) -> Any:
        """非流式请求；丢参数降级的优先级：扩展参数先丢，reasoning 最后丢。"""
        pending = dict(kwargs)
        pending.pop("stream", None)
        return self._drop_create(
            self.client.responses.create,
            ("include", "prompt_cache_retention", "prompt_cache_options", "prompt_cache_key", "reasoning"),
            **pending,
        )

    def _stream(self, kwargs: dict[str, Any], on_delta: DeltaFn) -> Any:
        pending = dict(kwargs)
        pending["stream"] = True
        stream = self.client.responses.create(**pending)
        self._stream_obj = stream
        final = self._iter_stream(stream, lambda event: consume_stream_event(event, on_delta))
        if final is None:
            getter = getattr(stream, "get_final_response", None)
            if callable(getter):
                final = getter()
        if final is None:
            raise RuntimeError("stream ended without a completed response")
        return final


class ChatCompletionsClient(_OpenAIBase):
    def complete(
        self,
        input_items: list[Item],
        tools: list[dict[str, Any]],
        instructions: str,
        on_delta: DeltaFn | None = None,
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self.provider.model,
            "messages": items_to_chat_messages(input_items, instructions),
        }
        payload = reasoning_payload(self.effort, self.provider.effort_levels)
        if payload and payload.get("effort") not in {None, "none"}:
            kwargs["reasoning_effort"] = payload["effort"]
        if tools:
            kwargs["tools"] = tools_to_chat(tools)
            kwargs["tool_choice"] = "auto"

        response = self._call(lambda: self._once(kwargs, on_delta))
        if isinstance(response, ModelResponse):
            return response
        message = None
        choices = getattr(response, "choices", None)
        if choices:
            message = getattr(choices[0], "message", None)
        elif isinstance(response, dict):
            rows = response.get("choices") or []
            message = (rows[0] or {}).get("message") if rows else None
        output = chat_message_to_output(message or {})
        return ModelResponse(
            output=output,
            usage=usage_from_response(response),
            raw_id=str(getattr(response, "id", "") or (response.get("id") if isinstance(response, dict) else "") or ""),
        )

    def _create(self, kwargs: dict[str, Any]) -> Any:
        """非流式请求；丢参数降级：reasoning 系先丢，最后丢 tool_choice。"""
        pending = dict(kwargs)
        pending.pop("stream", None)
        pending.pop("stream_options", None)
        return self._drop_create(
            self.client.chat.completions.create,
            ("reasoning_effort", "reasoning", "tool_choice"),
            **pending,
        )

    def _stream(self, kwargs: dict[str, Any], on_delta: DeltaFn) -> ModelResponse:
        pending = dict(kwargs)
        pending["stream"] = True
        pending["stream_options"] = {"include_usage": True}
        stream = self._drop_create(
            self.client.chat.completions.create,
            ("reasoning_effort", "reasoning", "tool_choice", "stream_options"),
            **pending,
        )
        self._stream_obj = stream
        assembled = _ChatAssembler()
        self._iter_stream(stream, lambda event: assembled.feed(event, on_delta))
        return ModelResponse(
            output=assembled.output(),
            usage=usage_from_response(assembled),
            raw_id=assembled.id,
        )


def _is_param_error(exc: Exception) -> bool:
    """判断是否为 400 参数错误（该丢参数重试，而非整次重试）。"""
    if is_transient_error(exc):
        return False
    status = _status_code(exc)
    if status == 400:
        return True
    if status is not None:
        return False
    text = str(exc).lower()
    return any(token in text for token in ("unknown", "invalid", "unrecognized", "include", "reasoning"))


def _is_stream_unsupported(exc: Exception) -> bool:
    """端点不支持流式时退回非流式（一些兼容代理会报这个）。"""
    if is_transient_error(exc):
        return False
    text = str(exc).lower()
    return any(token in text for token in ("stream not supported", "streaming is not", "stream=true"))


# 临时错误的关键字（429/5xx/网关错误/超时/断连），命中即重试。
TRANSIENT_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "gateway time-out",
    "gateway timeout",
    "bad gateway",
    "service unavailable",
    "too many requests",
    "rate limit",
    "overloaded",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection error",
    "connect error",
    "remote disconnected",
    "server disconnected",
    "network",
    "temporarily",
)


def _status_code(exc: BaseException) -> int | None:
    """从异常的多个可能属性/响应对象/文本里提取 HTTP 状态码。"""
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and value > 0:
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and value > 0:
            return value
    match = re.search(r"\b(429|500|502|503|504)\b", str(exc))
    return int(match.group(1)) if match else None


def is_transient_error(exc: BaseException) -> bool:
    """是否临时错误（值得重试）。

    4xx 中只有 408/409/425/429 重试；其余 4xx 是永久性错误，重试也没用。"""
    status = _status_code(exc)
    if status in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    if status is not None and 400 <= status < 500:
        return False
    text = str(exc).lower()
    if "<html" in text and any(code in text for code in ("502", "503", "504")):
        return True
    return any(marker in text for marker in TRANSIENT_MARKERS)


def brief_api_error(exc: BaseException) -> str:
    """把异常压成一行可读的简短描述（给 UI/事件用）。

    网关返回的 HTML 错误页提取 <title>；其余取首行并截断到 180 字符。"""
    status = _status_code(exc)
    raw = str(exc).strip() or exc.__class__.__name__
    if "<html" in raw.lower() or "<title>" in raw.lower():
        title = re.search(r"<title>([^<]+)</title>", raw, re.I)
        label = title.group(1).strip() if title else "gateway error"
        if status:
            return f"HTTP {status} {label}"
        return label
    line = raw.splitlines()[0].strip()
    line = re.sub(r"\s+", " ", line)
    if len(line) > 180:
        line = line[:177] + "..."
    if status and str(status) not in line:
        return f"HTTP {status}: {line}"
    return line


def _to_api_error(provider: ProviderConfig, exc: BaseException) -> APIError:
    """把底层异常归一成 APIError，带上 provider/model、是否临时、状态码。"""
    brief = brief_api_error(exc)
    return APIError(
        f"{provider.name}/{provider.model}: {brief}",
        transient=is_transient_error(exc),
        status=_status_code(exc),
    )


def _await_abortable(
    fn: Callable[[], Any],
    abort: Any | None,
    *,
    cancel: Callable[[], None] | None = None,
) -> Any:
    """在后台线程跑 fn，主线程轮询；abort 一旦置位就取消并抛 KeyboardInterrupt，
    不傻等一个卡死的 HTTP 请求跑完。"""
    if abort is None:
        return fn()
    if abort.is_set():
        raise KeyboardInterrupt
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["v"] = fn()
        except BaseException as exc:
            box["e"] = exc

    thread = threading.Thread(target=run, name="wheel-http", daemon=True)
    thread.start()
    while thread.is_alive():
        if abort.is_set():
            if cancel:
                cancel()
            raise KeyboardInterrupt
        thread.join(0.05)
    if abort.is_set():
        raise KeyboardInterrupt
    if "e" in box:
        raise box["e"]
    return box["v"]


def call_with_retry(
    fn: Callable[[], Any],
    *,
    attempts: int | None = None,
    base_delay: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, str], None] | None = None,
    abort: Any | None = None,
) -> Any:
    """指数退避重试包装：只对临时错误重试，最多 attempts 次（默认读 WHEEL_API_RETRIES）。"""
    tries = attempts if attempts is not None else int(os.getenv("WHEEL_API_RETRIES") or "4")
    tries = max(1, tries)
    delay = base_delay if base_delay is not None else float(os.getenv("WHEEL_API_RETRY_BASE") or "1")
    last: BaseException | None = None
    for attempt in range(1, tries + 1):
        if abort is not None and abort.is_set():
            raise KeyboardInterrupt
        try:
            return fn()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            last = exc
            if abort is not None and abort.is_set():
                raise KeyboardInterrupt from exc
            if attempt >= tries or not is_transient_error(exc):
                raise
            if on_retry:
                on_retry(attempt, brief_api_error(exc))
            _sleep_abortable(delay * (2 ** (attempt - 1)), sleep, abort)
    assert last is not None
    raise last


def _sleep_abortable(seconds: float, sleep: Callable[[float], None], abort: Any | None) -> None:
    """可中断的 sleep：按 0.1s 切片轮询 abort，避免重试等待期间无法 /stop。"""
    if seconds <= 0:
        return
    if abort is None:
        sleep(seconds)
        return
    end = time.time() + seconds
    while time.time() < end:
        if abort.is_set():
            raise KeyboardInterrupt
        sleep(min(0.1, end - time.time()))


class ScriptedModel:
    """确定性替身：测试、replay、离线评测用，按脚本顺序返回预设输出。"""

    def __init__(self, scripts: list[list[Item]] | None = None):
        self.scripts = list(scripts or [])
        self.index = 0
        self.calls: list[list[Item]] = []

    def complete(
        self,
        input_items: list[Item],
        tools: list[dict[str, Any]],
        instructions: str,
        on_delta: DeltaFn | None = None,
    ) -> ModelResponse:
        del tools, instructions, on_delta
        self.calls.append(input_items)
        # 脚本耗尽后返回一个“完成”消息，避免越界。
        if self.index >= len(self.scripts):
            output: list[Item] = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done."}],
                }
            ]
        else:
            output = self.scripts[self.index]
            self.index += 1
        return ModelResponse(output=output, usage=Usage(input_tokens=1, output_tokens=1))


def function_call_item(call_id: str, name: str, arguments: dict[str, Any] | str) -> Item:
    """构造一个 function_call item（参数统一存为 JSON 字符串）。"""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": raw}


def assistant_text(text: str) -> Item:
    """构造一条 assistant 文本消息 item。"""
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def make_client(
    provider: ProviderConfig,
    effort: str = "medium",
    cache_key: str | None = None,
) -> ModelClient:
    """按 provider.api 选客户端：chat → ChatCompletionsClient，否则 ResponsesClient。"""
    if provider.api == "chat":
        return ChatCompletionsClient(provider, effort=effort, cache_key=cache_key)
    return ResponsesClient(provider, effort=effort, cache_key=cache_key)
