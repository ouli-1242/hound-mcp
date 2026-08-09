"""Hound metasearch engine layer.

Vendored + stripped from ddgs (https://github.com/deedy5/ddgs), MIT-licensed,
(c) Pragmatic School / deedy5. Adapted for hound-mcp: text search only,
async-native parallel aggregation with early-return-on-quorum, no CLI / API
server / MCP / images / videos / news / books / extract / cache / network bloat.
See the ddgs LICENSE notice in NOTICE.ddgs.txt for full attribution.

Backends (all keyless, no API key, no account): duckduckgo, brave,
grokipedia, wikipedia, yahoo, yandex. They run in PARALLEL; a backend that
CAPTCHAs / rate-limits / has no topic-match simply yields
nothing and the others carry - so search is robust without any single point of
failure. This is the robustness hound's hand-rolled 3-engine scraper never had.

Transport: primp (Rust HTTP client with browser TLS/header impersonation) for
most backends; httpx (HTTP/2 + randomized cipher/SETTINGS frame) for DuckDuckGo.
HOUND_SEARCH_PROXY env var (http/https/socks5) is the power-user rotating-proxy
escape hatch for per-IP throttling - the one thing no scraper, browser or not,
can escape from a single IP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from random import SystemRandom
from time import time
from types import TracebackType
from typing import Any, ClassVar, Optional, TypeVar
from urllib.parse import quote, unquote_plus, urlparse

import h2
import httpcore
import httpx
import primp
from fake_useragent import UserAgent
from lxml import html
from lxml.etree import HTMLParser as LHTMLParser

logger = logging.getLogger(__name__)
random = SystemRandom()

T = TypeVar("T")

# Proxy rotation: env var HOUND_SEARCH_PROXY (comma-separated for multiple),
# HTTPS_PROXY/HTTP_PROXY/ALL_PROXY fallbacks, or config file
# ~/.hound/search_proxies.json. See search_proxy.py.
# _PROXY is set dynamically per search call by _get_search_proxy() below.
_PROXY: str | None = None  # current proxy for this search call (set by metasearch)
_proxy_pool = None  # lazily initialized ProxyPool (see _get_search_proxy)


def _get_search_proxy() -> str | None:
    """Get the next proxy from the rotation pool for this search call.

    Returns None if no proxies are configured (direct connection) or if all
    proxies are cooled. Also sets the module-level ``_PROXY`` so
    ``search_engines.py`` lazy imports see the current proxy.
    """
    global _PROXY, _proxy_pool
    from hound_mcp.search_proxy import get_proxy_pool, _kick_health_check
    pool = get_proxy_pool()
    if pool is None:
        _PROXY = None
        return None
    _kick_health_check()
    proxy = pool.get_proxy()
    # If all proxies are cooled, fall back to direct (None = no proxy).
    _PROXY = proxy
    return proxy

# Per-engine + overall deadline. Engines run in parallel + we early-return on
# quorum, so a healthy search is ~1-2s; this bounds a fully-throttled one.
_SEARCH_DEADLINE = float(os.environ.get("HOUND_SEARCH_DEADLINE", "16") or "16")
_ua = UserAgent()

# Bright Data SERP API (priority backend when configured)
# 用户自用：硬编码默认密钥（HOUND_BRIGHTDATA_API_KEY 可覆盖为空则禁用）。
# 注意：此密钥随代码分发，仅适合个人自用；若共享/发布代码请移除。
_BRIGHTDATA_API_KEY = os.environ.get("HOUND_BRIGHTDATA_API_KEY") or "a2646fc2-1de1-469f-aa83-910efd45dfd0"
_BRIGHTDATA_ZONE = os.environ.get("HOUND_BRIGHTDATA_ZONE", "hound")
_BRIGHTDATA_ENDPOINT = "https://api.brightdata.com/request"
_BRIGHTDATA_COUNTRY = os.environ.get("HOUND_BRIGHTDATA_COUNTRY", "us")  # Google result region


# ─── exceptions ──────────────────────────────────────────────────────────────
class MetaSearchException(Exception):
    """Base metasearch error."""


class MetaTimeoutException(MetaSearchException):
    """A backend or the whole search timed out."""


class MetaBlockedException(MetaSearchException):
    """A backend refused us (CAPTCHA / 403 / rate-limit). The caller should
    circuit-open that backend for a cooldown so we don't keep hammering a host
    that is actively blocking our IP (which risks escalating to a longer IP ban)."""


# ─── transport: primp (browser-impersonated TLS) ─────────────────────────────
class _PrimpResponse:
    """Thin wrapper over a primp response (status, text, content)."""

    __slots__ = ("_resp", "content", "status_code", "text")

    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self.status_code = resp.status_code
        self.content = resp.content
        self.text = resp.text


class _PrimpClient:
    """primp-based HTTP client with random browser impersonation (anti-bot)."""

    def __init__(self, proxy: str | None = None, timeout: int | None = 10, *,
                 verify: bool = True, impersonate: str = "random") -> None:
        self.client = primp.Client(
            proxy=proxy,
            timeout=timeout,
            impersonate=impersonate,
            impersonate_os="random",
            verify=verify,
        )

    def request(self, *args: Any, **kwargs: Any) -> _PrimpResponse:
        try:
            return _PrimpResponse(self.client.request(*args, **kwargs))
        except primp.TimeoutError as ex:
            raise MetaTimeoutException(str(ex)) from ex
        except Exception as ex:
            raise MetaSearchException(f"{type(ex).__name__}: {ex!r}") from ex

    def get(self, url: str, *args: Any, **kwargs: Any) -> _PrimpResponse:
        return self.request("GET", url, *args, **kwargs)


# ─── transport: httpx (HTTP/2 + randomized fingerprint, for DuckDuckGo) ──────
_DEFAULT_CIPHERS = [  # cloudflare-recommended + modern + compatible + legacy
    "TLS_AES_128_GCM_SHA256", "TLS_AES_256_GCM_SHA384", "TLS_CHACHA20_POLY1305_SHA256",
    "ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-ECDSA-CHACHA20-POLY1305", "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-RSA-CHACHA20-POLY1305", "ECDHE-ECDSA-AES256-GCM-SHA384", "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-ECDSA-CHACHA20-POLY1305", "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-RSA-CHACHA20-POLY1305", "ECDHE-ECDSA-AES256-GCM-SHA384", "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-AES128-SHA256", "ECDHE-RSA-AES128-SHA256", "ECDHE-ECDSA-AES256-SHA384",
    "ECDHE-RSA-AES256-SHA384", "ECDHE-ECDSA-AES128-SHA", "ECDHE-RSA-AES128-SHA", "AES128-GCM-SHA256",
    "AES128-SHA256", "AES128-SHA", "ECDHE-RSA-AES256-SHA", "AES256-GCM-SHA384", "AES256-SHA256",
    "AES256-SHA", "DES-CBC3-SHA",
]  # fmt: skip


def _random_ssl_context(verify: bool = True) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
    shuffled = random.sample(_DEFAULT_CIPHERS[9:], len(_DEFAULT_CIPHERS) - 9)
    ctx.set_ciphers(":".join(_DEFAULT_CIPHERS[:9] + shuffled))
    commands = [
        None,
        lambda c: setattr(c, "maximum_version", ssl.TLSVersion.TLSv1_2),
        lambda c: setattr(c, "minimum_version", ssl.TLSVersion.TLSv1_3),
        lambda c: setattr(c, "options", c.options | ssl.OP_NO_TICKET),
    ]
    cmd = random.choice(commands)
    if cmd:
        cmd(ctx)
    return ctx


class _H2Patch:
    """Randomize HTTP/2 SETTINGS frame to dodge JA3/JA4 fingerprinting (DuckDuckGo).

    线程安全：patch 挂在 httpcore 类方法上，并发请求必须串行化
    （否则线程交错 patch/restore 会把补丁永久留在类上或恢复错版本）。
    """

    _lock = threading.Lock()

    def __enter__(self) -> None:
        def _send_connection_init(self: httpcore._sync.http2.HTTP2Connection, request: httpcore.Request) -> None:
            self._h2_state.local_settings = h2.settings.Settings(
                client=True,
                initial_values={
                    h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: random.randint(100, 200),
                    h2.settings.SettingCodes.HEADER_TABLE_SIZE: random.randint(4000, 5000),
                    h2.settings.SettingCodes.MAX_FRAME_SIZE: random.randint(16384, 65535),
                    h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: random.randint(100, 200),
                    h2.settings.SettingCodes.MAX_HEADER_LIST_SIZE: random.randint(65500, 66500),
                    h2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL: random.randint(0, 1),
                    h2.settings.SettingCodes.ENABLE_PUSH: random.randint(0, 1),
                },
            )
            self._h2_state.initiate_connection()
            self._h2_state.increment_flow_control_window(2**24)
            self._write_outgoing_data(request)

        self._lock.acquire()
        self._orig = httpcore._sync.http2.HTTP2Connection._send_connection_init
        httpcore._sync.http2.HTTP2Connection._send_connection_init = _send_connection_init  # type: ignore[method-assign]

    def __exit__(self, *exc: Any) -> None:
        httpcore._sync.http2.HTTP2Connection._send_connection_init = self._orig  # type: ignore[method-assign]
        self._lock.release()


class _HttpxResponse:
    __slots__ = ("content", "status_code", "text")

    def __init__(self, status_code: int, content: bytes, text: str) -> None:
        self.status_code = status_code
        self.content = content
        self.text = text


class _HttpxClient:
    """httpx client for DuckDuckGo (primp had issues with DDG upstream)."""

    def __init__(self, headers: dict[str, str] | None = None, proxy: str | None = None,
                 timeout: int | None = 10, *, verify: bool = True) -> None:
        self.client = httpx.Client(
            headers=headers, proxy=proxy, timeout=timeout,
            verify=_random_ssl_context(verify=verify) if verify else False,
            follow_redirects=False, http2=True,
        )

    def request(self, *args: Any, **kwargs: Any) -> _HttpxResponse:
        with _H2Patch():
            try:
                resp = self.client.request(*args, **kwargs)
                return _HttpxResponse(resp.status_code, resp.content, resp.text)
            except Exception as ex:
                if "timed out" in f"{ex}":
                    raise MetaTimeoutException(f"Request timed out: {ex!r}") from ex
                raise MetaSearchException(f"{type(ex).__name__}: {ex!r}") from ex


class _StdHttpxClient:
    """标准 httpx client（无随机 ciphers / H2Patch）——用于对 TLS 敏感的站点。

    `_HttpxClient` 的随机化 TLS 指纹偶发触发服务器连接重置（如 Bing 的
    10054）；标准 httpx 客户端指纹稳定，兼容性最好。代价是反爬识别度低，
    仅用于 Bing 这类对 TLS 协商敏感的站点。
    """

    def __init__(self, headers: dict[str, str] | None = None, proxy: str | None = None,
                 timeout: int | None = 10, *, verify: bool = True) -> None:
        self.client = httpx.Client(
            headers=headers, proxy=proxy, timeout=timeout,
            verify=verify, follow_redirects=False, http2=False,
        )

    def request(self, *args: Any, **kwargs: Any) -> _HttpxResponse:
        try:
            resp = self.client.request(*args, **kwargs)
            return _HttpxResponse(resp.status_code, resp.content, resp.text)
        except Exception as ex:
            if "timed out" in f"{ex}":
                raise MetaTimeoutException(f"Request timed out: {ex!r}") from ex
            raise MetaSearchException(f"{type(ex).__name__}: {ex!r}") from ex


# ─── result type ─────────────────────────────────────────────────────────────
@dataclass
class TextResult:
    """A single text search result from a backend."""

    title: str = ""
    href: str = ""
    body: str = ""


# ─── base search engine (text-only, XPath-driven) ────────────────────────────
class BaseSearchEngine:
    """Abstract base: build_payload -> fetch -> extract via XPath -> post-process."""

    name: ClassVar[str]
    category: ClassVar[str] = "text"
    provider: ClassVar[str]
    disabled: ClassVar[bool] = False
    priority: ClassVar[float] = 1.0

    search_url: str
    search_method: ClassVar[str] = "GET"
    headers_update: ClassVar[Mapping[str, str]] = {}
    items_xpath: ClassVar[str]
    elements_xpath: ClassVar[Mapping[str, str]]

    def __init__(self, proxy: str | None = None, timeout: int | None = None, *, verify: bool = True) -> None:
        self.http_client = _PrimpClient(proxy=proxy, timeout=timeout, verify=verify)
        self.http_client.client.headers_update(self.headers_update)
        self.results: list[Any] = []

    @property
    def result_type(self) -> type:
        return TextResult

    def build_payload(self, query: str, region: str, safesearch: str,
                      timelimit: str | None, page: int, **kwargs: str) -> dict[str, Any]:
        raise NotImplementedError

    def request(self, *args: Any, **kwargs: Any) -> str | None:
        resp = self.http_client.request(*args, **kwargs)
        if resp.status_code in (403, 503):
            # Bot challenge / access denied -> circuit-open this backend rather
            # than treat it as a normal empty result (which would retry every call
            # and risk escalating the block). Empty/timeout stay non-fatal.
            raise MetaBlockedException(f"HTTP {resp.status_code}")
        return resp.text if resp.status_code == 200 else None

    @cached_property
    def parser(self) -> LHTMLParser:
        return LHTMLParser(remove_blank_text=True, remove_comments=True,
                           remove_pis=True, collect_ids=False)

    def extract_tree(self, html_text: str) -> html.Element:
        return html.fromstring(html_text, parser=self.parser)

    def pre_process_html(self, html_text: str) -> str:
        return html_text

    def extract_results(self, html_text: str) -> list[Any]:
        html_text = self.pre_process_html(html_text)
        tree = self.extract_tree(html_text)
        results = []
        for item in tree.xpath(self.items_xpath):
            result = self.result_type()
            for key, value in self.elements_xpath.items():
                data = " ".join("".join(item.xpath(value)).split())
                result.__setattr__(key, data)
            results.append(result)
        return results

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        return results

    def search(self, query: str, region: str = "us-en", safesearch: str = "moderate",
               timelimit: str | None = None, page: int = 1, **kwargs: str) -> list[Any] | None:
        payload = self.build_payload(query=query, region=region, safesearch=safesearch,
                                     timelimit=timelimit, page=page, **kwargs)
        if self.search_method == "GET":
            html_text = self.request(self.search_method, self.search_url, params=payload)
        else:
            html_text = self.request(self.search_method, self.search_url, data=payload)
        if not html_text:
            return None
        return self.post_extract_results(self.extract_results(html_text))


# ─── DuckDuckGo (httpx transport) ────────────────────────────────────────────
class Duckduckgo(BaseSearchEngine):
    name = "duckduckgo"
    provider = "bing"
    search_url = "https://html.duckduckgo.com/html/"
    search_method = "POST"
    items_xpath = "//div[contains(@class, 'body')]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h2//text()", "href": "./a/@href", "body": "./a//text()",
    }
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, proxy: str | None = None, timeout: int | None = None, *, verify: bool = True) -> None:
        # DDG uses the httpx transport (primp had issues upstream).
        self.headers = {"User-Agent": _ua.random}
        self.http_client = _HttpxClient(headers=self.headers, proxy=proxy, timeout=timeout, verify=verify)  # type: ignore[assignment]
        self.results: list[Any] = []

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1, **kwargs: str) -> dict[str, Any]:
        payload = {"q": query, "b": "", "l": region}
        if page > 1:
            payload["s"] = f"{10 + (page - 2) * 15}"
        if timelimit:
            payload["df"] = timelimit
        return payload

    def request(self, *args: Any, **kwargs: Any) -> str | None:
        # httpx transport: kwargs use method= instead of positional method.
        method = args[0] if args else kwargs.pop("method", "GET")
        url = args[1] if len(args) > 1 else kwargs.pop("url", "")
        resp = self.http_client.request(method=method, url=url, **kwargs)  # type: ignore[attr-defined]
        if resp.status_code in (403, 503):
            raise MetaBlockedException(f"HTTP {resp.status_code}")
        return resp.text if resp.status_code == 200 else None

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        return [r for r in results if not r.href.startswith("https://duckduckgo.com/y.js?")]


# ─── Brave ───────────────────────────────────────────────────────────────────
class Brave(BaseSearchEngine):
    name = "brave"
    provider = "brave"
    search_url = "https://search.brave.com/search"
    search_method = "GET"
    items_xpath = "//div[@data-type='web']"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//div[(contains(@class,'title') or contains(@class,'sitename-container')) and position()=last()]//text()",
        "href": ".//a[div[contains(@class, 'title')]]/@href",
        "body": ".//div[contains(@class, 'snippet')]//div[contains(@class, 'content')]//text()",
    }

    def build_payload(self, query: str, region: str, safesearch: str,
                      timelimit: str | None, page: int = 1, **kwargs: str) -> dict[str, Any]:
        payload = {"q": query, "source": "web"}
        country, _lang = region.lower().split("-")
        cookies = {country: country, "useLocation": "0"}
        if safesearch != "moderate":
            cookies["safesearch"] = "strict" if safesearch == "on" else "off"
        self.http_client.client.set_cookies("https://search.brave.com", cookies)  # type: ignore[attr-defined]
        if timelimit:
            payload["tf"] = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}[timelimit]
        if page > 1:
            payload["offset"] = f"{page - 1}"
        return payload


# ─── Grokipedia (keyless JSON API; encyclopedic/topic queries) ───────────────
class Grokipedia(BaseSearchEngine):
    name = "grokipedia"
    provider = "grokipedia"
    priority = 1.9
    search_url = "https://grokipedia.com/api/typeahead"
    search_method = "GET"

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1,  # noqa: ARG002
                      **kwargs: str) -> dict[str, Any]:
        return {"query": query, "limit": "1"}

    def extract_results(self, html_text: str) -> list[Any]:
        data = json.loads(html_text)
        items = data.get("results", [])
        if not items:
            return []
        r = TextResult()
        r.title = items[0].get("title", "").strip("_")
        body = items[0].get("snippet", "")
        r.body = body.split("\n\n", 1)[1] if "\n\n" in body else body
        r.href = f"https://grokipedia.com/page/{items[0]['slug']}"
        return [r]


# ─── Wikipedia (opensearch API; encyclopedic/topic queries) ──────────────────
class Wikipedia(BaseSearchEngine):
    name = "wikipedia"
    provider = "wikipedia"
    priority = 2.0
    search_url = "https://{lang}.wikipedia.org/w/api.php?action=opensearch&search={query}"
    search_method = "GET"

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1,  # noqa: ARG002
                      **kwargs: str) -> dict[str, Any]:
        _country, lang = region.lower().split("-")
        self.search_url = (f"https://{lang}.wikipedia.org/w/api.php?action=opensearch"
                           f"&profile=fuzzy&limit=1&search={quote(query)}")
        self.lang = lang
        return {}

    def extract_results(self, html_text: str) -> list[Any]:
        data = json.loads(html_text)
        if not data[1]:
            return []
        r = TextResult()
        r.title = data[1][0]
        r.href = data[3][0]
        resp = self.request("GET", f"https://{self.lang}.wikipedia.org/w/api.php?action=query"
                            f"&format=json&prop=extracts&titles={quote(r.title)}&explaintext=0&exintro=0&redirects=1")
        if resp:
            pages = json.loads(resp).get("query", {}).get("pages", {})
            r.body = next(iter(pages.values())).get("extract", "")
        if "may refer to:" in r.body:
            return []
        return [r]


# ─── Yahoo (Bing-index from a different server; RU= redirect decode) ─────────
def _yahoo_extract_url(u: str) -> str:
    t = u.split("/RU=", 1)[1]
    return unquote_plus(t.split("/RK=", 1)[0].split("/RS=", 1)[0])


class Yahoo(BaseSearchEngine):
    name = "yahoo"
    provider = "bing"
    search_url = "https://search.yahoo.com/search"
    search_method = "GET"
    items_xpath = "//div[contains(@class, 'relsrch')]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//div[contains(@class, 'Title')]//h3//text()",
        "href": ".//div[contains(@class, 'Title')]//a/@href",
        "body": ".//div[contains(@class, 'Text')]//text()",
    }

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1,  # noqa: ARG002
                      **kwargs: str) -> dict[str, Any]:
        from secrets import token_urlsafe
        self.search_url = (f"https://search.yahoo.com/search;_ylt={token_urlsafe(24 * 3 // 4)}"
                           f";_ylu={token_urlsafe(47 * 3 // 4)}")
        payload = {"p": query}
        if page > 1:
            payload["b"] = f"{(page - 1) * 7 + 1}"
        if timelimit:
            payload["btf"] = timelimit
        return payload

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        out = []
        for r in results:
            if r.href.startswith("https://www.bing.com/aclick?"):
                continue
            if "/RU=" in r.href:
                r.href = _yahoo_extract_url(r.href)
            out.append(r)
        return out


# ─── Mojeek (independent index) ──────────────────────────────────────────────
# ─── Yandex ──────────────────────────────────────────────────────────────────
class Yandex(BaseSearchEngine):
    name = "yandex"
    provider = "yandex"
    search_url = "https://yandex.com/search/site/"
    search_method = "GET"
    items_xpath = "//li[contains(@class, 'serp-item')]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h3//text()", "href": ".//h3/a/@href", "body": ".//div[contains(@class, 'text')]//text()",
    }

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None,  # noqa: ARG002
                      page: int = 1, **kwargs: str) -> dict[str, Any]:
        payload = {"text": query, "web": "1", "searchid": f"{random.randint(1000000, 9999999)}"}
        if page > 1:
            payload["p"] = f"{page - 1}"
        return payload


# ─── Bing (CN + intl; free, keyless, reachable from mainland CN) ─────────────
# 国内网可用：cn.bing.com 稳定可达；国际版 www.bing.com 作为回退。
# provider 复用 "bing" 索引家族（与 DuckDuckGo/Yahoo 同源，共识合并）。
# transport 用 httpx（非 primp）：primp 的浏览器 TLS 指纹与 Bing 协商
# 偶发 SelectedUnofferedKxGroup 失败，httpx 稳定。
def _bing_decode_ck_url(href: str) -> str:
    """从 Bing ck/a 跳转链接解码真实 URL。

    形如 https://www.bing.com/ck/a?...&u=a1aHR0cHM6Ly93d3cucHl0aG9uLm9yZy8&ntb=1
    ``u=`` 参数 = "a1" + base64(URL-safe) 编码的真实 URL。解析失败返回原样。
    """
    try:
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(href).query)
        u = params.get("u", [""])[0]
        if not u:
            return href
        # 去掉 "a1" 前缀后 base64 解码（URL-safe，可能带 -_ 而非 +/）
        b64 = u[2:] if u.startswith("a1") else u
        b64 += "=" * (-len(b64) % 4)
        import base64
        decoded = base64.urlsafe_b64decode(b64).decode("utf-8", errors="replace")
        return decoded if decoded.startswith(("http://", "https://")) else href
    except Exception:
        return href


class Bing(BaseSearchEngine):
    name = "bing"
    provider = "bing"
    search_url = "https://cn.bing.com/search"
    search_method = "GET"
    items_xpath = "//li[contains(@class, 'b_algo')]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h2//text()", "href": ".//h2/a/@href", "body": ".//p//text()",
    }
    # Bing 对单 IP 高频请求随机限流（连接重置/空结果），重试可显著提高命中率
    _retries = 2
    _retry_delay = 0.6

    def __init__(self, proxy: str | None = None, timeout: int | None = None, *, verify: bool = True) -> None:
        # Bing 用标准 httpx.Client（非 _HttpxClient）：随机 ciphers / H2Patch
        # 与 Bing 偶发 TLS 重置（10054），标准客户端最稳定。
        self.headers = {"User-Agent": _ua.random}
        self.http_client = _StdHttpxClient(headers=self.headers, proxy=proxy, timeout=timeout, verify=verify)  # type: ignore[assignment]
        self.results: list[Any] = []

    def search(self, query: str, region: str = "us-en", safesearch: str = "moderate",
               timelimit: str | None = None, page: int = 1, **kwargs: str) -> list[Any] | None:
        """Bing 网络抖动/限流时重试（连接重置或空结果都重试）。"""
        import time as _time
        last: list[Any] | None = None
        for attempt in range(self._retries + 1):
            try:
                last = super().search(query, region=region, safesearch=safesearch,
                                      timelimit=timelimit, page=page, **kwargs)
            except Exception:
                last = None
            if last:
                return last
            if attempt < self._retries:
                _time.sleep(self._retry_delay)
        return last

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1, **kwargs: str) -> dict[str, Any]:
        payload = {"q": query, "ensearch": "1"}
        if page > 1:
            payload["first"] = f"{(page - 1) * 10 + 1}"
        if timelimit:
            payload["filters"] = f"ex1:\"ez{timelimit}\""
        return payload

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        """解码 Bing ck/a 跳转链接，过滤 bing 自身页面。

        Bing 结果链接形如 https://www.bing.com/ck/a?...&u=a1aHR0cHM6Ly93d3cu...
        ——真实 URL base64 编码在 ``u=`` 参数（a1 头 + base64，URL-safe）。
        """
        out = []
        for r in results:
            href = r.href.strip()
            if not href:
                continue
            if "bing.com/ck/a" in href:
                href = _bing_decode_ck_url(href)
            # 过滤 bing 自身页面
            if href and ("bing.com" in href and "/search" not in href and "/ck/a" not in href):
                continue
            r.href = href
            out.append(r)
        return out


# ─── registry ────────────────────────────────────────────────────────────────
# All enabled text backends. Bing is enabled (free/keyless, reachable from
# mainland CN without VPN). Order = rough preference; run all in parallel.
_TEXT_ENGINES: dict[str, type[BaseSearchEngine]] = {
    "duckduckgo": Duckduckgo,
    "brave": Brave,
    "grokipedia": Grokipedia,
    "wikipedia": Wikipedia,
    "yahoo": Yahoo,
    "yandex": Yandex,
    "bing": Bing,
}
# Map hound's public engine names -> metasearch backends.
_HOUND_TO_BACKEND = {
    "duckduckgo": "duckduckgo", "ddg": "duckduckgo",  # ddg is a common alias
    "bing": "bing",
    "yahoo": "yahoo", "wikipedia": "wikipedia",
    "brave": "brave", "yandex": "yandex",
    "grokipedia": "grokipedia",
}
# 国内网默认池：bing/yandex 可达无需 VPN；ddg/brave/yahoo 需 VPN。
# 保留完整池（VPN 时更多信号），但 bing 排首位作为国内稳定兜底。
_DEFAULT_BACKENDS = ["bing", "duckduckgo", "brave", "yahoo", "yandex"]


# ─── Bright Data SERP API (priority backend) ────────────────────────────────

def _brightdata_serp_search(query: str, max_results: int = 10) -> list:
    """Call Bright Data SERP API. Returns list of result objects with .title/.href/.body.

    Synchronous (blocking HTTP) — call from a thread via asyncio.to_thread().
    Returns empty list on any failure (never raises).
    """
    if not _BRIGHTDATA_API_KEY:
        return []
    try:
        import httpx
        from urllib.parse import quote_plus
        url = f"https://www.google.com/search?q={quote_plus(query)}"
        payload = {
            "zone": _BRIGHTDATA_ZONE,
            "url": url,
            "format": "json",
            "data_format": "parsed_light",
            "country": _BRIGHTDATA_COUNTRY,
        }
        resp = httpx.post(
            _BRIGHTDATA_ENDPOINT,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_BRIGHTDATA_API_KEY}",
            },
            timeout=20.0,
        )
        if resp.status_code != 200:
            logger.debug("BrightData SERP HTTP %d: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        # Bright Data wraps the SERP content in a JSON string inside "body".
        # body = '{"organic": [...], "general": {...}}' (parsed_light format).
        body_str = data.get("body", "{}") if isinstance(data, dict) else "{}"
        body = json.loads(body_str) if isinstance(body_str, str) else body_str
        items = body.get("organic", body.get("organic_results", body.get("results", [])))
        if not items and isinstance(body, list):
            items = body
        results = []
        for item in items[:max_results]:
            title = item.get("title", "")
            href = item.get("url", item.get("link", item.get("href", "")))
            snippet = item.get("snippet", item.get("description", item.get("body", "")))
            if title and href:
                # Use SimpleNamespace for attribute access compatibility with
                # the metasearch result processing loop (getattr(r, 'href')).
                from types import SimpleNamespace
                results.append(SimpleNamespace(title=title, href=href, body=snippet))
        return results
    except Exception as e:
        logger.debug("BrightData SERP error: %r", e)
        return []


# ─── circuit breaker (per-backend block cooldown) ───────────────────────────
# A backend that CAPTCHAs / 403s / rate-limits us is skipped for a cooldown so
# we don't keep firing requests at a host that is actively blocking our IP
# (which risks escalating to a longer IP-level ban, and wastes quorum slots
# waiting on a backend that will not contribute). Empty results and timeouts
# are transient and do NOT trip the breaker. Cleared on the next success.
_CIRCUIT_COOLDOWN = 60.0  # seconds
_BACKEND_HEALTH: dict[str, float] = {}  # name -> block-until timestamp
_CIRCUIT_STATE_FILE = os.path.join(os.path.expanduser("~"), ".hound", "circuit_breaker.json")


def _load_circuit_state() -> None:
    """Load persisted circuit breaker state from disk (survives restarts).
    Expired entries are discarded. Called once at module load."""
    global _BACKEND_HEALTH
    try:
        if os.path.exists(_CIRCUIT_STATE_FILE):
            import json
            with open(_CIRCUIT_STATE_FILE, "r") as f:
                data = json.load(f)
            now_ts = time()
            # Only restore entries that haven't expired yet
            _BACKEND_HEALTH = {k: v for k, v in data.items() if v > now_ts}
    except Exception:
        pass


def _save_circuit_state() -> None:
    """Persist circuit breaker state to disk. Best-effort, never raises.
    Uses atomic write (tmpfile + os.replace) to prevent corruption from
    concurrent processes."""
    try:
        os.makedirs(os.path.dirname(_CIRCUIT_STATE_FILE), exist_ok=True)
        import json
        import tempfile
        now_ts = time()
        # Only save non-expired entries
        active = {k: v for k, v in _BACKEND_HEALTH.items() if v > now_ts}
        # Atomic write: write to temp file then rename (prevents partial writes)
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(_CIRCUIT_STATE_FILE), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(active, f)
            os.replace(tmp_path, _CIRCUIT_STATE_FILE)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass


# Load persisted state at module import (once per process)
_load_circuit_state()


def _is_circuit_open(name: str) -> bool:
    return _BACKEND_HEALTH.get(name, 0.0) > time()


def _record_block(name: str) -> None:
    _BACKEND_HEALTH[name] = time() + _CIRCUIT_COOLDOWN
    _save_circuit_state()


def _record_success(name: str) -> None:
    if name in _BACKEND_HEALTH:
        _BACKEND_HEALTH.pop(name, None)
        _save_circuit_state()


def _resolve_backends(engines: Optional[list[str]]) -> list[str]:
    """Map hound engine names (or 'auto'/None) to ddgs backend names, dropping dups/unknowns."""
    if not engines:
        return list(_DEFAULT_BACKENDS)
    out: list[str] = []
    for e in engines:
        b = _HOUND_TO_BACKEND.get(e)
        if b and b not in out:
            out.append(b)
    return out or list(_DEFAULT_BACKENDS)


_SEARCH_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "ref_src", "source", "_ga", "mc_cid", "mc_eid",
    "igshid", "si",  # YouTube/social share trackers
}

_GITHUB_REPO_HOSTS = {"github.com", "www.github.com"}

_GITHUB_RESERVED_ROUTES = frozenset({
    "about",
    "apps",
    "codespaces",
    "collections",
    "dashboard",
    "explore",
    "features",
    "issues",
    "login",
    "marketplace",
    "new",
    "notifications",
    "orgs",
    "pricing",
    "pulls",
    "search",
    "security",
    "settings",
    "sponsors",
    "topics",
    "trending",
})


def _normalize_url(url: str) -> str:
    """Normalize a URL for cross-backend dedup.

    Strips tracking/analytics query params (utm_*, fbclid, gclid, ref, ...) but
    KEEPS real query params, so two results that differ only in a tracking tag
    collapse to one, while genuinely distinct pages (e.g. ?page=2 vs ?page=3)
    stay distinct. Also lowercases scheme+host and strips non-root trailing slash.
    GitHub repository owner and name are case-insensitive, so their first two
    nonempty path segments are lowercased for github.com and www.github.com;
    later path segments remain unchanged because branches and file paths can be
    case-sensitive. GitHub system routes (topics, settings, explore, ...) are
    excluded from path folding because they are not repositories and case can
    carry meaning. Credential-bearing URLs skip GitHub-specific path folding so
    opaque userinfo is never part of a newly introduced canonical collision.
    www.github.com retains its distinct host rather than adding a separate
    host-alias canonicalization policy.
    """
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    p = urlparse(u)
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower()
    path = p.path.rstrip("/") if len(p.path) > 1 else p.path
    if p.hostname in _GITHUB_REPO_HOSTS and p.username is None and p.password is None:
        segments = path.split("/")
        repo_segment_indexes = [i for i, segment in enumerate(segments) if segment][:2]
        if (
            len(repo_segment_indexes) == 2
            and segments[repo_segment_indexes[0]].lower() not in _GITHUB_RESERVED_ROUTES
        ):
            for i in repo_segment_indexes:
                segments[i] = segments[i].lower()
            path = "/".join(segments)
    if p.query:
        kept = [kv for kv in p.query.split("&")
                if kv and kv.split("=", 1)[0].lower() not in _SEARCH_TRACKING_PARAMS]
        query = "&".join(kept)
    else:
        query = ""
    return f"{scheme}://{host}{path}{('?' + query) if query else ''}"


# ─── async metasearch aggregator ─────────────────────────────────────────────
async def metasearch(
    query: str,
    max_results: int = 10,
    *,
    region: str = "us-en",
    safesearch: str = "moderate",
    timelimit: Optional[str] = None,
    page: int = 1,
    engines: Optional[list[str]] = None,
    query_map: Optional[dict[str, str]] = None,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Run the backends in PARALLEL and return (results, per-backend-status).

    results: list of {title, href, body, backend} deduped by normalized URL,
    preserving first-seen order (the backend that delivered it is its `backend`).
    status: {backend: "ok" | "empty" | "error:..."} for every backend tried.

    Early-return-on-quorum: once enough unique results have landed we cancel the
    laggards, so a healthy search returns in ~1-2s while a throttled one still
    finishes within the deadline from whichever backends got through.
    """
    backends = _resolve_backends(engines)
    status: dict[str, str] = {}
    # Rotate proxies per search call so no single IP gets rate-limited.
    _search_proxy = _get_search_proxy()
    # One engine instance per backend (cheap; primp/httpx clients are light).
    # Circuit breaker: skip backends that recently blocked us (CAPTCHA/403/rate-
    # limit) for a cooldown, so we don't keep firing at a host that is actively
    # blocking our IP. They show up in status as 'circuit_open' (-> engine_blocked).
    instances: dict[str, BaseSearchEngine] = {}
    for b in backends:
        cls = _TEXT_ENGINES.get(b)
        if not cls or cls.disabled:
            continue
        if _is_circuit_open(b):
            status[b] = "circuit_open"
            continue
        try:
            instances[b] = cls(proxy=_search_proxy, timeout=int(_SEARCH_DEADLINE), verify=True)
        except Exception as ex:  # construction failure (e.g. primp missing) -> skip
            logger.debug("engine %s init failed: %r", b, ex)
            status[b] = f"init_error:{type(ex).__name__}"

    if not instances and not _BRIGHTDATA_API_KEY:
        proxy_note = f" (proxy in use: {_search_proxy})" if _search_proxy else ""
        raise MetaSearchException(
            f"No search engines could start{proxy_note}. "
            f"Engine status: {status}. "
            f"Check HOUND_SEARCH_PROXY or set HOUND_BRIGHTDATA_API_KEY."
        )

    seen: dict[str, dict[str, Any]] = {}
    order: list[dict[str, str]] = []
    # Diversity quorum: wait for at least MIN_ENGINES backends to contribute
    # (not just enough results from one) so a single backend's bias/rate-limit
    # can't dominate - the cross-backend diversity is the robustness. A soft
    # fallback returns at SOFT_DEADLINE once we have enough results even if some
    # backends are dead/captcha'd (don't wait the full deadline for them).
    min_engines = min(3, len(instances))
    soft_deadline = 2.0
    quorum_results = max_results + 4  # a little extra for the neural reranker

    async def _run(name: str, eng: BaseSearchEngine) -> tuple[str, list[Any]]:
        # Stagger delay for rate-limit-sensitive engines (DuckDuckGo's scraping
        # interface throttles concurrent requests; a 0.3s delay avoids 429s).
        if name == "duckduckgo":
            await asyncio.sleep(0.3)
        # Per-engine query: if a query_map is provided (multi-query fan-out),
        # each engine searches its assigned variant. Falls back to the main
        # query for engines not in the map (backward-compatible).
        q = (query_map or {}).get(name, query)
        # engine.search is sync (blocking HTTP) -> offload to a thread.
        res = await asyncio.to_thread(
            eng.search, q, region, safesearch, timelimit, page,
        )
        return name, (res or [])

    tasks = {asyncio.ensure_future(_run(n, e)): n for n, e in instances.items()}

    # Bright Data SERP API: priority backend (runs in parallel with free engines).
    # When configured, it almost always returns results (no rate-limiting).
    if _BRIGHTDATA_API_KEY:
        async def _run_brightdata():
            res = await asyncio.to_thread(_brightdata_serp_search, query, max_results + 4)
            return "brightdata", res
        tasks[asyncio.ensure_future(_run_brightdata())] = "brightdata"

    pending = set(tasks)
    deadline = time() + _SEARCH_DEADLINE
    start = time()
    engines_ok = 0

    while pending and time() < deadline:
        timeout = max(0.1, deadline - time())
        try:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED, timeout=timeout)
        except Exception:
            break
        if not done:
            break
        for t in done:
            name = tasks[t]
            try:
                _, res = t.result()
            except MetaBlockedException:
                # Backend refused us (CAPTCHA/403/rate-limit) -> circuit-open it
                # for the cooldown so we stop hammering it.
                _record_block(name)
                status[name] = "blocked"
                continue
            except BaseException as ex:  # CancelledError is BaseException in py3.11+
                status[name] = f"error:{type(ex).__name__}"
                continue
            added = 0
            touched = False  # returned a valid result that matched an existing key (dupe)
            for r in res:
                if not getattr(r, "href", None) or not getattr(r, "title", None):
                    continue
                key = _normalize_url(r.href)
                if not key:
                    continue
                if key in seen:
                    # another backend already returned this URL -> record the
                    # agreement (cross-backend consensus authority signal).
                    seen[key]["backends"].add(name)
                    touched = True
                    continue
                entry = {"title": r.title, "href": r.href, "body": getattr(r, "body", "") or "",
                         "backend": name, "backends": {name}}
                seen[key] = entry
                order.append(entry)
                added += 1
            if added:
                engines_ok += 1
            # 'ok' = contributed a valid result (new OR a dupe that confirms
            # consensus). 'empty' = returned nothing usable. (A backend whose
            # only result was a dupe still contributed - it confirmed the URL.)
            if added or touched:
                status[name] = "ok"
                _record_success(name)  # backend is healthy -> clear any prior block
            else:
                status[name] = "empty"
        # early-return: enough engines contributed enough results, OR enough
        # results after the soft deadline (don't hold for dead backends).
        elapsed = time() - start
        if len(order) >= quorum_results and (
            engines_ok >= min_engines or elapsed >= soft_deadline
        ):
            for pt in pending:
                if tasks.get(pt) == "brightdata":
                    continue  # let Bright Data finish (it's a paid API, don't waste credits)
                pt.cancel()
            for pt in list(pending):
                nm = tasks[pt]
                if nm not in status:
                    status[nm] = "preempted"  # cancelled because enough backends delivered
                try:
                    await pt
                except BaseException:
                    pass
            pending = {pt for pt in pending if not pt.cancelled()}
            if not pending:
                break

    # cancel + record any still-pending (timed out) backends
    for pt in pending:
        pt.cancel()
    for pt in list(pending):
        name = tasks[pt]
        if name not in status:
            status[name] = "timeout"
        try:
            await pt
        except BaseException:
            pass

    # freeze backends sets to sorted lists for the caller
    for e in order:
        e["backends"] = sorted(e["backends"])

    # Proxy health tracking: if all engines failed with connection errors
    # (not just empty), the proxy is bad - cool it. If any engine succeeded,
    # the proxy is healthy - mark success. Only track when a proxy was used.
    if _search_proxy:
        from hound_mcp.search_proxy import get_proxy_pool
        pool = get_proxy_pool()
        if pool is not None:
            has_connection_errors = any(
                v.startswith("error:") or v == "timeout" for v in status.values()
            )
            if engines_ok == 0 and has_connection_errors:
                pool.mark_failed(_search_proxy)
            elif engines_ok > 0:
                pool.mark_success(_search_proxy)

    return order, status
