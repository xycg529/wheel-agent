from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from wheel_agent.config import ProviderConfig
from wheel_agent.reasoning import reasoning_payload
from wheel_agent.types import APIError, Item, ModelResponse, Usage

DeltaFn = Callable[[str, str], None]

TEXT_DELTA_TYPES = {"response.output_text.delta", "response.text.delta"}
THINKING_DELTA_TYPES = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.thinking.delta",
    "response.reasoning.delta",
}


class ModelClient(Protocol):
    def complete(
        self,
        input_items: list[Item],
        tools: list[dict[str, Any]],
        instructions: str,
        on_delta: DeltaFn | None = None,
    ) -> ModelResponse: ...


def item_to_dict(item: Any) -> Item:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    if hasattr(item, "to_dict"):
        return item.to_dict()
    raise TypeError(f"cannot serialize model item: {type(item)!r}")


def extract_thinking(output: list[Item]) -> str:
    chunks: list[str] = []
    for item in output:
        kind = item.get("type")
        if kind in {"reasoning", "thinking"}:
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
    cur = obj
    for name in names:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(name)
        else:
            cur = getattr(cur, name, None)
    return cur


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _looks_like_usage(blob: Any) -> bool:
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
    if response is None:
        return Usage()
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None and _looks_like_usage(response):
        usage = response
    if usage is None:
        return Usage()
    if isinstance(usage, dict):
        input_tokens = _int(usage.get("input_tokens") or usage.get("prompt_tokens"))
        output_tokens = _int(usage.get("output_tokens") or usage.get("completion_tokens"))
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        cached = _int(
            (details.get("cached_tokens") if isinstance(details, dict) else None)
            or usage.get("cache_read_input_tokens")
            or usage.get("cached_tokens")
        )
        cache_write = _int(usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens"))
        out_details = usage.get("output_tokens_details") or {}
        reasoning = _int(out_details.get("reasoning_tokens") if isinstance(out_details, dict) else 0)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            cache_write_tokens=cache_write,
            reasoning_tokens=reasoning,
        )
    input_tokens = _int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0))
    output_tokens = _int(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0))
    details = getattr(usage, "input_tokens_details", None) or getattr(usage, "prompt_tokens_details", None)
    cached = _int(
        _nested(details, "cached_tokens")
        or getattr(usage, "cache_read_input_tokens", 0)
        or getattr(usage, "cached_tokens", 0)
    )
    cache_write = _int(
        getattr(usage, "cache_creation_input_tokens", 0) or getattr(usage, "cache_write_tokens", 0)
    )
    reasoning = _int(_nested(getattr(usage, "output_tokens_details", None), "reasoning_tokens"))
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
    )


def event_type(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("type") or "")
    return str(getattr(event, "type", "") or "")


def event_delta(event: Any) -> str:
    raw = event.get("delta") if isinstance(event, dict) else getattr(event, "delta", None)
    if raw is None:
        raw = event.get("text") if isinstance(event, dict) else getattr(event, "text", None)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("text") or raw.get("delta") or "")
    return str(raw) if raw else ""


def event_response(event: Any) -> Any:
    if isinstance(event, dict):
        return event.get("response")
    return getattr(event, "response", None)


def consume_stream_event(event: Any, on_delta: DeltaFn | None) -> Any | None:
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
            on_delta("thinking", "")
    if kind == "response.completed":
        return event_response(event)
    return None


def item_text(item: Item) -> str:
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
        if isinstance(call, dict):
            fn = call.get("function") or {}
            call_id = str(call.get("id") or f"call_{index}")
            name = _delta_str(fn.get("name"))
            arguments = fn.get("arguments") or "{}"
        else:
            fn = getattr(call, "function", None)
            call_id = str(getattr(call, "id", None) or f"call_{index}")
            name = _delta_str(getattr(fn, "name", None) if fn is not None else None)
            arguments = getattr(fn, "arguments", None) if fn is not None else None
            if arguments is None:
                arguments = "{}"
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
            if isinstance(call, dict):
                index = int(call.get("index") or 0)
                fn = call.get("function") or {}
                call_id = _delta_str(call.get("id"))
                name = _delta_str(fn.get("name"))
                arguments = fn.get("arguments") or ""
            else:
                index = int(getattr(call, "index", 0) or 0)
                fn = getattr(call, "function", None)
                call_id = _delta_str(getattr(call, "id", None))
                name = _delta_str(getattr(fn, "name", None) if fn is not None else None)
                arguments = getattr(fn, "arguments", None) if fn is not None else None
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


class ResponsesClient:
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

        def once() -> Any:
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

        try:
            response = _await_abortable(
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
        output = [item_to_dict(item) for item in (getattr(response, "output", None) or [])]
        return ModelResponse(
            output=output,
            usage=usage_from_response(response),
            raw_id=str(getattr(response, "id", "") or ""),
        )

    def _create(self, kwargs: dict[str, Any]) -> Any:
        pending = dict(kwargs)
        pending.pop("stream", None)
        drop_order = (
            "include",
            "prompt_cache_retention",
            "prompt_cache_options",
            "prompt_cache_key",
            "reasoning",
        )
        while True:
            try:
                return self.client.responses.create(**pending)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if not _is_param_error(exc):
                    raise
                dropped = False
                for key in drop_order:
                    if key in pending:
                        pending.pop(key)
                        dropped = True
                        break
                if not dropped:
                    raise

    def _stream(self, kwargs: dict[str, Any], on_delta: DeltaFn) -> Any:
        pending = dict(kwargs)
        pending["stream"] = True
        stream = self.client.responses.create(**pending)
        self._stream_obj = stream
        final = None
        try:
            for event in stream:
                abort = getattr(self, "abort", None)
                if abort is not None and abort.is_set():
                    raise KeyboardInterrupt
                completed = consume_stream_event(event, on_delta)
                if completed is not None:
                    final = completed
        except KeyboardInterrupt:
            self.cancel()
            raise
        finally:
            if self._stream_obj is stream:
                self._stream_obj = None
        if final is None:
            getter = getattr(stream, "get_final_response", None)
            if callable(getter):
                final = getter()
        if final is None:
            raise RuntimeError("stream ended without a completed response")
        return final


class ChatCompletionsClient:
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

        def once() -> Any:
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

        try:
            response = _await_abortable(
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
        pending = dict(kwargs)
        pending.pop("stream", None)
        pending.pop("stream_options", None)
        drop_order = ("reasoning_effort", "reasoning", "tool_choice")
        while True:
            try:
                return self.client.chat.completions.create(**pending)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if not _is_param_error(exc):
                    raise
                dropped = False
                for key in drop_order:
                    if key in pending:
                        pending.pop(key)
                        dropped = True
                        break
                if not dropped:
                    raise

    def _stream(self, kwargs: dict[str, Any], on_delta: DeltaFn) -> ModelResponse:
        pending = dict(kwargs)
        pending["stream"] = True
        pending["stream_options"] = {"include_usage": True}
        drop_order = ("reasoning_effort", "reasoning", "tool_choice", "stream_options")
        stream = None
        last_exc: Exception | None = None
        while True:
            try:
                stream = self.client.chat.completions.create(**pending)
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_exc = exc
                if not _is_param_error(exc):
                    raise
                dropped = False
                for key in drop_order:
                    if key in pending:
                        pending.pop(key)
                        dropped = True
                        break
                if not dropped:
                    raise
        if stream is None:
            raise last_exc or RuntimeError("chat stream failed")
        self._stream_obj = stream
        assembled = _ChatAssembler()
        try:
            for event in stream:
                abort = getattr(self, "abort", None)
                if abort is not None and abort.is_set():
                    raise KeyboardInterrupt
                assembled.feed(event, on_delta)
        except KeyboardInterrupt:
            self.cancel()
            raise
        finally:
            if self._stream_obj is stream:
                self._stream_obj = None
        return ModelResponse(
            output=assembled.output(),
            usage=usage_from_response(assembled),
            raw_id=assembled.id,
        )


def _is_param_error(exc: Exception) -> bool:
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
    if is_transient_error(exc):
        return False
    text = str(exc).lower()
    return any(token in text for token in ("stream not supported", "streaming is not", "stream=true"))


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
    """Run fn; if abort is set, raise KeyboardInterrupt without waiting it out."""
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
    """Deterministic stand-in used by tests, replay, and offline eval."""

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
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": raw}


def assistant_text(text: str) -> Item:
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
    if provider.api == "chat":
        return ChatCompletionsClient(provider, effort=effort, cache_key=cache_key)
    return ResponsesClient(provider, effort=effort, cache_key=cache_key)
