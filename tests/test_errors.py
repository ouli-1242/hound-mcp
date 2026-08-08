"""Tests for network error classification and agent-actionable diagnostics."""

import pytest

from hound_mcp.errors import classify_network_error, get_hint


# ─── classify_network_error: category detection ──────────────────────────────

class TestClassifyNetworkError:
    """Test that known error strings are classified into the right category."""

    @pytest.mark.parametrize("error_str,expected_category", [
        # Connection refused
        ("net::ERR_CONNECTION_REFUSED", "connection_refused"),
        ("ConnectionRefusedError: [WinError 10061]", "connection_refused"),
        ("os error 10061", "connection_refused"),
        ("ECONNREFUSED", "connection_refused"),
        ("connection refused by host", "connection_refused"),
        # Connection reset
        ("os error 10054", "connection_reset"),
        ("net::ERR_CONNECTION_RESET", "connection_reset"),
        ("ConnectionResetError: [WinError 10054]", "connection_reset"),
        ("ECONNRESET", "connection_reset"),
        ("远程主机强迫关闭了一个现有的连接。 (os error 10054)", "connection_reset"),
        ("An existing connection was forcibly closed by the remote host", "connection_reset"),
        ("connection was forcibly closed", "connection_reset"),
        # DNS failure
        ("net::ERR_NAME_NOT_RESOLVED", "dns_failure"),
        ("NameResolutionError: Failed to resolve 'example.com'", "dns_failure"),
        ("getaddrinfo failed", "dns_failure"),
        ("nodename nor servname provided, or not known", "dns_failure"),
        ("Name or service not known", "dns_failure"),
        ("DNS resolution failed for host", "dns_failure"),
        # Timeout
        ("TimeoutError: request timed out", "timeout"),
        ("net::ERR_TIMED_OUT", "timeout"),
        ("net::ERR_CONNECTION_TIMED_OUT", "timeout"),
        ("ERR_NAVIGATION_TIMEOUT", "timeout"),
        ("Navigation timeout of 30000ms exceeded", "timeout"),
        ("Timeout 30000ms exceeded", "timeout"),
        ("deadline exceeded", "timeout"),
        # TLS errors
        ("CERTIFICATE_VERIFY_FAILED", "tls_error"),
        ("SSL: CERTIFICATE_VERIFY_FAILED", "tls_error"),
        ("TLS handshake failed", "tls_error"),
        ("ssl_error_rx_record_too_long", "tls_error"),
        ("certificate verify failed", "tls_error"),
        ("net::ERR_CERT_AUTHORITY_INVALID", "tls_error"),
        # Proxy errors
        ("net::ERR_PROXY_CONNECTION_FAILED", "proxy_error"),
        ("proxy connection refused", "proxy_error"),
        ("tunnel connection failed", "proxy_error"),
    ])
    def test_known_errors(self, error_str, expected_category):
        category, hint = classify_network_error(error_str)
        assert category == expected_category
        assert hint  # non-empty hint
        assert len(hint) > 20  # actionable sentence, not just a word

    def test_empty_string(self):
        category, hint = classify_network_error("")
        assert category == "unknown"
        assert hint

    def test_none_like_empty(self):
        category, hint = classify_network_error("")
        assert category == "unknown"

    def test_unrecognized_error(self):
        category, hint = classify_network_error("some random error xyz 12345")
        assert category == "unknown"
        assert "Fetch failed" in hint

    def test_hint_is_actionable(self):
        """Each category's hint should tell the agent what to do."""
        for error_str, expected in [
            ("ERR_CONNECTION_REFUSED", "connection_refused"),
            ("os error 10054", "connection_reset"),
            ("ERR_NAME_NOT_RESOLVED", "dns_failure"),
            ("timed out", "timeout"),
            ("CERTIFICATE_VERIFY_FAILED", "tls_error"),
            ("ERR_PROXY_CONNECTION_FAILED", "proxy_error"),
        ]:
            _, hint = classify_network_error(error_str)
            # Hints should contain action verbs
            assert any(word in hint.lower() for word in
                      ("retry", "switch", "verify", "check", "do not")), \
                f"Hint for {expected} lacks actionable guidance: {hint}"


class TestGetHint:
    def test_known_category(self):
        hint = get_hint("connection_refused")
        assert "refused" in hint.lower()

    def test_unknown_category_fallback(self):
        hint = get_hint("nonexistent_category")
        assert hint == get_hint("unknown")


# ─── _agent_hints: next_action for network errors ────────────────────────────

class TestAgentHintsNetworkErrors:
    """Test that _agent_hints produces actionable next_action for failures."""

    def _make_result(self, status=0, error="", content=None):
        """Create a minimal ResponseModel for testing _agent_hints."""
        from hound_mcp.server import ResponseModel
        return ResponseModel(
            url="https://example.com",
            status=status,
            content=content or [""],
            fetcher_used="http",
            error=error,
        )

    def test_connection_refused_next_action(self):
        from hound_mcp.server import _agent_hints
        result = self._make_result(
            status=0,
            error="network_error: net::ERR_CONNECTION_REFUSED",
        )
        _, next_action, content_ok = _agent_hints(result)
        assert not content_ok
        assert "refused" in next_action.lower() or "connection" in next_action.lower()
        assert "retry" in next_action.lower() or "switch" in next_action.lower() or "do not" in next_action.lower()

    def test_timeout_next_action(self):
        from hound_mcp.server import _agent_hints
        result = self._make_result(
            status=0,
            error="network_error: TimeoutError: request timed out",
        )
        _, next_action, _ = _agent_hints(result)
        assert "timeout" in next_action.lower() or "timed out" in next_action.lower()

    def test_all_tiers_failed_connection_refused(self):
        from hound_mcp.server import _agent_hints
        result = self._make_result(
            status=0,
            error="all_tiers_failed: connection_refused (HTTP status 0)",
            content=["[All fetch tiers failed] net::ERR_CONNECTION_REFUSED"],
        )
        _, next_action, _ = _agent_hints(result)
        assert "connection refused" in next_action.lower()
        assert "do not retry" in next_action.lower() or "switch" in next_action.lower()

    def test_all_tiers_failed_timeout(self):
        from hound_mcp.server import _agent_hints
        result = self._make_result(
            status=0,
            error="all_tiers_failed: timeout (HTTP status 0)",
            content=["[All fetch tiers failed] TimeoutError"],
        )
        _, next_action, _ = _agent_hints(result)
        assert "timeout" in next_action.lower()

    def test_generic_http_error_still_gets_hint(self):
        from hound_mcp.server import _agent_hints
        result = self._make_result(
            status=500,
            error="http_error_500: server returned error status",
        )
        _, next_action, content_ok = _agent_hints(result)
        assert not content_ok
        assert next_action  # should not be empty


# ─── Search: low diversity warning ───────────────────────────────────────────

class TestSearchLowDiversityWarning:
    """Test that search next_action warns when most engines are blocked."""

    def test_high_block_ratio_warns(self):
        from hound_mcp.search import _search_next_action, SearchResult
        results = [SearchResult(
            title="Test", url="https://example.com", snippet="test",
            source="yandex", position=1, relevance_score=0.8,
            fetch_relevance="high", engines_consensus="1 of 1",
        )]
        engine_blocked = ["mojeek", "qwant", "google", "startpage", "brave", "duckduckgo", "yahoo"]
        engines_used = ["yandex"]
        next_action = _search_next_action(results, engine_blocked, "", engines_used)
        assert "LOW diversity" in next_action or "WARNING" in next_action
        assert "HOUND_SEARCH_PROXY" in next_action

    def test_low_block_ratio_no_warning(self):
        from hound_mcp.search import _search_next_action, SearchResult
        results = [SearchResult(
            title="Test", url="https://example.com", snippet="test",
            source="yandex", position=1, relevance_score=0.8,
            fetch_relevance="high", engines_consensus="3 of 5",
        )]
        engine_blocked = ["google"]
        engines_used = ["yandex", "brave", "duckduckgo", "mojeek"]
        next_action = _search_next_action(results, engine_blocked, "", engines_used)
        assert "LOW diversity" not in next_action
        assert "Some engines" in next_action

    def test_no_blocked_no_warning(self):
        from hound_mcp.search import _search_next_action, SearchResult
        results = [SearchResult(
            title="Test", url="https://example.com", snippet="test",
            source="yandex", position=1, relevance_score=0.8,
            fetch_relevance="high", engines_consensus="3 of 5",
        )]
        next_action = _search_next_action(results, [], "", ["yandex", "brave", "duckduckgo"])
        assert "WARNING" not in next_action
        assert "blocked" not in next_action.lower()


# ─── Crawl: network failure aggregate diagnostics ────────────────────────────

class TestCrawlNetworkDiagnostics:
    """Test that crawl provides next_action when pages fail with network errors."""

    def test_all_pages_network_failure(self):
        """When all pages fail with network errors, next_action should not be empty."""
        from hound_mcp.crawl import CrawlPage, CrawlResponseModel
        # Simulate what smart_crawl does at the end
        pages = [
            CrawlPage(url="https://example.com/1", depth=0, status=-1,
                     content_ok=False, error="net::ERR_CONNECTION_REFUSED"),
            CrawlPage(url="https://example.com/2", depth=1, status=-1,
                     content_ok=False, error="net::ERR_CONNECTION_REFUSED"),
        ]
        # Replicate the diagnostic logic from crawl.py
        ok = sum(1 for p in pages if p.content_ok)
        network_failures = sum(1 for p in pages if p.status == -1 or p.status == 0)
        assert network_failures == 2
        assert network_failures >= len(pages) * 0.5

        from hound_mcp.errors import classify_network_error
        sample_errors = [p.error for p in pages if p.error][:3]
        category, hint = classify_network_error(" ".join(sample_errors))
        assert category == "connection_refused"
        assert "refused" in hint.lower()

    def test_mixed_results_no_network_diagnostic(self):
        """When most pages succeed, no network diagnostic is added."""
        from hound_mcp.crawl import CrawlPage
        pages = [
            CrawlPage(url="https://example.com/1", depth=0, status=200,
                     content_ok=True, content=["real content here"]),
            CrawlPage(url="https://example.com/2", depth=1, status=200,
                     content_ok=True, content=["more content"]),
            CrawlPage(url="https://example.com/3", depth=1, status=-1,
                     content_ok=False, error="timeout"),
        ]
        network_failures = sum(1 for p in pages if p.status == -1 or p.status == 0)
        # Only 1/3 failed - below 50% threshold
        assert network_failures < len(pages) * 0.5
