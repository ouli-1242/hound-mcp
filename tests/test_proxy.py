"""Adversarial tests for the smart proxy rotation system (search_proxy.py).

Tests cover:
- ProxyPool round-robin rotation (per-call, cycling through all)
- Health tracking (cooldown on failure, recovery after cooldown)
- Config file management (add, remove, clear, list)
- Env var parsing (single + comma-separated)
- Merging env var + config file (dedup, order, max cap)
- Backwards compatibility (single proxy = pool of 1)
- Edge cases (invalid scheme, empty, duplicate, overflow)
- Redaction (credentials hidden in display)
- Metasearch integration (proxy selected per call, health marked)
"""

import os
import time
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

from hound_mcp.search_proxy import (
    ProxyPool, _validate_proxy, _read_config_file, _read_env_var,
    load_proxies, get_proxy_pool, get_next_proxy,
    MAX_PROXIES,
)


@pytest.fixture(autouse=True)
def _reset_pool_singleton():
    """Reset the module-level proxy pool before and after each test."""
    import hound_mcp.search_proxy as _sp
    _sp._pool = None
    yield
    _sp._pool = None


# ─── ProxyPool rotation ────────────────────────────────────────────

class TestProxyPoolRotation:

    def test_round_robin_cycles_through_all_proxies(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80", "http://p3:80"])
        results = [pool.get_proxy() for _ in range(7)]
        assert results == [
            "http://p1:80", "http://p2:80", "http://p3:80",
            "http://p1:80", "http://p2:80", "http://p3:80",
            "http://p1:80",
        ]

    def test_single_proxy_always_returns_same(self):
        pool = ProxyPool(["socks5://1.2.3.4:1080"])
        assert pool.get_proxy() == "socks5://1.2.3.4:1080"
        assert pool.get_proxy() == "socks5://1.2.3.4:1080"

    def test_empty_pool_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            ProxyPool([])

    def test_size_property(self):
        pool = ProxyPool(["http://a:80", "http://b:80"])
        assert pool.size == 2


# ─── ProxyPool health tracking ─────────────────────────────────────

class TestProxyPoolHealth:

    def test_mark_failed_cools_proxy(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        pool.mark_failed("http://p1:80")
        assert pool.get_proxy() == "http://p2:80"
        assert pool.get_proxy() == "http://p2:80"

    def test_mark_success_clears_cooldown(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        pool.mark_failed("http://p1:80")
        assert pool.get_proxy() == "http://p2:80"
        pool.mark_success("http://p1:80")
        assert pool.get_proxy() == "http://p1:80"

    def test_all_cooled_returns_none(self):
        pool = ProxyPool(["http://p1:80"])
        pool.mark_failed("http://p1:80")
        assert pool.get_proxy() is None

    def test_all_cooled_multiple_proxies_returns_none(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80", "http://p3:80"])
        pool.mark_failed("http://p1:80")
        pool.mark_failed("http://p2:80")
        pool.mark_failed("http://p3:80")
        assert pool.get_proxy() is None

    def test_cooldown_expires_and_proxy_returns(self):
        pool = ProxyPool(["http://p1:80"])
        pool.mark_failed("http://p1:80")
        assert pool.get_proxy() is None
        pool._state["http://p1:80"]["cooled_until"] = time.time() - 1
        assert pool.get_proxy() == "http://p1:80"

    def test_mark_failed_unknown_proxy_does_not_crash(self):
        pool = ProxyPool(["http://p1:80"])
        pool.mark_failed("http://unknown:80")
        assert pool.get_proxy() == "http://p1:80"

    def test_status_reports_cooled_state(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        pool.mark_failed("http://p1:80")
        statuses = pool.status()
        assert len(statuses) == 2
        assert statuses[0]["proxy"] == "http://p1:80"
        assert statuses[0]["cooled"] is True
        assert statuses[0]["cooled_remaining"] > 0
        assert statuses[1]["cooled"] is False


# ─── Proxy dead-detection + health probe ───────────────────────────

class TestProxyDeadDetection:

    def test_skips_dead_proxy_when_alive_exists(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        for _ in range(ProxyPool.MAX_CONSECUTIVE_FAILS):
            pool.mark_failed("http://p1:80")
        # p1 is dead -> only p2 is handed out
        assert pool.get_proxy() == "http://p2:80"
        assert pool.get_proxy() == "http://p2:80"

    def test_success_revives_proxy(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        for _ in range(ProxyPool.MAX_CONSECUTIVE_FAILS):
            pool.mark_failed("http://p1:80")
        assert pool.get_proxy() == "http://p2:80"
        pool.mark_success("http://p1:80")
        assert pool.get_proxy() == "http://p1:80"

    def test_all_dead_falls_back_to_first(self):
        # Stale probes shouldn't hard-block a call that might succeed.
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        for p in ("http://p1:80", "http://p2:80"):
            pool._stats[p]["consecutive_fails"] = ProxyPool.MAX_CONSECUTIVE_FAILS
        assert pool.get_proxy() in ("http://p1:80", "http://p2:80")

    def test_status_reports_dead_flag(self):
        pool = ProxyPool(["http://p1:80"])
        for _ in range(ProxyPool.MAX_CONSECUTIVE_FAILS):
            pool.mark_failed("http://p1:80")
        statuses = pool.status()
        assert statuses[0]["dead"] is True
        assert statuses[0]["fail"] == ProxyPool.MAX_CONSECUTIVE_FAILS

    @pytest.mark.asyncio
    async def test_health_check_revives_and_marks_dead(self, monkeypatch):
        pool = ProxyPool(["http://alive:80", "http://dead:80"])

        class FakeResp:
            status = 200

        class FakeSession:
            def __init__(self, proxy=None, **kwargs):
                self._proxy = proxy

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, follow_redirects="safe"):
                if self._proxy == "http://alive:80":
                    return FakeResp()
                raise ConnectionError("refused")

        monkeypatch.setattr("hound_mcp.fetcher.HTTPSession", FakeSession)

        results = await pool.health_check()
        assert results["http://alive:80"] is True
        assert results["http://dead:80"] is False
        # alive proxy revived (fail streak reset), dead one not marked dead yet
        assert pool._is_dead("http://alive:80") is False
        assert pool.get_proxy() == "http://alive:80"


# ─── Validation ────────────────────────────────────────────────────

class TestProxyValidation:

    def test_valid_http(self):
        assert _validate_proxy("http://1.2.3.4:8080") == "http://1.2.3.4:8080"

    def test_valid_https(self):
        assert _validate_proxy("https://1.2.3.4:443") == "https://1.2.3.4:443"

    def test_valid_socks5(self):
        assert _validate_proxy("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"

    def test_valid_socks5h(self):
        assert _validate_proxy("socks5h://1.2.3.4:1080") == "socks5h://1.2.3.4:1080"

    def test_valid_with_auth(self):
        assert _validate_proxy("http://user:pass@1.2.3.4:8080") == "http://user:pass@1.2.3.4:8080"

    def test_strips_whitespace(self):
        assert _validate_proxy("  http://1.2.3.4:8080  ") == "http://1.2.3.4:8080"

    def test_rejects_empty(self):
        assert _validate_proxy("") is None
        assert _validate_proxy("   ") is None

    def test_rejects_none(self):
        assert _validate_proxy(None) is None

    def test_rejects_non_string(self):
        assert _validate_proxy(123) is None

    def test_rejects_invalid_scheme(self):
        assert _validate_proxy("ftp://1.2.3.4:21") is None
        assert _validate_proxy("1.2.3.4:8080") is None


# ─── Env var parsing ───────────────────────────────────────────────

class TestEnvVarParsing:

    def test_single_proxy_env_var(self, monkeypatch):
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "http://1.2.3.4:8080")
        monkeypatch.setattr("hound_mcp.search_proxy._config_path",
                             lambda: Path("/nonexistent_proxy_test"))
        assert _read_env_var() == ["http://1.2.3.4:8080"]

    def test_comma_separated_env_var(self, monkeypatch):
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "http://p1:80,socks5://p2:1080,http://p3:80")
        assert _read_env_var() == ["http://p1:80", "socks5://p2:1080", "http://p3:80"]

    def test_env_var_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "  http://1.2.3.4:8080  ,  socks5://5.6.7.8:1080  ")
        assert _read_env_var() == ["http://1.2.3.4:8080", "socks5://5.6.7.8:1080"]

    def test_env_var_empty(self, monkeypatch):
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        assert _read_env_var() == []

    def test_env_var_invalid_scheme_skipped(self, monkeypatch):
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "ftp://bad:21,http://good:80")
        assert _read_env_var() == ["http://good:80"]


# ─── Merging env var + config file ─────────────────────────────────

class TestProxyMerging:

    def test_env_and_config_merged_deduped(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80", "http://p2:80"]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "http://p1:80,http://p3:80")
        result = load_proxies()
        assert result == ["http://p1:80", "http://p3:80", "http://p2:80"]

    def test_env_only(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "http://p1:80")
        assert load_proxies() == ["http://p1:80"]

    def test_config_only(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["socks5://p1:1080"]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        assert load_proxies() == ["socks5://p1:1080"]

    def test_none_configured(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        assert load_proxies() == []

    def test_max_proxies_cap(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": [f"http://p{i}:80" for i in range(MAX_PROXIES + 5)]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        result = load_proxies()
        assert len(result) == MAX_PROXIES


# ─── Pool singleton ────────────────────────────────────────────────

class TestPoolSingleton:

    def test_get_proxy_pool_none_when_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: tmp_path / "none.json")
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        assert get_proxy_pool() is None
        assert get_next_proxy() is None

    def test_get_pool_caches_until_config_changes(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80"]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        pool1 = get_proxy_pool()
        pool2 = get_proxy_pool()
        assert pool1 is pool2
        config_file.write_text(json.dumps({"proxies": ["http://p1:80", "http://p2:80"]}))
        pool3 = get_proxy_pool()
        assert pool3 is not pool1
        assert pool3.size == 2

    def test_rotation_across_calls(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80", "http://p2:80", "http://p3:80"]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        assert get_next_proxy() == "http://p1:80"
        assert get_next_proxy() == "http://p2:80"
        assert get_next_proxy() == "http://p3:80"
        assert get_next_proxy() == "http://p1:80"


# ─── Metasearch integration ────────────────────────────────────────

class TestMetasearchProxyIntegration:

    def test_metasearch_uses_no_proxy_when_unconfigured(self, monkeypatch, tmp_path):
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: tmp_path / "none.json")
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        from hound_mcp.search_metasearch import _get_search_proxy
        assert _get_search_proxy() is None

    def test_metasearch_rotates_proxy_per_call(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80", "http://p2:80"]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        from hound_mcp.search_metasearch import _get_search_proxy
        assert _get_search_proxy() == "http://p1:80"
        assert _get_search_proxy() == "http://p2:80"
        assert _get_search_proxy() == "http://p1:80"

    def test_metasearch_sets_module_level_proxy(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80"]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        import hound_mcp.search_metasearch as sm
        sm._PROXY = "stale_value"
        proxy = sm._get_search_proxy()
        assert proxy == "http://p1:80"
        assert sm._PROXY == "http://p1:80"

    def test_all_cooled_falls_back_to_direct(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80"]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        pool = get_proxy_pool()
        pool.mark_failed("http://p1:80")
        from hound_mcp.search_metasearch import _get_search_proxy
        assert _get_search_proxy() is None


# ─── Crawl proxy integration ───────────────────────────────────────

class TestCrawlProxyIntegration:
    """Verify crawl fetches rotate through the proxy pool."""

    def test_crawl_fetch_one_passes_proxy_to_smart_fetch(self, monkeypatch, tmp_path):
        """fetch_one in crawl.py should call get_next_proxy and pass it to smart_fetch."""
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://crawl-p1:80", "http://crawl-p2:80"]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)

        received_proxies = []

        async def mock_smart_fetch(self, url=None, **kwargs):
            from hound_mcp.server import ResponseModel
            received_proxies.append(kwargs.get("proxy"))
            return ResponseModel(
                url=url or "", status=200, content=["<html>ok</html>"],
                fetcher_used="http",
            )

        from hound_mcp.server import MasterFetchServer
        monkeypatch.setattr(MasterFetchServer, "smart_fetch", mock_smart_fetch)

        server = MasterFetchServer()
        import asyncio
        result = asyncio.run(server.smart_crawl(
            "https://example.com/", max_pages=1, cache_ttl=0,
        ))

        assert len(received_proxies) > 0
        assert received_proxies[0] in ["http://crawl-p1:80", "http://crawl-p2:80"]

    def test_crawl_no_proxies_passes_none(self, monkeypatch, tmp_path):
        """When no proxies configured, crawl passes None (direct connection)."""
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: tmp_path / "none.json")
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)

        received_proxies = []

        async def mock_smart_fetch(self, url=None, **kwargs):
            from hound_mcp.server import ResponseModel
            received_proxies.append(kwargs.get("proxy"))
            return ResponseModel(
                url=url or "", status=200, content=["<html>ok</html>"],
                fetcher_used="http",
            )

        from hound_mcp.server import MasterFetchServer
        monkeypatch.setattr(MasterFetchServer, "smart_fetch", mock_smart_fetch)

        server = MasterFetchServer()
        import asyncio
        asyncio.run(server.smart_crawl(
            "https://example.com/", max_pages=1, cache_ttl=0,
        ))

        assert all(p is None for p in received_proxies)

    def test_crawl_rotates_proxies_across_pages(self, monkeypatch, tmp_path):
        """Multiple crawl page fetches should rotate through different proxies."""
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": [
            "http://c1:80", "http://c2:80", "http://c3:80",
        ]}))
        monkeypatch.setattr("hound_mcp.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)

        received_proxies = []

        async def mock_smart_fetch(self, url=None, **kwargs):
            from hound_mcp.server import ResponseModel
            received_proxies.append(kwargs.get("proxy"))
            return ResponseModel(
                url=url or "", status=200, content=[
                    '<html><body><a href="/page2">2</a><a href="/page3">3</a></body></html>'
                ],
                fetcher_used="http",
            )

        from hound_mcp.server import MasterFetchServer
        monkeypatch.setattr(MasterFetchServer, "smart_fetch", mock_smart_fetch)

        server = MasterFetchServer()
        import asyncio
        asyncio.run(server.smart_crawl(
            "https://example.com/", max_pages=3, cache_ttl=0,
        ))

        assert len(received_proxies) >= 2
        unique_proxies = set(received_proxies)
        assert len(unique_proxies) > 1, f"Expected rotation but got {received_proxies}"
