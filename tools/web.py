from __future__ import annotations

import html as htmlmod
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
EXA_API_URL = "https://api.exa.ai/search"
TAVILY_API_URL = "https://api.tavily.com/search"
# Exa's hosted MCP is free but rate-limits anonymous requests per IP; after a
# 429, skip it entirely for this long instead of burning round trips.
MCP_RATE_COOLDOWN = 60.0
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 5
SNIPPET_MAX = 400
TIMEOUT = 30
USER_AGENT = "wheel-agent/0.1 (+https://github.com/wheel)"

# Monotonic deadline while the keyless Exa MCP is throttled (see MCP_RATE_COOLDOWN).
_mcp_cooldown_until = 0.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    # Refuse to auto-follow redirects: the default opener would follow 301/
    # 302/303/307 (even cross-origin) before fetch_url's per-hop SSRF host
    # check ever runs. Returning None makes urlopen raise HTTPError so the
    # manual redirect loop below validates every hop itself.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N803
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


class WebError(Exception):
    pass


def _clip_snippet(text: str) -> str:
    """Whitespace-collapsed snippet truncated to the display budget, elided."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > SNIPPET_MAX:
        return text[: SNIPPET_MAX - 3] + "..."
    return text


def search_web(query: str, *, num_results: int = 5, api_key: str | None = None) -> str:
    query = query.strip()
    if not query:
        raise WebError("query is empty")
    num_results = max(1, min(int(num_results or 5), 10))
    key = api_key or os.getenv("EXA_API_KEY") or ""
    errors: list[str] = []
    if key:
        try:
            rows = _search_exa_api(query, num_results, key)
        except WebError as exc:
            errors.append(f"Exa API: {exc}")
            rows = []
    else:
        try:
            rows = _search_exa_mcp(query, num_results)
        except WebError as exc:
            errors.append(f"Exa MCP: {exc}")
            rows = []
    if errors and not rows:
        rows = _try_search_tavily(query, num_results, errors)
    if not rows:
        if errors:
            hint = (
                ""
                if (os.getenv("EXA_API_KEY") or os.getenv("TAVILY_API_KEY"))
                else "; set EXA_API_KEY or TAVILY_API_KEY for unthrottled search"
            )
            raise WebError(" | ".join(errors) + hint)
        return "(no results)"
    lines: list[str] = []
    for i, row in enumerate(rows, start=1):
        title = row.get("title") or f"Source {i}"
        url = row.get("url") or ""
        snippet = _clip_snippet(row.get("snippet") or "")
        lines.append(f"{i}. {title}\n   {url}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


def _decode_body(raw: bytes, content_type: str) -> str:
    """Decode with the declared charset when present (gbk pages would mojibake as utf-8)."""
    match = re.search(r"charset=([\w.\-]+)", content_type or "", re.I)
    if match:
        try:
            return raw.decode(match.group(1))
        except (LookupError, UnicodeDecodeError):
            pass
    return raw.decode("utf-8", errors="replace")


def fetch_url(url: str) -> str:
    current = _validate_url(url)
    redirects = 0
    while True:
        req = urllib.request.Request(
            current.geturl(),
            headers={"User-Agent": USER_AGENT, "Accept": "text/*,application/json,application/xml"},
            method="GET",
        )
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                if 300 <= int(status) < 400:
                    location = resp.headers.get("Location")
                    if not location:
                        raise WebError(f"redirect {status} without Location")
                    nxt = urljoin(current.geturl(), location)
                    target = _validate_url(nxt)
                    if (target.scheme, target.hostname, target.port) != (current.scheme, current.hostname, current.port):
                        raise WebError(f"cross-origin redirect to {target.hostname} not followed; fetch that URL directly")
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        raise WebError(f"exceeded {MAX_REDIRECTS} redirects")
                    current = target
                    continue
                ctype_header = resp.headers.get("Content-Type") or ""
                ctype = ctype_header.split(";")[0].strip().lower()
                raw = resp.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                location = exc.headers.get("Location") if exc.headers else None
                if not location:
                    raise WebError(f"HTTP {exc.code}: {exc.reason}") from exc
                nxt = urljoin(current.geturl(), location)
                target = _validate_url(nxt)
                if (target.scheme, target.hostname, target.port) != (current.scheme, current.hostname, current.port):
                    raise WebError(f"cross-origin redirect to {target.hostname} not followed") from exc
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise WebError(f"exceeded {MAX_REDIRECTS} redirects") from exc
                current = target
                continue
            raise WebError(f"HTTP {exc.code}: {exc.reason}") from exc
        except WebError:
            raise
        except Exception as exc:
            raise WebError(str(exc)) from exc
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]
        text = _decode_body(raw, ctype_header)
        if ctype in {"text/html", "application/xhtml+xml"} or (not ctype and _looks_html(text)):
            text = html_to_text(text)
        return f"URL: {current.geturl()}\n\n{text.strip() or '(empty)'}"


def _is_rate_limited(message: str) -> bool:
    lowered = (message or "").lower()
    return (
        "rate limit" in lowered
        or "rate-limit" in lowered
        or "too many requests" in lowered
        or "429" in lowered
    )


def _note_rate_limit(message: str) -> None:
    global _mcp_cooldown_until
    if _is_rate_limited(message):
        _mcp_cooldown_until = time.monotonic() + MCP_RATE_COOLDOWN


def _try_search_tavily(query: str, num_results: int, errors: list[str]) -> list[dict[str, str]]:
    """Fallback provider when Exa fails or is rate-limited (mirrors pi-web-access cascades)."""
    key = os.getenv("TAVILY_API_KEY") or ""
    if not key:
        errors.append("Tavily fallback unavailable: TAVILY_API_KEY not set")
        return []
    try:
        return _search_tavily(query, num_results, key)
    except WebError as exc:
        errors.append(f"Tavily: {exc}")
        return []


def _search_tavily(query: str, num_results: int, api_key: str) -> list[dict[str, str]]:
    base = (os.getenv("TAVILY_BASE_URL") or "https://api.tavily.com").rstrip("/")
    payload = json.dumps(
        {"query": query, "search_depth": "basic", "max_results": num_results, "include_answer": "basic"}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/search",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise WebError(f"Tavily API failed: {exc}") from exc
    rows: list[dict[str, str]] = []
    for item in body.get("results") or []:
        if not item.get("url"):
            continue
        rows.append(
            {"title": str(item.get("title") or ""), "url": str(item["url"]), "snippet": _clip_snippet(str(item.get("content") or ""))}
        )
    return rows


def _search_exa_api(query: str, num_results: int, api_key: str) -> list[dict[str, str]]:
    payload = json.dumps({"query": query, "type": "auto", "numResults": num_results}).encode("utf-8")
    req = urllib.request.Request(
        EXA_API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise WebError(f"Exa API failed: {exc}") from exc
    rows = []
    for item in body.get("results") or []:
        if not item.get("url"):
            continue
        snippet = ""
        highlights = item.get("highlights") or []
        if isinstance(highlights, list) and highlights:
            snippet = _clip_snippet(" ".join(str(h) for h in highlights if h))
        elif item.get("text"):
            snippet = _clip_snippet(str(item["text"]))
        rows.append({"title": str(item.get("title") or ""), "url": str(item["url"]), "snippet": snippet})
    return rows


def _search_exa_mcp(query: str, num_results: int) -> list[dict[str, str]]:
    if time.monotonic() < _mcp_cooldown_until:
        raise WebError("Exa free MCP rate-limited (cooldown active); set EXA_API_KEY or TAVILY_API_KEY")
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "web_search_exa", "arguments": {"query": query, "numResults": num_results}},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{EXA_MCP_URL}?tools=web_search_exa",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-exa-source": "wheel-agent",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}: {exc.reason}"
        try:
            detail += f" — {exc.read().decode('utf-8', 'replace')[:200]}"
        except Exception:
            pass
        _note_rate_limit(detail)
        raise WebError(f"Exa MCP failed: {detail}") from exc
    except Exception as exc:
        raise WebError(f"Exa MCP failed: {exc}") from exc
    try:
        parsed = _parse_mcp_body(body)
        text = _mcp_text(parsed)
    except WebError as exc:
        _note_rate_limit(str(exc))
        raise
    try:
        data = json.loads(text)
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, list):
            rows = []
            for item in results:
                if isinstance(item, dict) and item.get("url"):
                    rows.append(
                        {
                            "title": str(item.get("title") or ""),
                            "url": str(item["url"]),
                            "snippet": _clip_snippet(str(item.get("text") or item.get("snippet") or "")),
                        }
                    )
            if rows:
                return rows
    except json.JSONDecodeError:
        pass
    return _parse_mcp_text_results(text)


def _parse_mcp_body(body: str) -> dict[str, Any]:
    parsed: dict[str, Any] | None = None
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            candidate = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and (candidate.get("result") or candidate.get("error")):
            parsed = candidate
            break
    if parsed is None:
        try:
            candidate = json.loads(body)
        except json.JSONDecodeError as exc:
            raise WebError("Exa MCP returned an empty response") from exc
        if not isinstance(candidate, dict):
            raise WebError("Exa MCP returned an empty response")
        parsed = candidate
    if parsed.get("error"):
        err = parsed["error"]
        raise WebError(f"Exa MCP error: {err.get('message') or err}")
    return parsed


def _mcp_text(parsed: dict[str, Any]) -> str:
    result = parsed.get("result") or {}
    chunks: list[str] = []
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("text"):
            chunks.append(str(part.get("text") or ""))
        elif isinstance(part, str):
            chunks.append(part)
    text = "\n".join(chunks).strip()
    if result.get("isError"):
        raise WebError(text or "Exa MCP tool error")
    return text


def _parse_mcp_text_results(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    blocks = re.split(r"\n(?=\S+\nhttps?://)", text.strip()) if text.strip() else []
    if not blocks:
        url_re = re.compile(r"https?://\S+")
        for m in url_re.finditer(text):
            rows.append({"title": "", "url": m.group(0).rstrip(").,]"), "snippet": ""})
        return rows
    for block in blocks:
        url_m = re.search(r"https?://\S+", block)
        if not url_m:
            continue
        url = url_m.group(0).rstrip(").,]")
        title = block.split("\n", 1)[0].strip()
        if title.startswith("http"):
            title = ""
        snippet = _clip_snippet(block[url_m.end() :])
        rows.append({"title": title, "url": url, "snippet": snippet})
    return rows


def _validate_url(url: str):
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise WebError(f"invalid URL: {url}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise WebError(f"unsupported URL scheme {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise WebError("credentials in URLs are not allowed")
    host = parsed.hostname
    if not host:
        raise WebError("URL must include a hostname")
    if host.lower() in {"localhost", "127.0.0.1", "::1"} or host.lower().endswith(".local"):
        raise WebError(f"blocked host: {host}")
    _assert_public_host(host)
    return parsed


def _assert_public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise WebError(f"failed to resolve {host}") from exc
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise WebError(f"blocked private or reserved address for {host}: {ip}")


def _looks_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return "<html" in head or "<!doctype html" in head


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)


def html_to_text(source: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        return htmlmod.unescape(re.sub(r"<[^>]+>", " ", source))
    text = htmlmod.unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
