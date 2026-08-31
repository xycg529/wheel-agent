# `tools/web.py` 逐段讲解

> 本篇讲 web 能力层。上游是 [tools/tools.md](tools.md)（`web_search` / `web_fetch` 两个 ToolSpec 的 execute 实现），下游只有标准库。

一句话职责：把「联网搜索一个查询词」和「抓一个公网 URL 转成纯文本」两件事做成两个纯函数，全部用标准库实现（不依赖 `requests`），并在抓取路径上做 SSRF 防护。

- 行数：469 行
- 依赖：
  - 标准库 `urllib` / `socket` / `ipaddress` / `html.parser` / `json` / `re`（无第三方依赖，符合项目"运行时只依赖两个包"的约束）
  - [core/truncate.md](../core/truncate.md) —— 间接协作：本模块只负责"抓到全量文本"，截断由 `ToolRuntime._after()` 按 `truncate="head"` 声明统一做
- 被谁用：
  - [tools/tools.md](tools.md) —— `_web_search`（759–765 行）、`_web_fetch`（768–774 行）包装成本模块的 `search_web` / `fetch_url`
  - [tools/safety.md](safety.md) —— 两个工具都列在 `READ_ONLY`，永不弹确认
  - [tools/audit.md](audit.md) —— `redact_tool_output` 对敏感 URL 的 `web_fetch` 整个输出打码
  - [ui/graph.md](../ui/graph.md) —— `PARALLEL_TOOLS` 含这两个，相邻的 web 调用在图上并排一行
  - [core/prompt.md](../core/prompt.md) —— 系统提示的工具清单里列出 `web_search, web_fetch`

## 目录

- [1. 常量与冷却状态（1–33 行）](#1-常量与冷却状态1–33-行)
- [2. 禁自动重定向的 opener（36–45 行）](#2-禁自动重定向的-opener36–45-行)
- [3. `WebError` 与摘要裁剪（47–57 行）](#3-weberror-与摘要裁剪47–57-行)
- [4. `search_web`：三级 provider 级联（59–101 行）](#4-search_web三级-provider-级联59–101-行)
- [5. 编码解码与重定向单跳校验（103–168 行）](#5-编码解码与重定向单跳校验103–168-行)
- [6. `fetch_url`：抓取主循环（128–168 行）](#6-fetch_url抓取主循环128–168-行)
- [7. 限流识别与冷却（170–186 行）](#7-限流识别与冷却170–186-行)
- [8. Tavily 回退（188–226 行）](#8-tavily-回退188–226-行)
- [9. Exa API（228–254 行）](#9-exa-api228–254-行)
- [10. Exa 免费 MCP（256–317 行）](#10-exa-免费-mcp256–317-行)
- [11. MCP 响应解析三层（319–384 行）](#11-mcp-响应解析三层319–384-行)
- [12. SSRF 防护：`_validate_url` / `_assert_public_host`（386–427 行）](#12-ssrf-防护_validate_url--_assert_public_host386–427-行)
- [13. HTML → 纯文本（428–469 行）](#13-html--纯文本428–469-行)

---

## 1. 常量与冷却状态（1–33 行）

三个 provider 端点：

```python
EXA_MCP_URL  = "https://mcp.exa.ai/mcp"        # 免费 MCP（无需 key）
EXA_API_URL  = "https://api.exa.ai/search"     # Exa 官方 API（需 EXA_API_KEY）
TAVILY_API_URL = "https://api.tavily.com/search"  # 回退 provider
```

注意 `TAVILY_API_URL` **定义了但没用**：`_search_tavily` 里实际拼的是 `os.getenv("TAVILY_BASE_URL") or "https://api.tavily.com"`，多了一次 env 覆盖的机会，代价是这个常量成了死值。

阈值常量（26–30 行）：

| 常量 | 值 | 意图 |
|---|---|---|
| `MAX_BYTES` | 1,000,000 | 单次抓取上限（≈1MB）。比 [core/truncate.md](../core/truncate.md) 的 50KB **先**起作用：它是网络侧的硬顶，截断是之后给模型看的软预算 |
| `MAX_REDIRECTS` | 5 | 重定向上限，防重定向环 |
| `SNIPPET_MAX` | 400 | 搜索摘要显示上限（字符） |
| `TIMEOUT` | 30 | 所有 HTTP 调用统一超时（秒） |
| `MCP_RATE_COOLDOWN` | 60.0 | 免费 MCP 被限流后的整段冷却时长（秒） |

`USER_AGENT = "wheel-agent/0.1 (+https://github.com/wheel)"` —— 带项目标识，网站管理员能从日志认出是谁在抓；比默认的 `Python-urllib/3.x` 少被 WAF 拒。

`_mcp_cooldown_until`（33 行）是模块级全局变量：存一个 `time.monotonic()` 截止时间戳。**用 monotonic 而非 wall clock**——系统时间被改也不会让冷却逻辑错乱。

## 2. 禁自动重定向的 opener（36–45 行）

```python
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # 拒绝自动跟随
```

`urllib` 默认会**自动**跟掉 301/302/303/307。这对本模块是致命的：自动跟随发生在 `fetch_url` 的 SSRF 主机校验**之前**，攻击者可以用 `http://evil.com → http://169.254.169.254/latest/meta-data/` 把请求带进内网。

返回 `None` 让 `urlopen` 对 3xx 抛 `HTTPError`，把控制权交还给 `fetch_url` 的手动循环——**每一跳都要过 `_validate_url`**。这是"让安全的默认行为失效，自己接管"的典型处理。

`_OPENER = urllib.request.build_opener(_NoRedirectHandler)` 模块级只建一次。

## 3. `WebError` 与摘要裁剪（47–57 行）

`WebError(Exception)`：本模块唯一的异常类型。所有 provider 差异（HTTP 错误、JSON 解析失败、MCP error）都归一到它，再由 [tools/tools.md](tools.md) 的 `_web_search` / `_web_fetch` 转成 `ValueError` —— 工具层的约定是"抛 `ValueError` 就是工具出错"，`ToolRuntime` 会把它变成 `is_error` 结果而不是崩掉整个循环。

`_clip_snippet`（51–57 行）做两件事：

```python
text = re.sub(r"\s+", " ", text or "").strip()   # 所有空白压成单空格
if len(text) > SNIPPET_MAX:
    return text[: SNIPPET_MAX - 3] + "..."
```

**压成单行**是关键：搜索摘要要拼进 `f"   {snippet}"` 这种缩进列表里，原始文本里的换行会把编号列表结构撑坏。截断时留 3 字符给省略号，保证总长不超 `SNIPPET_MAX`。

## 4. `search_web`：三级 provider 级联（59–101 行）

对外唯一入口。级联策略：

```
有 EXA_API_KEY  → _search_exa_api()   （付费，稳）
否则            → _search_exa_mcp()   （免费，按 IP 限流）
两者都失败/无结果 → _try_search_tavily()  （需 TAVILY_API_KEY）
```

注意分支是 **if/else 而非连续 try**：有 key 就**只**试 API，不会先白试一遍免费的 MCP。注释说明这份级联是**镜像 pi-web-access 的策略**。

参数钳制（65 行）：`max(1, min(int(num_results or 5), 10))` —— 模型爱发 `num_results=50`，这里硬钳到 10，与工具 schema 里写的 "Default 5, max 10" 一致。

**错误累积模式**（67–79 行）：每个 provider 的失败都 `errors.append(f"{name}: {exc}")`，只有**全部失败**才汇总抛 `WebError(" | ".join(errors) + hint)`。好处：模型看到的是"Exa API: ... | Tavily: ..."一条完整诊断，而不是最后一级的孤零零报错。

`hint`（83–88 行）只在**两个 key 都没配**时才追加：

```python
hint = "" if (os.getenv("EXA_API_KEY") or os.getenv("TAVILY_API_KEY")) else "; set EXA_API_KEY or TAVILY_API_KEY for unthrottled search"
```

用户已经配了 key 还失败时，再说"去配 key"是误导。这条提示文本的用途是：让**模型**能告诉**用户**该怎么修，而不是 agent 自己卡住。

输出格式（92–101 行）——编号列表，每条三行中的两行：

```
1. Title
   https://url
   snippet（可选，单行）
```

`title` 为空时退成 `f"Source {i}"`，保证编号列表不断档。无结果时返回字符串 `"(no results)"` 而**不报错**——搜不到东西是正常结果，不是错误。

## 5. 编码解码与重定向单跳校验（103–168 行）

`_decode_body`（103–111 行）：

```python
match = re.search(r"charset=([\w.\-]+)", content_type or "", re.I)
if match:
    try:
        return raw.decode(match.group(1))      # 按声明解
    except (LookupError, UnicodeDecodeError):
        pass                                   # 声明的编码不存在/解码失败 → 落到 utf-8
return raw.decode("utf-8", errors="replace")
```

先按 Content-Type 声明的 charset 解（gbk 页面按 utf-8 解会整篇乱码），失败才回退 utf-8。**最后一道 `errors="replace"`** 保证任何字节都能变成字符串——抓取工具绝不能因为编码问题抛异常。

`_redirect`（114–126 行）处理一跳重定向，三个检查：

1. 无 `Location` 头 → 抛 `WebError(missing)`（`missing` 是调用方传进来的文案，因为 HTTP 200 分支和 HTTPError 分支的错误描述不同）。
2. **跨源重定向不跟**：

```python
if (target.scheme, target.hostname, target.port) != (current.scheme, current.hostname, current.port):
    raise WebError(f"cross-origin redirect to {target.hostname} not followed" + ("; fetch that URL directly" if hint else ""))
```

跨源重定向是**重定向劫持**的经典载体。这里不跟，而是把目标 URL 放进错误信息，让模型自己决定要不要重新调 `web_fetch` 抓那个 URL —— 决定权在模型，风险提示在人能看到的文本里。

3. 跳数超 `MAX_REDIRECTS` → 抛错。注意 `redirects += 1` 在**校验通过后**才加，超限判断用 `>`。

`urljoin(current.geturl(), location)` 处理相对 Location（`/next` 这种）。

## 6. `fetch_url`：抓取主循环（128–168 行）

结构是一个 `while True`，每轮发一次请求：

```python
req = urllib.request.Request(
    current.geturl(),
    headers={"User-Agent": USER_AGENT, "Accept": "text/*,application/json,application/xml"},
    method="GET",
)
```

`Accept` 头只声明文本类——不请求图片/二进制，省带宽也避开无意义的解码。

**两个 3xx 分支**（141–160 行）：

- 正常响应但 `status` 在 300–399：`_redirect(..., hint=True)`，`continue`。
- `urllib.error.HTTPError` 且 `exc.code` 在 300–399：因为禁了自动重定向，这种情况才出现；`_redirect(..., hint=False, exc=exc)`，`continue`。

`hint` 的差别：正常 3xx 响应说明服务端给了明确的 Location，值得提示"直接抓那个 URL"；`HTTPError` 的 3xx 通常是错误页，不提示。

**异常分层**（154–160 行）：

```python
except urllib.error.HTTPError as exc:   # 先单独处理 3xx
except WebError:
    raise                                # _redirect 抛的，原样上抛（否则被下面兜住会丢信息）
except Exception as exc:
    raise WebError(str(exc)) from exc    # 超时/DNS/连接问题 → 归一
```

`except WebError: raise` 这一行不能省：它排在宽 `except Exception` **之前**，否则 `_redirect` 抛的精心构造的错误文案会被 `str(exc)` 再包一层。

**多读 1 字节判超限**（146、163–164 行）：

```python
raw = resp.read(MAX_BYTES + 1)   # 多读 1 字节用于判断是否超限
...
if len(raw) > MAX_BYTES:
    raw = raw[:MAX_BYTES]
```

这是不依赖 `Content-Length`（服务端常常不给）判断"响应是否超预算"的技巧：读 N+1 字节，读到 N+1 说明至少还有更多。超限时**静默截断**，不报错、不提示——因为截断到 1MB 后还会过一遍 [core/truncate.md](../core/truncate.md) 的 50KB 软预算，那层会明确告诉模型"输出被截了，完整内容在 .wheel-agent/outputs/"。**网络层静默、展示层显式**，分工清晰。

**返回格式**（165–168 行）：

```python
if ctype in {"text/html", "application/xhtml+xml"} or (not ctype and _looks_html(text)):
    text = html_to_text(text)
return f"URL: {current.geturl()}\n\n{text.strip() or '(empty)'}"
```

只有确认是 HTML 才走 `html_to_text`；JSON / XML / 纯文本原样返回（模型读原始 JSON 比读被剥过的文本更好）。`current.geturl()` 是**最终** URL（跟过重定向后的），不是用户传进来的——模型需要知道实际抓的是哪儿。

`not ctype and _looks_html(text)`：有些服务器根本不发 Content-Type，靠内容猜（见第 13 节）。

## 7. 限流识别与冷却（170–186 行）

`_is_rate_limited`（170–179 行）靠**字符串匹配**认限流：

```python
"rate limit" in lowered or "rate-limit" in lowered or "too many requests" in lowered or "429" in lowered
```

为什么不用状态码：MCP 的错误信息常把 429 包在 JSON-RPC 的 error message 里，状态码可能是 200。文本匹配对跨 provider 更宽容，代价是"页面正文里恰好含 429"会误判——但限流判断只作用于 MCP 这条路径，误判的后果只是多冷却 60 秒，可接受。

`_note_rate_limit`（181–186 行）设全局冷却：

```python
global _mcp_cooldown_until
if _is_rate_limited(message):
    _mcp_cooldown_until = time.monotonic() + MCP_RATE_COOLDOWN
```

设计意图写在常量注释里：**免费 MCP 按 IP 限匿名请求，收到 429 后整段冷却，不再白白多花几个往返**。一次 run 里搜 10 次，第一次被限流后剩下 9 次直接跳过 MCP（要么有 key 走 API，要么回退 Tavily），省掉 9 个必然失败的网络往返。

## 8. Tavily 回退（188–226 行）

`_try_search_tavily`（188–199 行）：**只有 Exa 两条路都失败才调用**（第 80 行的 `if errors and not rows`）。没配 `TAVILY_API_KEY` 时往 `errors` 追加一条说明再返回空列表——把"为什么没回退"也记进最终错误信息，避免用户困惑于"明明配了 Tavily 怎么没用"。

`_search_tavily`（201–226 行）：POST JSON，Bearer 鉴权。

```python
base = (os.getenv("TAVILY_BASE_URL") or "https://api.tavily.com").rstrip("/")
```

env 覆盖 base URL 是为了接自建/代理端点；`.rstrip("/")` 保证 `.../` 和 `...` 都能拼对。请求参数 `search_depth: "basic"`、`include_answer: "basic"` —— 用便宜档，深度搜索的额外开销对这个场景不值。

异常归一：`except Exception as exc: raise WebError(f"Tavily API failed: {exc}")`。注意这里**不解析 HTTP 错误体**，只带 `str(exc)`——Tavily 是最后一级，错误信息够定位就行。

结果只收有 `url` 的条目，字段名映射：`content` → `snippet`（Tavily 叫 content，Exa 叫 text/highlights）。

## 9. Exa API（228–254 行）

`_search_exa_api`：POST 到 `EXA_API_URL`，`x-api-key` 头（Exa 的鉴权头，不是 Bearer）。

请求体 `{"query": ..., "type": "auto", "numResults": num_results}` —— `type: "auto"` 让 Exa 自己判断该用神经搜索还是关键词搜索。

摘要优先级（241–247 行）：

```python
highlights = item.get("highlights") or []
if isinstance(highlights, list) and highlights:
    snippet = _clip_snippet(" ".join(str(h) for h in highlights if h))
elif item.get("text"):
    snippet = _clip_snippet(str(item["text"]))
```

**highlights 优先于 text**：`highlights` 是 Exa 挑出的与查询最相关的片段，`text` 是整页正文。同样是 400 字符预算，highlights 的信息密度高得多。多个 highlight 用空格接起来再统一裁剪。

## 10. Exa 免费 MCP（256–317 行）

`_search_exa_mcp`：**无需 key 的默认搜索路径**，走 MCP 的 JSON-RPC over HTTP。

冷却检查（258–259 行）在函数最开头，冷却期内直接抛错，一个网络请求都不发。

```python
payload = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "method": "tools/call",
    "params": {"name": "web_search_exa", "arguments": {"query": query, "numResults": num_results}},
})
req = urllib.request.Request(f"{EXA_MCP_URL}?tools=web_search_exa", ...)
```

`?tools=web_search_exa` 是 Exa 托管 MCP 的约定：用 query 参数声明这次会话要用哪些工具（省掉 MCP 的 initialize + tools/list 握手）。**项目不依赖 MCP SDK**，直接手写 JSON-RPC，与 README 里"没有 MCP"的定位一致——这里只是调用一个 HTTP 端点。

`Accept: "application/json, text/event-stream"` —— MCP 可能回 SSE 流，也可能回裸 JSON，两种都要接（解析见第 11 节）。

**HTTPError 分支读错误体**（288–294 行）：

```python
detail = f"HTTP {exc.code}: {exc.reason}"
try:
    detail += f" — {exc.read().decode('utf-8', 'replace')[:200]}"
except Exception:
    pass
_note_rate_limit(detail)   # 可能是 429，记冷却
```

读错误体**截到 200 字符**再拼——限流判断需要看到 "429"/"rate limit" 字样，但错误体可能是整个 HTML 错误页，不截断会污染错误信息。里面那层 `except Exception: pass` 是因为 `HTTPError.read()` 本身也可能失败。

结果解析分两路（305–317 行）：

1. 先尝试把文本当 JSON 解析，取 `data["results"]` 列表 → 标准路径。
2. 不行就 `_parse_mcp_text_results(text)` → 宽容解析纯文本格式。

## 11. MCP 响应解析三层（319–384 行）

`_parse_mcp_body`（319–347 行）—— 兼容 SSE 和裸 JSON：

```python
for line in body.splitlines():
    if not line.startswith("data:"):
        continue
    ...
    if isinstance(candidate, dict) and (candidate.get("result") or candidate.get("error")):
        parsed = candidate
        break
```

SSE 逐行扫描，只认 `data:` 开头，且要求该行 JSON 里有 `result` 或 `error` 字段——**避免认错 SSE 的心跳行或中间事件**。扫不到就退回整体 `json.loads(body)`。两种都失败抛 `WebError("Exa MCP returned an empty response")`。

`parsed.get("error")` 检查在**最后统一做**（345–347 行）：JSON-RPC 的 error 可以在 SSE 路径或裸 JSON 路径里出现，收敛到一处抛错。

`_mcp_text`（349–362 行）：把 MCP 的 `content` 数组拼成文本。

```python
for part in result.get("content") or []:
    if isinstance(part, dict) and part.get("text"):
        chunks.append(str(part.get("text") or ""))
    elif isinstance(part, str):
        chunks.append(part)
text = "\n".join(chunks).strip()
if result.get("isError"):
    raise WebError(text or "Exa MCP tool error")
```

MCP 的 content 是 `{type: "text", text: "..."}` 数组，但不同实现会塞裸字符串，两种都收。**`isError` 检查在拼完文本之后**——这样错误信息里能带上 MCP 给的说明文字，而不是一句干巴巴的 "tool error"。

`_parse_mcp_text_results`（364–384 行）：最后一层的兜底，用正则从纯文本里抠结果：

```python
blocks = re.split(r"\n(?=\S+\nhttps?://)", text.strip())
```

按"一行标题 + 一行 URL"的模式切块。切不出块（有些 MCP 只返回一串 URL）就退化成 `url_re.finditer` 把所有 URL 抠出来，`title` 和 `snippet` 留空。

两处 `.rstrip(").,]")` 是清 URL 尾部的标点——文本里 URL 后面常跟句号或括号。

宽容解析的设计意图：免费 MCP 的输出格式不受本项目控制，与其因为格式变了整个工具挂掉，不如能抠出多少算多少。**降级优于失败**。

## 12. SSRF 防护：`_validate_url` / `_assert_public_host`（386–427 行）

`_validate_url`（386–403 行）是第一道，纯字符串层：

| 检查 | 拒绝理由 |
|---|---|
| scheme ∉ {http, https} | `file://` / `ftp://` / `gopher://` 都能打内网或读本地文件 |
| 有 `username` 或 `password` | 凭据 URL（`http://user:pass@host`）可用来混淆真实目标 |
| hostname 为空 | 无主机无意义 |
| host ∈ {localhost, 127.0.0.1, ::1} 或结尾 `.local` | 本机/mDNS 名直连内网服务 |

`_assert_public_host`（405–427 行）是第二道，**先 DNS 解析再逐 IP 校验**：

```python
infos = socket.getaddrinfo(host, None)
for info in infos:
    raw = info[4][0]
    ip = ipaddress.ip_address(raw.split("%")[0])   # 去掉 IPv6 的 zone 后缀
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise WebError(f"blocked private or reserved address for {host}: {ip}")
```

**为什么必须解析后逐个 IP 校验**：只查主机名的话，`http://internal.evil.com`（解析到 10.0.0.5）就绕过了。`getaddrinfo` 返回多个 IP 时**每个都要过**——只要有一个是内网地址就拒绝，否则 A 记录里混一个内网 IP 就能穿透。

挡的地址类型里 `is_link_local`（169.254.0.0/16）是重点：**云厂商的 metadata 服务就在 169.254.169.254**，这是 SSRF 最值钱的目标（能拿到 IAM 临时凭据）。

`raw.split("%")[0]` 处理 IPv6 的 zone id（`fe80::1%eth0`），`ipaddress` 不接受带 zone 的字符串。

**两道校验都在每一跳重定向前重跑**（第 116 行 `_validate_url(urljoin(...))`）——不是只在入口查一次。这是防重定向劫持的关键。

## 13. HTML → 纯文本（428–469 行）

`_looks_html`（428–432 行）：无 Content-Type 时的猜测。

```python
head = text.lstrip()[:200].lower()
return "<html" in head or "<!doctype html" in head
```

只看开头 200 字符、先 `lstrip()` 掉前导空白（有些页面开头有 BOM 或空行）。**保守策略**：认不出就不当 HTML 处理，原样返回——模型读带标签的原始文本比读被错误剥掉内容的文本更好。

`_HTMLText(HTMLParser)`（434–456 行）：状态机式剥离。

```python
def __init__(self):
    super().__init__(convert_charrefs=True)
    self.parts = []
    self._skip = 0
```

`convert_charrefs=True` 让 `&amp;` `&nbsp;` 这类实体自动转成字符（HTMLParser 的默认行为，写出来是强调这个依赖）。

两个关键机制：

1. **跳过计数而非布尔**（441–443、446–447 行）：

```python
def handle_starttag(self, tag, attrs):
    if tag in {"script", "style", "noscript"}:
        self._skip += 1
    if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
        self.parts.append("\n")
```

用 `+= 1` / `-= 1` 而不是 `True`/`False`，因为标签可能嵌套（`<div><script><script>`）。布尔标志在嵌套闭合时会被提前清掉，把后面本该跳过的 JS 代码吐出来。跳过的是 `script` / `style` / `noscript`——这三样对模型的正文理解全是噪音。

2. **块级标签换行**：`p/div/br/li/h1/h2/h3/tr` 的开始标签处插 `\n`，让页面结构在纯文本里保留成段落。**不插换行的 HTML→text 会变成一坨连在一起的单词**。注意只在开始标签插，结束标签不插——避免 `<p>a</p><p>b</p>` 产生多余空行。

`handle_data` 里 `if self._skip: return` —— 跳过块内的文本直接丢弃。

`html_to_text`（458–469 行）：

```python
parser = _HTMLText()
try:
    parser.feed(source)
    parser.close()
except Exception:
    return htmlmod.unescape(re.sub(r"<[^>]+>", " ", source))   # 粗暴回退
text = htmlmod.unescape("".join(parser.parts))
text = re.sub(r"[ \t]+\n", "\n", text)     # 清行尾空白
text = re.sub(r"\n{3,}", "\n\n", text)     # 压多余空行
return text.strip()
```

`HTMLParser` 对畸形 HTML 会抛异常（未闭合标签、非法实体都很常见），所以有**粗暴回退**：正则把所有 `<...>` 换成空格再 `unescape`。回退路径会丢掉"跳过 script"和"块级换行"两个特性，但**保证有输出**。

两个清理正则：`[ \t]+\n → \n` 清行尾空白（缩进的 HTML 源码会留下大量行尾空格）；`\n{3,} → \n\n` 把连续空行压成一个（块级标签密集处会产生很多空行，全留着浪费 token）。

**没有正文提取**：不做 readability 式的主体识别，导航/页脚/广告都在输出里。取舍是——正文提取算法复杂且容易切错，而模型本身擅长跳过无关内容；把 1MB 抓下来交给 [core/truncate.md](../core/truncate.md) 截前 50KB，通常正文就在里面（正文一般在页面前部）。

**没有缓存**：每次 `fetch_url` 都是真请求。同一 URL 在一轮里被抓两次就发两次请求——靠 `web_fetch` 在 [ui/graph.md](../ui/graph.md) 里标为可并行工具、模型自己合并同类调用来缓解。
