"""Cache tests: round-trip set/get, TTL expiry, clear expired vs all,
cache key determinism, MAX_CACHE_ENTRIES eviction.

Uses a temp directory so tests never touch the real cache DB.
All async, real SQLite operations. No mocks.
"""

import asyncio
import time
import pytest
from pathlib import Path
from hound_mcp.cache import (
    get_cached, set_cached, clear_cache, clear_all_cache,
    _cache_key, DEFAULT_TTL, MAX_CACHE_ENTRIES,
)


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


# ─── Round-trip ────────────────────────────────────────────────────

class TestCacheRoundTrip:

    @pytest.mark.asyncio
    async def test_set_then_get_returns_content(self, cache_dir):
        await set_cached("https://example.com", "markdown", ["Hello world"],
                         200, cache_dir=cache_dir)
        result = await get_cached("https://example.com", "markdown",
                                  cache_dir=cache_dir)
        assert result is not None
        assert result["content"] == ["Hello world"]
        assert result["status"] == 200

    @pytest.mark.asyncio
    async def test_different_extraction_types_get_different_keys(self, cache_dir):
        await set_cached("https://example.com", "markdown", ["md"], 200, cache_dir=cache_dir)
        await set_cached("https://example.com", "html", ["<html>"], 200, cache_dir=cache_dir)
        md = await get_cached("https://example.com", "markdown", cache_dir=cache_dir)
        html = await get_cached("https://example.com", "html", cache_dir=cache_dir)
        assert md["content"] == ["md"]
        assert html["content"] == ["<html>"]

    @pytest.mark.asyncio
    async def test_css_selector_differentiates_keys(self, cache_dir):
        await set_cached("https://example.com", "markdown", ["all"],
                         200, css_selector=".main", cache_dir=cache_dir)
        await set_cached("https://example.com", "markdown", ["filtered"],
                         200, css_selector=".sidebar", cache_dir=cache_dir)
        main = await get_cached("https://example.com", "markdown",
                                css_selector=".main", cache_dir=cache_dir)
        sidebar = await get_cached("https://example.com", "markdown",
                                   css_selector=".sidebar", cache_dir=cache_dir)
        assert main["content"] == ["all"]
        assert sidebar["content"] == ["filtered"]

    @pytest.mark.asyncio
    async def test_content_type_round_trips(self, cache_dir):
        await set_cached("https://example.com", "markdown", ["x"], 200,
                        cache_dir=cache_dir, content_type="text/html",
                        total_size_bytes=12345)
        result = await get_cached("https://example.com", "markdown", cache_dir=cache_dir)
        assert result["content_type"] == "text/html"
        assert result["total_size_bytes"] == 12345

    @pytest.mark.asyncio
    async def test_envelope_round_trips(self, cache_dir):
        env = {"metadata": {"title": "Test"}, "page_type": "article"}
        await set_cached("https://example.com", "markdown", ["x"], 200,
                        cache_dir=cache_dir, envelope=env)
        result = await get_cached("https://example.com", "markdown", cache_dir=cache_dir)
        assert result["envelope"]["metadata"]["title"] == "Test"
        assert result["envelope"]["page_type"] == "article"


# ─── TTL behavior ──────────────────────────────────────────────────

class TestCacheTTL:

    @pytest.mark.asyncio
    async def test_expired_entry_not_returned(self, cache_dir):
        await set_cached("https://example.com", "markdown", ["x"], 200,
                        cache_dir=cache_dir, ttl=1)
        await asyncio.sleep(1.1)
        result = await get_cached("https://example.com", "markdown",
                                  cache_dir=cache_dir)
        assert result is None

    @pytest.mark.asyncio
    async def test_caller_ttl_can_request_fresher(self, cache_dir):
        # Stored with TTL=3600, caller requests TTL=1
        await set_cached("https://example.com", "markdown", ["x"], 200,
                        cache_dir=cache_dir, ttl=3600)
        await asyncio.sleep(1.1)
        result = await get_cached("https://example.com", "markdown",
                                  ttl=1, cache_dir=cache_dir)
        assert result is None  # lesser of stored (3600) and requested (1) = 1, expired

    @pytest.mark.asyncio
    async def test_fresh_entry_returned(self, cache_dir):
        await set_cached("https://example.com", "markdown", ["x"], 200,
                        cache_dir=cache_dir, ttl=3600)
        result = await get_cached("https://example.com", "markdown", cache_dir=cache_dir)
        assert result is not None


# ─── Cache clearing ────────────────────────────────────────────────

class TestCacheClearing:

    @pytest.mark.asyncio
    async def test_clear_cache_removes_expired_only(self, cache_dir):
        await set_cached("https://fresh.com", "markdown", ["fresh"], 200,
                        cache_dir=cache_dir, ttl=3600)
        await set_cached("https://stale.com", "markdown", ["stale"], 200,
                        cache_dir=cache_dir, ttl=1)
        await asyncio.sleep(1.1)
        purged = await clear_cache(cache_dir=cache_dir)
        assert purged == 1
        # Fresh entry still there
        fresh = await get_cached("https://fresh.com", "markdown", cache_dir=cache_dir)
        assert fresh is not None
        # Stale entry gone (already expired, then purged)
        stale = await get_cached("https://stale.com", "markdown", cache_dir=cache_dir)
        assert stale is None

    @pytest.mark.asyncio
    async def test_clear_all_cache_nukes_everything(self, cache_dir):
        await set_cached("https://a.com", "markdown", ["a"], 200, cache_dir=cache_dir)
        await set_cached("https://b.com", "markdown", ["b"], 200, cache_dir=cache_dir)
        purged = await clear_all_cache(cache_dir=cache_dir)
        assert purged == 2
        assert await get_cached("https://a.com", "markdown", cache_dir=cache_dir) is None
        assert await get_cached("https://b.com", "markdown", cache_dir=cache_dir) is None


# ─── Cache key ─────────────────────────────────────────────────────

class TestCacheKey:

    def test_same_params_same_key(self):
        k1 = _cache_key("https://example.com", "markdown", ".main", "1-5")
        k2 = _cache_key("https://example.com", "markdown", ".main", "1-5")
        assert k1 == k2

    def test_different_url_different_key(self):
        assert _cache_key("https://a.com", "markdown") != _cache_key("https://b.com", "markdown")

    def test_different_source_different_key(self):
        assert _cache_key("https://x.com", "markdown", source="live") != \
               _cache_key("https://x.com", "markdown", source="archive.org")

    def test_key_is_24_chars(self):
        assert len(_cache_key("https://x.com", "markdown")) == 24


# ─── MAX_CACHE_ENTRIES eviction ────────────────────────────────────

class TestCacheEviction:

    @pytest.mark.asyncio
    async def test_eviction_when_over_cap(self, cache_dir, monkeypatch):
        # Lower the cap for testing
        monkeypatch.setattr("hound_mcp.cache.MAX_CACHE_ENTRIES", 20)
        # Insert 25 entries
        for i in range(25):
            await set_cached(f"https://example.com/{i}", "markdown", [f"content-{i}"],
                            200, cache_dir=cache_dir)
        # Should have evicted oldest down to ~90% of cap (18)
        # Check some old entries are gone
        old = await get_cached("https://example.com/0", "markdown", cache_dir=cache_dir)
        assert old is None
        # Check newest entry is still there
        new = await get_cached("https://example.com/24", "markdown", cache_dir=cache_dir)
        assert new is not None
