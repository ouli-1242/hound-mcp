"""Server core tests: JS shell detection, content issue detection, cacheability,
agent hints, chunking, Cloudflare detection, CF challenge signals.

Tests the REAL signal-detection functions (_is_js_shell, _detect_content_issue,
_is_cacheable, _agent_hints, _apply_chunking, _is_cloudflare_from_response)
against real ResponseModel objects. No mocks of the functions themselves.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from hound_mcp.server import (
    MasterFetchServer,
    ResponseModel, BulkResponseModel, _is_js_shell, _detect_content_issue, _is_cacheable,
    _agent_hints, _apply_chunking, _is_cloudflare_from_response,
    _annotate_quality, _with_agent_hints, MAX_CONTENT_CHARS, MIN_CHUNK_CHARS, MAX_BULK_URLS,
    _JS_SHELL_SIGNALS, _CF_CHALLENGE_SIGNALS, MAX_RESPONSE_BYTES,
    _browser_deps_available,
    _strict_options, _SF_OPTIONS_ALLOWED, _SF_OPTIONS_FORWARDED,
    _SHOT_OPTIONS,
)


def _make_result(**kwargs):
    """Build a ResponseModel with sensible defaults for testing."""
    defaults = dict(
        status=200, content=["Hello world"], url="https://example.com",
        fetcher_used="http", content_type="text/html",
        total_size_bytes=1000, extracted_type="markdown",
    )
    defaults.update(kwargs)
    return ResponseModel(**defaults)


# ─── JS shell detection ───────────────────────────────────────────

class TestIsJsShell:

    def test_empty_content_is_js_shell(self):
        result = _make_result(content=[], status=200)
        assert _is_js_shell(result) is True

    def test_blank_content_is_js_shell(self):
        result = _make_result(content=["   "], status=200)
        assert _is_js_shell(result) is True

    def test_real_content_not_js_shell(self):
        result = _make_result(content=["Real article text about Python."], status=200)
        assert _is_js_shell(result) is False

    def test_enable_javascript_signal(self):
        result = _make_result(content=["Please enable JavaScript to run this app."], status=200)
        assert _is_js_shell(result) is True

    def test_javascript_disabled_signal(self):
        result = _make_result(content=["JavaScript is disabled in this browser."], status=200)
        assert _is_js_shell(result) is True

    def test_large_body_low_text_is_shell(self):
        # Large HTML body but almost no extractable text -> JS shell
        result = _make_result(
            content=["hi"], status=200, fetcher_used="http",
            total_size_bytes=5000,
        )
        assert _is_js_shell(result) is True

    def test_stealthy_large_body_low_text_not_shell(self):
        # Stealthy result with little text is a real low-text page, not a shell
        result = _make_result(
            content=["hi"], status=200, fetcher_used="stealthy",
            total_size_bytes=5000,
        )
        assert _is_js_shell(result) is False

    def test_cf_challenge_detected_in_200(self):
        # CF Turnstile challenge pages return 200 with CF markers
        result = _make_result(
            content=["<script>challenges.cloudflare.com/turnstile</script>"],
            status=200, fetcher_used="http",
        )
        assert _is_js_shell(result) is True

    def test_cf_turnstile_marker_detected(self):
        result = _make_result(
            content=["<div class='cf-turnstile'></div>"],
            status=200, fetcher_used="http",
        )
        assert _is_js_shell(result) is True

    def test_cf_chl_opt_detected(self):
        result = _make_result(
            content=["var cf_chl_opt = {};"],
            status=200, fetcher_used="http",
        )
        assert _is_js_shell(result) is True

    def test_normal_page_with_word_javascript_not_shell(self):
        # A page that mentions "javascript" but has real content
        result = _make_result(
            content=["This article discusses JavaScript frameworks and their performance."],
            status=200, fetcher_used="http", total_size_bytes=1000,
        )
        assert _is_js_shell(result) is False


# ─── Content issue detection ───────────────────────────────────────

class TestDetectContentIssue:

    def test_clean_content_no_issue(self):
        result = _make_result()
        assert _detect_content_issue(result) == ""

    def test_js_shell_detected(self):
        result = _make_result(content=[], status=200)
        assert "js_shell" in _detect_content_issue(result)

    def test_geo_redirect_detected(self):
        result = _make_result(content=["Choose your country to continue shopping"])
        assert "geo_redirect" in _detect_content_issue(result)

    def test_auth_wall_detected(self):
        # zhihu.com redirects to /signin and serves only the login form
        result = _make_result(
            url="https://www.zhihu.com/signin?next=%2F",
            content=["知乎 - 有问题，就会有答案", "验证码登录", "密码登录",
                     "获取短信验证码", "登录/注册"],
        )
        assert "auth_wall" in _detect_content_issue(result)

    def test_auth_wall_not_detected_on_normal_nav(self):
        # A stray "Sign in" link in the navbar is not a wall
        result = _make_result(content=["Home", "Sign in", "About us", "Contact"])
        assert "auth_wall" not in _detect_content_issue(result)

    def test_auth_wall_login_path_with_single_signal(self):
        result = _make_result(url="https://example.com/login", content=["Home", "Sign in"])
        assert "auth_wall" in _detect_content_issue(result)

    def test_pdf_no_text_is_not_js_shell(self):
        # Scanned PDF with no text layer: distinct pdf_no_text error, never js_shell
        result = _make_result(
            content=["> OCR-extracted from scanned PDF · 1 pages", "--- Page 1 ---",
                     "[No text detected on this page.]"],
            content_type="application/pdf; qs=0.001",
            total_size_bytes=13264,
        )
        issue = _detect_content_issue(result)
        assert "pdf_no_text" in issue
        assert "js_shell" not in issue

    def test_pdf_with_text_has_no_issue(self):
        result = _make_result(
            content=["Real PDF text content here."],
            content_type="application/pdf",
        )
        assert _detect_content_issue(result) == ""

    def test_pdf_with_http_error_still_reports_error(self):
        result = _make_result(status=404, content=["Not Found"], content_type="application/pdf")
        assert "http_error_404" in _detect_content_issue(result)

    def test_http_404_error(self):
        result = _make_result(status=404, content=["404 Not Found"])
        assert "http_error_404" in _detect_content_issue(result)

    def test_http_500_error(self):
        result = _make_result(status=500, content=["Internal Server Error"])
        assert "http_error_500" in _detect_content_issue(result)

    def test_network_error(self):
        # status=0 with content -> js_shell check triggers first (empty content)
        # Test with non-empty content and status=0
        result = _make_result(status=0, content=["connection refused"], error="")
        issue = _detect_content_issue(result)
        assert "network_error" in issue or "http_error" in issue

    def test_cf_challenge_on_403(self):
        result = _make_result(status=403, content=["Checking your browser. Cloudflare."])
        assert "bot_challenge" in _detect_content_issue(result)

    def test_cf_challenge_on_503(self):
        result = _make_result(status=503, content=["Please verify you are a human."])
        assert "bot_challenge" in _detect_content_issue(result)

    def test_cf_mention_on_200_not_challenge(self):
        # A 200 page about Cloudflare security is NOT a bot challenge
        result = _make_result(status=200, content=["This article about Cloudflare CDN..."])
        assert "bot_challenge" not in _detect_content_issue(result)


# ─── Cloudflare detection ─────────────────────────────────────────

class TestCloudflareDetection:

    def test_200_not_cloudflare(self):
        result = _make_result(status=200, content=["cloudflare mentions"])
        assert _is_cloudflare_from_response(result) is False

    def test_403_with_cloudflare_signal(self):
        result = _make_result(status=403, content=["Cloudflare challenge page"])
        assert _is_cloudflare_from_response(result) is True

    def test_503_with_datadome_signal(self):
        result = _make_result(status=503, content=["datadome captcha-delivery.com"])
        assert _is_cloudflare_from_response(result) is True

    def test_200_not_checked(self):
        result = _make_result(status=200, content=["ray id: abc123"])
        assert _is_cloudflare_from_response(result) is False


# ─── Cacheability ──────────────────────────────────────────────────

class TestIsCacheable:

    def test_clean_200_cacheable(self):
        result = _make_result()
        assert _is_cacheable(result) is True

    def test_404_not_cacheable(self):
        result = _make_result(status=404, error="http_error_404")
        assert _is_cacheable(result) is False

    def test_error_not_cacheable(self):
        result = _make_result(error="js_shell_detected")
        assert _is_cacheable(result) is False

    def test_empty_content_not_cacheable(self):
        result = _make_result(content=[], status=200)
        assert _is_cacheable(result) is False

    def test_blank_content_not_cacheable(self):
        result = _make_result(content=["  "], status=200)
        assert _is_cacheable(result) is False

    def test_3xx_cacheable(self):
        result = _make_result(status=301, content=["redirected"])
        assert _is_cacheable(result) is True


# ─── Agent hints ───────────────────────────────────────────────────

class TestAgentHints:

    def test_clean_result_summary(self):
        result = _make_result()
        summary, next_action, content_ok = _agent_hints(result)
        assert "200" in summary
        assert "OK" in summary
        assert content_ok is True
        assert next_action == ""

    def test_truncated_result_next_action(self):
        result = _make_result(is_truncated=True, next_offset=40000)
        summary, next_action, content_ok = _agent_hints(result)
        assert "truncated" in summary
        assert "offset=40000" in next_action
        assert "focus=" in next_action  # v11.2: suggest focus= first

    def test_error_result_content_ok_false(self):
        result = _make_result(status=404, error="http_error_404")
        summary, next_action, content_ok = _agent_hints(result)
        assert content_ok is False
        assert "failed" in next_action.lower()
        assert next_action  # should have actionable guidance

    def test_network_error_summary(self):
        result = _make_result(status=0, error="network_error")
        summary, _, _ = _agent_hints(result)
        assert "network error" in summary

    def test_cached_result_in_summary(self):
        result = _make_result(cached=True)
        summary, _, _ = _agent_hints(result)
        assert "cached" in summary

    def test_js_shell_next_action(self):
        result = _make_result(error="js_shell_detected: placeholder")
        _, next_action, _ = _agent_hints(result)
        assert "stealthy" in next_action

    def test_bot_challenge_next_action(self):
        result = _make_result(error="bot_challenge_detected: cf page")
        _, next_action, _ = _agent_hints(result)
        assert "stealthy" in next_action

    def test_large_pdf_suggests_focus_or_pages(self):
        result = _make_result(
            page_type="pdf", total_extracted_chars=50000,
            content=["A" * 100], status=200,
        )
        _, next_action, _ = _agent_hints(result)
        assert "focus=" in next_action
        assert "pages=" in next_action

    def test_short_pdf_no_focus_hint(self):
        result = _make_result(
            page_type="pdf", total_extracted_chars=5000,
            content=["A" * 100], status=200,
        )
        _, next_action, _ = _agent_hints(result)
        assert "focus=" not in next_action

        result = _make_result(page_type="list", links={
            "citations": [{"url": "https://example.com/page1", "text": "P1"}]
        })
        _, next_action, _ = _agent_hints(result)
        assert "list page" in next_action.lower()
        assert "example.com/page1" in next_action

    def test_auth_wall_next_action(self):
        result = _make_result(page_type="auth_wall")
        _, next_action, _ = _agent_hints(result)
        assert "login" in next_action.lower() or "authentication" in next_action.lower()

    def test_stale_content_next_action(self):
        result = _make_result(page_type="article", is_stale=True, content_age_days=500)
        _, next_action, _ = _agent_hints(result)
        assert "500" in next_action
        assert "outdated" in next_action.lower() or "search" in next_action.lower()


# ─── Chunking ──────────────────────────────────────────────────────

class TestChunking:

    def test_short_content_not_truncated(self):
        result = _make_result(content=["Short content"])
        chunked = _apply_chunking(result)
        assert chunked.is_truncated is False
        assert chunked.next_offset == 0

    def test_long_content_truncated(self):
        long_text = "A" * (MAX_CONTENT_CHARS + 1000)
        result = _make_result(content=[long_text])
        chunked = _apply_chunking(result)
        assert chunked.is_truncated is True
        assert chunked.next_offset == MAX_CONTENT_CHARS
        assert chunked.total_extracted_chars > MAX_CONTENT_CHARS

    def test_offset_retrieves_next_chunk(self):
        long_text = "A" * (MAX_CONTENT_CHARS + 1000)
        result = _make_result(content=[long_text])
        first = _apply_chunking(result, offset=0)
        second = _apply_chunking(result, offset=first.next_offset)
        assert second.content[0].startswith("A")

    def test_offset_past_end_returns_no_more(self):
        result = _make_result(content=["Short content"])
        chunked = _apply_chunking(result, offset=99999)
        assert chunked.is_truncated is False
        assert "No more content" in chunked.content[0]

    def test_smart_merge_small_remaining(self):
        # Content slightly over MAX_CONTENT_CHARS: remaining is small -> not truncated
        long_text = "A" * (MAX_CONTENT_CHARS + MIN_CHUNK_CHARS - 10)
        result = _make_result(content=[long_text])
        chunked = _apply_chunking(result)
        # Remaining (MIN_CHUNK_CHARS - 10) < MIN_CHUNK_CHARS -> merged into one chunk
        assert chunked.is_truncated is False

    def test_chunking_preserves_envelope_fields(self):
        result = _make_result(
            url="https://github.com/user/repo",
            metadata={"title": "Test"}, page_type="article",
            quality_score=0.9,
        )
        chunked = _apply_chunking(result)
        assert chunked.metadata["title"] == "Test"
        assert chunked.page_type == "article"
        assert chunked.source_type == "github"  # recomputed from URL
        assert chunked.quality_score == 0.9

    def test_chunking_stamps_fetched_at(self):
        result = _make_result()
        chunked = _apply_chunking(result)
        assert chunked.fetched_at != ""

    def test_chunking_stamps_content_ok(self):
        result = _make_result()
        chunked = _apply_chunking(result)
        assert chunked.content_ok is True


# ─── Annotate quality ─────────────────────────────────────────────

class TestAnnotateQuality:

    def test_sets_error_on_js_shell(self):
        result = _make_result(content=[])
        annotated = _annotate_quality(result)
        assert "js_shell" in annotated.error

    def test_does_not_overwrite_existing_error(self):
        result = _make_result(error="custom error")
        annotated = _annotate_quality(result)
        assert annotated.error == "custom error"

    def test_clean_result_no_error_set(self):
        result = _make_result()
        annotated = _annotate_quality(result)
        assert annotated.error == ""


# ─── _with_agent_hints (envelope enrichment) ───────────────────────

class TestWithAgentHints:
    """_with_agent_hints() stamps content_ok, summary, fetched_at, page_type,
    source_type, next_action on every ResponseModel."""

    def test_sets_summary(self):
        result = _with_agent_hints(_make_result())
        assert "200" in result.summary
        assert "OK" in result.summary

    def test_sets_content_ok_true(self):
        result = _with_agent_hints(_make_result())
        assert result.content_ok is True

    def test_sets_fetched_at(self):
        result = _with_agent_hints(_make_result())
        assert result.fetched_at != ""
        assert "T" in result.fetched_at  # ISO-8601 format

    def test_error_result_content_ok_false(self):
        result = _with_agent_hints(_make_result(status=404, error="http_error_404"))
        assert result.content_ok is False

    def test_classifies_source(self):
        result = _with_agent_hints(_make_result(url="https://docs.python.org/3/"))
        assert result.source_type == "docs-site"
        assert result.is_official is True

    def test_github_is_official(self):
        result = _with_agent_hints(_make_result(url="https://github.com/python/cpython"))
        assert result.source_type == "github"
        assert result.is_official is True

    def test_unknown_domain_defaults(self):
        result = _with_agent_hints(_make_result(url="https://random-site-123.com/page"))
        assert result.source_type == "unknown"
        assert result.is_official is False

    def test_preserves_existing_metadata(self):
        result = _with_agent_hints(_make_result(
            metadata={"title": "My Title", "author": "Test"},
            links={"citations": [{"url": "https://example.com", "text": "Ex"}]},
        ))
        assert result.metadata["title"] == "My Title"
        assert result.metadata["author"] == "Test"
        assert len(result.links["citations"]) == 1

    def test_sets_next_action_for_list_page(self):
        result = _with_agent_hints(_make_result(
            page_type="list",
            links={"citations": [{"url": "https://example.com/p1", "text": "P1"}]},
        ))
        assert "list page" in result.next_action.lower()

    def test_sets_next_action_for_auth_wall(self):
        result = _with_agent_hints(_make_result(page_type="auth_wall"))
        assert "login" in result.next_action.lower()

    def test_summary_uses_extracted_chars_not_raw_size(self):
        """P2 regression: summary size should use total_extracted_chars,
        not total_size_bytes (raw HTML body)."""
        result = _with_agent_hints(_make_result(
            total_size_bytes=100000,    # 100KB raw HTML
            total_extracted_chars=15000,  # 15KB extracted text
            content=["A" * 15000],
        ))
        # Size in summary should be ~15KB, not ~100KB
        # _format_size(15000) = "14.6KB"
        assert "100" not in result.summary.split("·")[1]  # not 100KB


# ─── _finalize_result (annotate + cache + chunk + envelope) ─────────

class TestFinalizeResult:
    """_finalize_result() orchestrates _annotate_quality + cache + chunking."""

    @pytest.mark.asyncio
    @patch("hound_mcp.server.set_cached", new_callable=AsyncMock)
    async def test_sets_envelope_fields(self, mock_set_cached):
        srv = MasterFetchServer(cache_ttl=0)
        result = _make_result()
        finalized = await srv._finalize_result(
            result, "https://example.com", "markdown", None, 0,
        )
        assert finalized.summary != ""
        assert finalized.content_ok is True
        assert finalized.fetched_at != ""
        assert finalized.page_type is not None
        mock_set_cached.assert_not_called()  # cache_ttl=0 → no cache write

    @pytest.mark.asyncio
    @patch("hound_mcp.server.set_cached", new_callable=AsyncMock)
    async def test_cacheable_writes_cache(self, mock_set_cached):
        srv = MasterFetchServer(cache_ttl=3600)
        result = _make_result()
        finalized = await srv._finalize_result(
            result, "https://example.com", "markdown", None, 3600,
        )
        assert finalized.summary != ""
        mock_set_cached.assert_called_once()

    @pytest.mark.asyncio
    @patch("hound_mcp.server.set_cached", new_callable=AsyncMock)
    async def test_annotate_quality_runs(self, mock_set_cached):
        """JS shell content should be detected before caching."""
        srv = MasterFetchServer(cache_ttl=3600)
        result = _make_result(content=[], status=200)  # empty = JS shell
        finalized = await srv._finalize_result(
            result, "https://example.com", "markdown", None, 3600,
        )
        assert "js_shell" in finalized.error
        assert finalized.content_ok is False
        mock_set_cached.assert_not_called()  # JS shell not cacheable

    @pytest.mark.asyncio
    @patch("hound_mcp.server.set_cached", new_callable=AsyncMock)
    async def test_chunking_applied(self, mock_set_cached):
        srv = MasterFetchServer(cache_ttl=0)
        long = "A" * (MAX_CONTENT_CHARS + 1000)
        result = _make_result(content=[long])
        finalized = await srv._finalize_result(
            result, "https://example.com", "markdown", None, 0,
        )
        assert finalized.is_truncated is True
        assert finalized.next_offset == MAX_CONTENT_CHARS
        assert finalized.total_extracted_chars == len(long)


# ─── bulk_get envelope fields ──────────────────────────────────────

class TestBulkGetEnvelope:
    """bulk_get() returns ResponseModels with full envelope.
    Regression test for P0: get()/bulk_get() must call _with_agent_hints()."""

    @staticmethod
    def _mock_http_response(status=200, body=b"<html><body><p>Test</p></body></html>",
                            content_type="text/html", url="https://example.com"):
        """Create a minimal mock HTTP response object."""
        m = MagicMock()
        m.status = status
        m.body = body
        m.headers = {"content-type": content_type}
        m.url = url
        m.encoding = "utf-8"
        return m

    @pytest.mark.asyncio
    @patch("hound_mcp.fetcher.HTTPSession")
    async def test_success_path_has_envelope(self, mock_http_session):
        """A successful bulk_get() result must have content_ok, summary, fetched_at."""
        mock_session = AsyncMock()
        mock_session.get.return_value = self._mock_http_response()
        mock_http_session.return_value.__aenter__.return_value = mock_session
        mock_http_session.return_value.__aexit__.return_value = None

        bulk = await MasterFetchServer.bulk_get(
            urls=["https://example.com"],
            extraction_type="markdown",
        )
        assert bulk.total == 1
        result = bulk.results[0]
        assert result.summary != "", "summary should be set by _with_agent_hints"
        assert result.content_ok is True, "content_ok should be True for clean 200"
        assert result.fetched_at != "", "fetched_at should be ISO timestamp"
        assert result.url == "https://example.com"

    @pytest.mark.asyncio
    @patch("hound_mcp.fetcher.HTTPSession")
    async def test_error_path_has_envelope(self, mock_http_session):
        """A network error from bulk_get() must still have summary + content_ok."""
        mock_session = AsyncMock()
        mock_session.get.side_effect = RuntimeError("Connection refused")
        mock_http_session.return_value.__aenter__.return_value = mock_session
        mock_http_session.return_value.__aexit__.return_value = None

        bulk = await MasterFetchServer.bulk_get(
            urls=["https://example.com"],
            extraction_type="markdown",
            timeout=5,
        )
        assert bulk.total == 1
        result = bulk.results[0]
        assert result.summary != "", "error path should still have summary"
        assert result.content_ok is False, "error path content_ok should be False"
        assert result.fetched_at != "", "error path should still have fetched_at"
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    @patch("hound_mcp.fetcher.HTTPSession")
    async def test_page_type_detected(self, mock_http_session):
        """page_type should be detected from HTML content."""
        mock_session = AsyncMock()
        mock_session.get.return_value = self._mock_http_response(
            body=b"<html><body><article><h1>Article</h1><p>Content</p></article></body></html>",
        )
        mock_http_session.return_value.__aenter__.return_value = mock_session
        mock_http_session.return_value.__aexit__.return_value = None

        bulk = await MasterFetchServer.bulk_get(
            urls=["https://example.com/article"],
            extraction_type="markdown",
        )
        result = bulk.results[0]
        # <article> tag → page_type should be "article"
        assert result.page_type == "article", f"expected article, got {result.page_type}"

    @pytest.mark.asyncio
    @patch("hound_mcp.fetcher.HTTPSession")
    async def test_source_type_from_url(self, mock_http_session):
        """source_type should be classified from URL."""
        mock_session = AsyncMock()
        mock_session.get.return_value = self._mock_http_response(url="https://github.com/user/repo")
        mock_http_session.return_value.__aenter__.return_value = mock_session
        mock_http_session.return_value.__aexit__.return_value = None

        bulk = await MasterFetchServer.bulk_get(
            urls=["https://github.com/user/repo"],
            extraction_type="markdown",
        )
        result = bulk.results[0]
        assert result.source_type == "github"
        assert result.is_official is True


# ─── Stealthy proxy bypass ─────────────

class TestSmartFetchProxy:

    @pytest.mark.asyncio
    async def test_forced_stealthy_proxy_bypasses_direct_auto_session(self):
        server = MasterFetchServer()
        server._ensure_auto_session = AsyncMock(return_value="direct-session")
        server.stealthy_fetch = AsyncMock(
            return_value=_make_result(fetcher_used="stealthy")
        )
        server._finalize_result = AsyncMock(side_effect=lambda result, *args: result)

        await server.smart_fetch(
            "https://example.com",
            force_fetcher="stealthy",
            proxy="http://127.0.0.1:8080",
            cache_ttl=0,
        )

        server._ensure_auto_session.assert_not_awaited()
        assert server.stealthy_fetch.await_args.kwargs["session_id"] is None

    @pytest.mark.asyncio
    async def test_auto_escalation_proxy_bypasses_direct_auto_session(self):
        server = MasterFetchServer()
        server.get = AsyncMock(
            return_value=_make_result(status=403, content=["Forbidden"])
        )
        server._ensure_auto_session = AsyncMock(return_value="direct-session")
        server.stealthy_fetch = AsyncMock(
            return_value=_make_result(fetcher_used="stealthy")
        )
        server._finalize_result = AsyncMock(side_effect=lambda result, *args: result)

        with patch("hound_mcp.server._browser_deps_available", return_value=True):
            await server.smart_fetch(
                "https://example.com",
                proxy="http://127.0.0.1:8080",
                cache_ttl=0,
            )

        server._ensure_auto_session.assert_not_awaited()
        assert server.stealthy_fetch.await_args.kwargs["session_id"] is None


# ─── Focus context scoping ─────────────────────────

class TestSmartFetchFocusContext:
    """smart_fetch request options must be scoped per invocation: focus set in
    one call must not leak into the next, and bulk fetches must forward focus.
    """

    @staticmethod
    def _long_content_result():
        return _make_result(content=[
            "## Apples\nalpha fruit\n\n"
            "## Bananas\nyellow fruit\n\n"
            "## Carrots\norange vegetable"
        ])

    @pytest.mark.asyncio
    async def test_bulk_fetch_forwards_focus_to_each_result(self):
        server = MasterFetchServer(cache_ttl=0)
        server.get = AsyncMock(return_value=self._long_content_result())

        result = await server.smart_fetch(
            "https://example.com",
            urls=["https://example.com"],
            focus="bananas",
            cache_ttl=0,
        )

        assert isinstance(result, BulkResponseModel)
        content = result.results[0].content[0]
        assert "[Focus: 'bananas'" in content
        assert "## Bananas" in content
        assert "## Apples" not in content

    @pytest.mark.asyncio
    async def test_no_focus_call_does_not_inherit_previous_focus(self):
        server = MasterFetchServer(cache_ttl=0)
        server.get = AsyncMock(
            side_effect=[self._long_content_result(), self._long_content_result()]
        )

        focused = await server.smart_fetch(
            "https://example.com/first", focus="bananas", cache_ttl=0
        )
        unfiltered = await server.smart_fetch(
            "https://example.com/second", cache_ttl=0
        )

        assert "[Focus: 'bananas'" in focused.content[0]
        assert "[Focus:" not in unfiltered.content[0]
        assert "## Apples" in unfiltered.content[0]
        assert "## Bananas" in unfiltered.content[0]


# ─── Constants and signals ─────────────────────────────────────────

class TestConstants:

    def test_max_content_chars_reasonable(self):
        assert 10000 < MAX_CONTENT_CHARS < 100000

    def test_min_chunk_chars_reasonable(self):
        assert 100 < MIN_CHUNK_CHARS < 2000

    def test_max_bulk_urls_prevents_dos(self):
        assert MAX_BULK_URLS == 100

    def test_max_response_bytes_prevents_dos(self):
        assert MAX_RESPONSE_BYTES == 50 * 1024 * 1024

    def test_js_shell_signals_not_empty(self):
        assert len(_JS_SHELL_SIGNALS) > 5

    def test_cf_challenge_signals_defined(self):
        assert "cf-turnstile" in _CF_CHALLENGE_SIGNALS
        assert "challenges.cloudflare.com/turnstile" in _CF_CHALLENGE_SIGNALS
        assert "cf_chl_opt" in _CF_CHALLENGE_SIGNALS
        assert "__cf_chl" in _CF_CHALLENGE_SIGNALS
        assert "challenge-platform" in _CF_CHALLENGE_SIGNALS
        assert "cf-mitigated" in _CF_CHALLENGE_SIGNALS


# ─── Event-loop safety: browser availability check must never block ──
# Regression test for issue #11: _browser_deps_available() called on the
# asyncio event loop triggered `import patchright` synchronously, blocking
# the loop for 1-3s and starving the MCP initialize handshake (-32001).
# The fix: _browser_deps_available() reads only the cache (never imports).
# The prewarm thread populates the cache via check_browser_available().

from hound_mcp.browser import check_browser_available, is_browser_available_cached
import time as _time


class TestBrowserDepsNonBlocking:
    """Verify _browser_deps_available() never blocks the event loop."""

    def test_returns_true_when_cache_unset(self):
        """When cache is None (prewarm hasn't run), return True instantly.

        This is the optimistic default: if patchright isn't installed, the
        browser operation will raise ImportError and the tool handler catches
        it. But the availability check itself never blocks.
        """
        import hound_mcp.browser as bmod
        import hound_mcp.server as srv
        original = bmod._browser_available
        bmod._browser_available = None
        srv._browser_import_error = None
        try:
            t0 = _time.monotonic()
            result = srv._browser_deps_available()
            elapsed = _time.monotonic() - t0
            assert result is True
            assert elapsed < 0.001  # < 1ms = no blocking import
        finally:
            bmod._browser_available = original

    def test_returns_cached_true_instantly(self):
        """When cache is True, return True instantly without re-importing."""
        import hound_mcp.browser as bmod
        original = bmod._browser_available
        bmod._browser_available = True
        try:
            t0 = _time.monotonic()
            result = _browser_deps_available()
            elapsed = _time.monotonic() - t0
            assert result is True
            assert elapsed < 0.001
        finally:
            bmod._browser_available = original

    def test_returns_cached_false_instantly(self):
        """When cache is False, return False and set error, instantly."""
        import hound_mcp.browser as bmod
        import hound_mcp.server as srv
        original_avail = bmod._browser_available
        original_err = bmod._browser_import_error
        bmod._browser_available = False
        bmod._browser_import_error = "patchright not found"
        srv._browser_import_error = None
        try:
            t0 = _time.monotonic()
            result = _browser_deps_available()
            elapsed = _time.monotonic() - t0
            assert result is False
            assert srv._browser_import_error == "patchright not found"
            assert elapsed < 0.001
        finally:
            bmod._browser_available = original_avail
            bmod._browser_import_error = original_err

    def test_is_browser_available_cached_reads_without_import(self):
        """is_browser_available_cached() returns the cache or None, never imports."""
        import hound_mcp.browser as bmod
        original = bmod._browser_available
        bmod._browser_available = None
        try:
            assert is_browser_available_cached() is None
        finally:
            bmod._browser_available = original

        bmod._browser_available = True
        try:
            assert is_browser_available_cached() is True
        finally:
            bmod._browser_available = original

        bmod._browser_available = False
        try:
            assert is_browser_available_cached() is False
        finally:
            bmod._browser_available = original

    def test_check_browser_available_caches_result(self):
        """check_browser_available() populates the cache (first call does import)."""
        import hound_mcp.browser as bmod
        original = bmod._browser_available
        bmod._browser_available = None
        try:
            result = check_browser_available()
            # After first call, cache must be populated (not None)
            assert bmod._browser_available is not None
            assert result == bmod._browser_available
        finally:
            bmod._browser_available = original

    def test_prewarm_does_browser_check_in_thread(self):
        """Verify _prewarm_stealthy offloads browser check to a worker thread.

        This is the core regression test for issue #11: the old code called
        _browser_deps_available() on the event loop before asyncio.to_thread,
        which blocked the loop. The fix moves the entire check into the thread.
        """
        import inspect
        from hound_mcp.server import MasterFetchServer
        src = inspect.getsource(MasterFetchServer._prewarm_stealthy)
        # The _warm inner function must use asyncio.to_thread for the browser check
        warm_body = src.split("async def _warm")[1] if "async def _warm" in src else src
        assert "asyncio.to_thread" in warm_body, "prewarm must use to_thread for browser check"
        # Must NOT call _browser_deps_available() on the event loop before to_thread
        before_thread = warm_body.split("asyncio.to_thread")[0] if "asyncio.to_thread" in warm_body else warm_body
        assert "_browser_deps_available" not in before_thread, \
            "_browser_deps_available must not be called before to_thread (blocks event loop)"


# ─── options bag validation ───────────────────────────────────────

class TestStrictOptions:

    def test_unknown_key_raises_with_supported_set(self):
        with pytest.raises(ValueError) as exc:
            _strict_options({"wait": 100, "typo_key": 1}, _SHOT_OPTIONS, _SHOT_OPTIONS, "screenshot")
        msg = str(exc.value)
        assert "typo_key" in msg
        assert "screenshot" in msg
        assert "full_page" in msg  # supported set shown so the agent can self-correct

    def test_known_keys_forwarded(self):
        out = _strict_options({"full_page": True, "timeout": 5000}, _SHOT_OPTIONS, _SHOT_OPTIONS, "screenshot")
        assert out == {"full_page": True, "timeout": 5000}

    def test_empty_options_ok(self):
        assert _strict_options({}, _SHOT_OPTIONS, _SHOT_OPTIONS, "screenshot") == {}

    def test_smart_fetch_promoted_keys_allowed_but_not_forwarded(self):
        # css_selector is read via options.get fallback in _dispatch, so it must
        # be ALLOWED (no false rejection) yet not FORWARDED (**kw would collide
        # with the explicit css_selector arg -> duplicate-keyword TypeError).
        out = _strict_options(
            {"css_selector": ".main", "proxy": "http://p:1"},
            _SF_OPTIONS_ALLOWED, _SF_OPTIONS_FORWARDED, "smart_fetch",
        )
        assert out == {"proxy": "http://p:1"}
        assert "css_selector" not in out

    def test_smart_fetch_unknown_key_raises(self):
        with pytest.raises(ValueError):
            _strict_options({"includeMedia": True}, _SF_OPTIONS_ALLOWED, _SF_OPTIONS_FORWARDED, "smart_fetch")
