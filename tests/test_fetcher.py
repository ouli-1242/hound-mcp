"""Fetcher tests: Response class properties, CSS selector queries,
cross-platform HTML parsing, follow_redirects coercion.

Tests the real Response class against real HTML. No mocks of the class itself.
HTTPSession tests use minimal mocking only for the primp client.
"""

import pytest
from unittest.mock import MagicMock
from hound_mcp.fetcher import Response, HTTPSession, _extract_encoding


# ─── Response class ───────────────────────────────────────────────

class TestResponseClass:

    def test_properties_return_init_values(self):
        r = Response(
            url="https://example.com/page",
            body=b"<html><body>Hello</body></html>",
            status=200,
            headers={"content-type": "text/html"},
            encoding="utf-8",
            reason="OK",
            cookies={"session": "abc"},
        )
        assert r.status == 200
        assert r.url == "https://example.com/page"
        assert r.headers["content-type"] == "text/html"
        assert r.body == b"<html><body>Hello</body></html>"
        assert r.encoding == "utf-8"
        assert r.reason == "OK"
        assert r.cookies["session"] == "abc"

    def test_content_decodes_body(self):
        r = Response(url="https://x.com", body=b"Hello world", status=200)
        assert r.content == "Hello world"

    def test_content_caches_result(self):
        r = Response(url="https://x.com", body=b"Hello", status=200)
        first = r.content
        second = r.content
        assert first is second  # cached, not re-decoded

    def test_content_handles_non_utf8(self):
        r = Response(url="https://x.com", body="Hello".encode("latin-1"), status=200, encoding="latin-1")
        assert r.content == "Hello"

    def test_html_content_alias(self):
        r = Response(url="https://x.com", body=b"<p>Hi</p>", status=200)
        assert r.html_content == r.content

    def test_empty_body_returns_empty_content(self):
        r = Response(url="https://x.com", body=b"", status=200)
        assert r.content == ""

    def test_non_bytes_body_coerced_to_empty(self):
        r = Response(url="https://x.com", body=None, status=200)
        assert r.body == b""

    def test_default_headers_empty_dict(self):
        r = Response(url="https://x.com", body=b"x", status=200)
        assert r.headers == {}

    def test_default_cookies_empty_dict(self):
        r = Response(url="https://x.com", body=b"x", status=200)
        assert r.cookies == {}


# ─── CSS selector queries ──────────────────────────────────────────

class TestCSSSelectors:

    def test_css_finds_elements(self):
        html = b'<html><body><div class="main">Text</div></body></html>'
        r = Response(url="https://x.com", body=html, status=200)
        results = r.css(".main")
        assert len(results) == 1
        assert results[0].text_content() == "Text"

    def test_css_no_matches_returns_empty_list(self):
        html = b'<html><body><div>Text</div></body></html>'
        r = Response(url="https://x.com", body=html, status=200)
        results = r.css(".nonexistent")
        assert results == []

    def test_css_finds_multiple_elements(self):
        html = b'<html><body><p>A</p><p>B</p><p>C</p></body></html>'
        r = Response(url="https://x.com", body=html, status=200)
        results = r.css("p")
        assert len(results) == 3

    def test_css_selector_on_subtree(self):
        html = b'<html><body><div class="container"><p>Inner</p></div><p>Outer</p></body></html>'
        r = Response(url="https://x.com", body=html, status=200)
        container = r.css(".container")
        assert len(container) == 1
        inner = container[0].css("p")
        assert len(inner) == 1
        assert inner[0].text_content() == "Inner"

    def test_invalid_css_selector_raises_error(self):
        # PR #11.1.5: errors propagate, not silently swallowed
        html = b'<html><body><div>Text</div></body></html>'
        r = Response(url="https://x.com", body=html, status=200)
        from lxml.cssselect import SelectorSyntaxError
        with pytest.raises(SelectorSyntaxError):
            r.css("div > >")

    def test_element_wrapper_url_propagated(self):
        html = b'<html><body><div class="x">Y</div></body></html>'
        r = Response(url="https://example.com/page", body=html, status=200)
        el = r.css(".x")[0]
        assert el.url == "https://example.com/page"


# ─── Cross-platform HTML parsing ──────────────────────────────────

class TestHTMLParsing:

    def test_full_html_document_parsed_correctly(self):
        html = b"""<!DOCTYPE html><html><head><title>Test</title></head>
        <body><div class="content">Hello</div></body></html>"""
        r = Response(url="https://x.com", body=html, status=200)
        results = r.css(".content")
        assert len(results) == 1
        assert results[0].text_content() == "Hello"

    def test_html_without_doctype_parsed(self):
        html = b'<html><body><p>No doctype</p></body></html>'
        r = Response(url="https://x.com", body=html, status=200)
        assert len(r.css("p")) == 1

    def test_fragment_html_parsed(self):
        html = b'<div><p>Fragment</p></div>'
        r = Response(url="https://x.com", body=html, status=200)
        assert len(r.css("p")) == 1

    def test_empty_body_does_not_crash(self):
        r = Response(url="https://x.com", body=b"", status=200)
        assert r.css("div") == []


# ─── Encoding extraction ──────────────────────────────────────────

class TestExtractEncoding:

    def test_extracts_utf8(self):
        assert _extract_encoding("text/html; charset=utf-8") == "utf-8"

    def test_extracts_latin1(self):
        assert _extract_encoding("text/html; charset=iso-8859-1") == "iso-8859-1"

    def test_returns_utf8_for_empty(self):
        assert _extract_encoding("") == "utf-8"

    def test_returns_utf8_for_no_charset(self):
        assert _extract_encoding("application/json") == "utf-8"

    def test_handles_quoted_charset(self):
        assert _extract_encoding('text/html; charset="utf-8"') == "utf-8"


# ─── HTTPSession follow_redirects coercion (v11.0.2 fix) ──────────

class TestFollowRedirectsCoercion:

    @pytest.mark.asyncio
    async def test_string_safe_coerced_to_follow(self):
        # scrapling-style "safe" -> 手动跟随（primp 收 follow_redirects=False，
        # 由 HTTPSession 逐跳校验目标 URL 后自行跟随）
        session = HTTPSession()
        session._client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.content = b"ok"
        mock_resp.url = "https://example.com"
        mock_resp.reason = "OK"
        mock_resp.cookies = []
        session._client.get = MagicMock(return_value=mock_resp)

        await session.get("https://example.com", follow_redirects="safe")
        # primp 收到 False（手动跟随由本层实现，重定向目标逐跳 SSRF 校验）
        call_kwargs = session._client.get.call_args
        assert call_kwargs.kwargs.get("follow_redirects") is False

    @pytest.mark.asyncio
    async def test_string_never_coerced_to_false(self):
        session = HTTPSession()
        session._client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.content = b"ok"
        mock_resp.url = "https://example.com"
        mock_resp.reason = "OK"
        mock_resp.cookies = []
        session._client.get = MagicMock(return_value=mock_resp)

        await session.get("https://example.com", follow_redirects="never")
        call_kwargs = session._client.get.call_args
        assert call_kwargs.kwargs.get("follow_redirects") is False

    @pytest.mark.asyncio
    async def test_bool_passed_through(self):
        session = HTTPSession()
        session._client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.content = b"ok"
        mock_resp.url = "https://example.com"
        mock_resp.reason = "OK"
        mock_resp.cookies = []
        session._client.get = MagicMock(return_value=mock_resp)

        await session.get("https://example.com", follow_redirects=False)
        call_kwargs = session._client.get.call_args
        assert call_kwargs.kwargs.get("follow_redirects") is False
