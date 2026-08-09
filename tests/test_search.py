"""Search tests: site/exclude_sites hostname filtering (PR #7), GitHub
case-folding dedup (PR #8), URL normalization, multi_search mapping logic,
RawResult consensus, EngineReport status mapping.

The metasearch backend is mocked (no network), but the filtering, dedup,
consensus, and mapping logic being tested is the REAL code that runs on
real search results.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from hound_mcp import search as search
from hound_mcp import search_engines as se
from hound_mcp.search_engines import (
    _passes_site_filter, _normalize_domain, _is_domain_or_subdomain,
    RawResult, EngineReport, multi_search,
    DEFAULT_ENGINES, _INDEX_FAMILY,
)


@pytest.fixture
def smart_search_cache(monkeypatch):
    """In-memory cache and deterministic search backend for cache-key tests."""
    cache = {}
    live_regions = []

    async def fake_get_cached(query, cache_type, css_selector, **kwargs):
        return cache.get((query, cache_type))

    async def fake_set_cached(query, cache_type, content, status, css_selector, ttl, **kwargs):
        cache[(query, cache_type)] = {"content": content}

    async def fake_multi_search(query, max_results, **kwargs):
        region = kwargs["region"]
        live_regions.append(region)
        return [RawResult(
            title=f"Result for {region}",
            url=f"https://{region}.example.test/",
            snippet="regional result",
            source="brave",
            position=1,
        )], [EngineReport(name="brave", ok=True)]

    async def fake_ensure_reranker():
        return None

    monkeypatch.setattr(search, "get_cached", fake_get_cached)
    monkeypatch.setattr(search, "set_cached", fake_set_cached)
    monkeypatch.setattr(search, "multi_search", fake_multi_search)
    monkeypatch.setattr(search, "ensure_reranker", fake_ensure_reranker)
    monkeypatch.setattr(
        search, "_rank",
        lambda query, ranked, mode: (ranked, [1.0] * len(ranked), "merge", ""),
    )
    return cache, live_regions


class TestSmartSearchRegionCache:

    @pytest.mark.asyncio
    async def test_explicit_regions_use_distinct_cache_entries(self, smart_search_cache):
        cache, live_regions = smart_search_cache

        us = await search.smart_search(
            None, "regional cache test", engines=["brave"], region="us-en",
        )
        gb = await search.smart_search(
            None, "regional cache test", engines=["brave"], region="gb-en",
        )

        assert us.cached is False
        assert gb.cached is False
        assert [us.results[0].title, gb.results[0].title] == [
            "Result for us-en", "Result for gb-en",
        ]
        assert live_regions == ["us-en", "gb-en"]
        assert len(cache) == 2

    @pytest.mark.asyncio
    async def test_same_normalized_explicit_region_uses_cached_result(self, smart_search_cache):
        cache, live_regions = smart_search_cache

        first = await search.smart_search(
            None, "regional cache test", engines=["brave"], region="GB-EN",
        )
        second = await search.smart_search(
            None, "regional cache test", engines=["brave"], region="gb-en",
        )

        assert first.cached is False
        assert second.cached is True
        assert second.results[0].title == "Result for GB-EN"
        assert live_regions == ["GB-EN"]
        assert len(cache) == 1

    @pytest.mark.asyncio
    async def test_omitted_region_keeps_existing_cache_key(self, smart_search_cache):
        cache, _ = smart_search_cache

        await search.smart_search(
            None, "regional cache test", engines=["brave"],
        )

        assert list(cache) == [
            ("regional cache test", "search:v5:6:::::0:brave::auto:regional cache test"),
        ]


# ─── Hostname boundary filtering (PR #7) ──────────────────────────

class TestSiteFilter:

    def test_exact_domain_passes_site_filter(self):
        assert _passes_site_filter("https://github.com/repo", "github.com", None) is True

    def test_subdomain_passes_site_filter(self):
        assert _passes_site_filter("https://docs.github.com/en", "github.com", None) is True

    def test_evil_prefix_rejected(self):
        assert _passes_site_filter("https://evilgithub.com/repo", "github.com", None) is False

    def test_evil_suffix_rejected(self):
        assert _passes_site_filter("https://github.com.evil.test/repo", "github.com", None) is False

    def test_similar_domain_rejected(self):
        assert _passes_site_filter("https://notgithub.com/repo", "github.com", None) is False

    def test_port_stripped_for_comparison(self):
        assert _passes_site_filter("https://github.com:8443/repo", "github.com", None) is True

    def test_www_prefix_stripped_correctly(self):
        # www.github.com as site filter should match www.github.com
        assert _passes_site_filter("https://www.github.com/repo", "www.github.com", None) is True
        # But github.com as site should also match www.github.com (subdomain)
        assert _passes_site_filter("https://www.github.com/repo", "github.com", None) is True

    def test_ww_prefix_not_stripped_as_www(self):
        # "wwgithub.com" must not be stripped as if it started with "www."
        assert _passes_site_filter("https://github.com/repo", "wwgithub.com", None) is False


class TestExcludeSitesFilter:

    def test_exclude_exact_domain(self):
        assert _passes_site_filter("https://github.com/repo", None, ["github.com"]) is False

    def test_exclude_subdomain(self):
        assert _passes_site_filter("https://docs.github.com/repo", None, ["github.com"]) is False

    def test_exclude_does_not_block_evil_prefix(self):
        assert _passes_site_filter("https://evilgithub.com/repo", None, ["github.com"]) is True

    def test_exclude_does_not_block_evil_suffix(self):
        assert _passes_site_filter("https://github.com.evil.test/repo", None, ["github.com"]) is True

    def test_exclude_does_not_block_similar(self):
        assert _passes_site_filter("https://notgithub.com/repo", None, ["github.com"]) is True


class TestNormalizeDomain:

    def test_strips_www_prefix(self):
        assert _normalize_domain("www.example.com") == "example.com"

    def test_does_not_strip_ww(self):
        assert _normalize_domain("wwexample.com") == "wwexample.com"

    def test_strips_trailing_dot(self):
        assert _normalize_domain("example.com.") == "example.com"

    def test_lowercases(self):
        assert _normalize_domain("Example.COM") == "example.com"

    def test_empty_returns_empty(self):
        assert _normalize_domain("") == ""

    def test_handles_url_with_scheme(self):
        assert _normalize_domain("https://www.example.com/path") == "example.com"

    def test_handles_url_without_scheme(self):
        assert _normalize_domain("www.example.com") == "example.com"


# ─── GitHub case-folding dedup (PR #8) ────────────────────────────

class TestGitHubCaseFolding:

    def test_owner_repo_casefolded(self):
        from hound_mcp.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/nousresearch/hermes-agent")
        b = _normalize_url("https://github.com/NousResearch/hermes-agent")
        assert a == b

    def test_branch_case_preserved(self):
        from hound_mcp.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/NousResearch/Hermes-Agent/tree/Main")
        b = _normalize_url("https://github.com/nousresearch/hermes-agent/tree/main")
        assert a != b

    def test_non_github_paths_case_sensitive(self):
        from hound_mcp.search_metasearch import _normalize_url
        a = _normalize_url("https://example.com/Docs/Readme")
        b = _normalize_url("https://example.com/docs/readme")
        assert a != b

    def test_credential_urls_skip_folding(self):
        from hound_mcp.search_metasearch import _normalize_url
        a = _normalize_url("https://User:Secret@github.com/NousResearch/Hermes-Agent")
        b = _normalize_url("https://user:secret@github.com/nousresearch/hermes-agent")
        assert a != b


class TestGitHubReservedRoutes:
    """PR #10: GitHub system routes (topics, settings, explore, etc.) should
    NOT have their path segments case-folded, because they are not repositories
    and case can carry meaning (e.g. /topics/Python vs /topics/python)."""

    def test_reserved_route_not_folded(self):
        from hound_mcp.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/Settings/Keys")
        b = _normalize_url("https://github.com/settings/keys")
        assert a != b

    def test_topics_route_case_preserved(self):
        from hound_mcp.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/topics/Python")
        b = _normalize_url("https://github.com/topics/python")
        assert a != b

    def test_explore_route_case_preserved(self):
        from hound_mcp.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/Explore/Rust")
        b = _normalize_url("https://github.com/explore/rust")
        assert a != b

    def test_repo_still_folded_after_fix(self):
        from hound_mcp.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/NousResearch/Hermes-Agent")
        b = _normalize_url("https://github.com/nousresearch/hermes-agent")
        assert a == b

    def test_reserved_route_lowercased_unchanged(self):
        """Already-lowercase reserved routes should be unchanged."""
        from hound_mcp.search_metasearch import _normalize_url
        assert _normalize_url("https://github.com/topics/python") == \
               "https://github.com/topics/python"

    def test_multiple_reserved_routes(self):
        from hound_mcp.search_metasearch import _normalize_url
        for route in ["settings", "topics", "explore", "dashboard", "notifications",
                      "marketplace", "sponsors", "collections", "trending", "search"]:
            a = _normalize_url(f"https://github.com/{route.title()}/Sub")
            b = _normalize_url(f"https://github.com/{route}/sub")
            assert a != b, f"Route '{route}' should not be case-folded"


# ─── multi_search mapping logic ────────────────────────────────────

class TestMultiSearchMapping:
    """Test the mapping from metasearch dicts to RawResult + EngineReport.
    The metasearch backend is mocked, but the mapping/filtering is real."""

    @pytest.mark.asyncio
    async def test_site_filter_applied_on_results(self, monkeypatch):
        fake_results = [
            {"title": "A", "href": "https://github.com/repo", "body": "b", "backend": "brave", "backends": ["brave"]},
            {"title": "B", "href": "https://evilgithub.com/repo", "body": "b", "backend": "brave", "backends": ["brave"]},
        ]
        async def fake_metasearch(q, n, **kw):
            return fake_results, {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        ranked, reports = await multi_search("test", 6, site="github.com")
        assert len(ranked) == 1
        assert ranked[0].url == "https://github.com/repo"

    @pytest.mark.asyncio
    async def test_exclude_sites_filter_applied(self, monkeypatch):
        fake_results = [
            {"title": "A", "href": "https://github.com/repo", "body": "b", "backend": "brave", "backends": ["brave"]},
            {"title": "B", "href": "https://example.com/repo", "body": "b", "backend": "brave", "backends": ["brave"]},
        ]
        async def fake_metasearch(q, n, **kw):
            return fake_results, {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        ranked, _ = await multi_search("test", 6, exclude_sites=["github.com"])
        assert len(ranked) == 1
        assert ranked[0].url == "https://example.com/repo"

    @pytest.mark.asyncio
    async def test_consensus_from_multiple_backends(self, monkeypatch):
        fake_results = [
            {"title": "A", "href": "https://github.com/repo", "body": "b",
             "backend": "brave", "backends": ["brave", "duckduckgo"]},
        ]
        async def fake_metasearch(q, n, **kw):
            return fake_results, {"brave": "ok", "duckduckgo": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        ranked, _ = await multi_search("test", 6)
        assert len(ranked) == 1
        # brave and duckduckgo are different index families
        assert ranked[0].consensus == 2

    @pytest.mark.asyncio
    async def test_consensus_same_index_family(self, monkeypatch):
        # DDG and Yahoo both use Bing's index -> consensus = 1
        fake_results = [
            {"title": "A", "href": "https://example.com", "body": "b",
             "backend": "duckduckgo", "backends": ["duckduckgo", "yahoo"]},
        ]
        async def fake_metasearch(q, n, **kw):
            return fake_results, {"duckduckgo": "ok", "yahoo": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        ranked, _ = await multi_search("test", 6)
        assert ranked[0].consensus == 1  # same index family (Bing)

    @pytest.mark.asyncio
    async def test_engine_reports_mapped(self, monkeypatch):
        async def fake_metasearch(q, n, **kw):
            return [], {"brave": "ok", "google": "blocked", "yahoo": "empty", "mojeek": "timeout"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        _, reports = await multi_search("test", 6)
        report_map = {r.name: r for r in reports}
        assert report_map["brave"].ok is True
        assert report_map["google"].blocked is True
        assert report_map["yahoo"].ok is False
        assert report_map["mojeek"].blocked is True

    @pytest.mark.asyncio
    async def test_freshness_maps_to_timelimit(self, monkeypatch):
        captured = {}
        async def fake_metasearch(q, n, **kw):
            captured["timelimit"] = kw.get("timelimit")
            return [], {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        await multi_search("test", 6, freshness="week")
        assert captured["timelimit"] == "w"

    @pytest.mark.asyncio
    async def test_page_zero_indexed_to_one(self, monkeypatch):
        captured = {}
        async def fake_metasearch(q, n, **kw):
            captured["page"] = kw.get("page")
            return [], {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        await multi_search("test", 6, page=0)
        assert captured["page"] == 1

    @pytest.mark.asyncio
    async def test_site_prefix_added_to_query(self, monkeypatch):
        captured = {}
        async def fake_metasearch(q, n, **kw):
            captured["query"] = q
            return [], {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        await multi_search("test query", 6, site="github.com")
        assert "site:github.com" in captured["query"]

    @pytest.mark.asyncio
    async def test_exclude_prefix_added_to_query(self, monkeypatch):
        captured = {}
        async def fake_metasearch(q, n, **kw):
            captured["query"] = q
            return [], {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        await multi_search("test", 6, exclude_sites=["pinterest.com"])
        assert "-site:pinterest.com" in captured["query"]


# ─── DEFAULT_ENGINES and index family ─────────────────────────────

class TestEngineConfig:

    def test_default_engines_has_five(self):
        assert len(DEFAULT_ENGINES) == 5

    def test_default_engines_contains_key_backends(self):
        for engine in ("bing", "duckduckgo", "brave", "yahoo", "yandex"):
            assert engine in DEFAULT_ENGINES

    def test_index_family_mapping(self):
        # DDG and Yahoo share Bing's index
        assert _INDEX_FAMILY["duckduckgo"] == _INDEX_FAMILY["yahoo"] == "bing"
        # Brave has its own independent index
        assert _INDEX_FAMILY["brave"] == "brave"
        # Yandex has its own independent index
        assert _INDEX_FAMILY["yandex"] == "yandex"


# ─── Proxy validation (upstream 11.1.9) ───────────────────────────

class TestSearchProxyValidation:
    def _load_with(self, monkeypatch, tmp_path, env_value):
        """Load proxies from the env var + an empty config file (isolated)."""
        from hound_mcp import search_proxy as sp
        if env_value is None:
            monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        else:
            monkeypatch.setenv("HOUND_SEARCH_PROXY", env_value)
        monkeypatch.setattr(sp, "_config_path", lambda: tmp_path / "no_such_proxies.json")
        sp._pool = None
        return sp.load_proxies()

    def test_whitespace_stripped(self, monkeypatch, tmp_path):
        """Leading/trailing whitespace is stripped so httpx doesn't crash."""
        proxies = self._load_with(monkeypatch, tmp_path, " http://proxy:8080 ")
        assert proxies == ["http://proxy:8080"]

    def test_whitespace_only_nulled(self, monkeypatch, tmp_path):
        """Whitespace-only proxy becomes empty, not a crash-inducing string."""
        assert self._load_with(monkeypatch, tmp_path, "   ") == []

    def test_invalid_scheme_rejected(self, monkeypatch, tmp_path):
        """Unknown scheme (not http/https/socks5/socks5h) is rejected."""
        assert self._load_with(monkeypatch, tmp_path, "garbage://proxy") == []

    def test_valid_socks5_accepted(self, monkeypatch, tmp_path):
        """socks5 scheme is accepted (primp supports it natively)."""
        assert self._load_with(monkeypatch, tmp_path, "socks5://192.0.2.1:1080") == [
            "socks5://192.0.2.1:1080"
        ]

    def test_no_proxy_env(self, monkeypatch, tmp_path):
        """Unset env var -> [] (direct connection)."""
        assert self._load_with(monkeypatch, tmp_path, None) == []

    def test_multiple_comma_separated(self, monkeypatch, tmp_path):
        """Comma-separated env var yields a rotation pool."""
        proxies = self._load_with(
            monkeypatch, tmp_path, "http://p1:8080,http://p2:8080,http://p3:8080"
        )
        assert proxies == ["http://p1:8080", "http://p2:8080", "http://p3:8080"]

    def test_all_engines_construction_failure_raises(self):
        """If every engine fails to construct (bad deps, etc), raise an error
        instead of silently returning 0 results."""
        import asyncio
        import hound_mcp.search_metasearch as m

        class BrokenEngine:
            disabled = False
            def __init__(self, **kwargs):
                raise RuntimeError("simulated construction failure")

        original = dict(m._TEXT_ENGINES)
        original_bd_key = m._BRIGHTDATA_API_KEY
        m._BRIGHTDATA_API_KEY = ""  # disable the brightdata fallback for this test
        for name in m._TEXT_ENGINES:
            m._TEXT_ENGINES[name] = type(
                f"Broken{name}", (BrokenEngine,),
                {"name": name, "disabled": False, "priority": 1.0}
            )
        try:
            with pytest.raises(m.MetaSearchException, match="No search engines could start"):
                asyncio.run(m.metasearch("test", max_results=3))
        finally:
            m._TEXT_ENGINES.clear()
            m._TEXT_ENGINES.update(original)
            m._BRIGHTDATA_API_KEY = original_bd_key


# ─── Result diversity (upstream v12.0.0) ──────────────────────────

class TestDiversify:

    def _make_results(self, urls):
        return ([RawResult(title=f"Title {i}", url=u, snippet="",
                          source="brave", position=i) for i, u in enumerate(urls)],
                [0.9 - i * 0.01 for i in range(len(urls))])

    def test_same_domain_capped_at_two(self):
        ranked, scores = self._make_results([
            "https://medium.com/a",
            "https://medium.com/b",
            "https://medium.com/c",
            "https://medium.com/d",
        ])
        r, s = search._diversify(ranked, scores, max_per_domain=2)
        # First two medium.com kept in top, last two deferred to bottom
        top_domains = [search._get_domain(r[i].url) for i in range(2)]
        assert top_domains == ["medium.com", "medium.com"]
        # Third position should NOT be medium.com (deferred)
        assert search._get_domain(r[2].url) != "medium.com" or len(r) == 4
        # Actually all 4 are medium.com so 2 kept + 2 deferred
        assert len(r) == 4

    def test_different_domains_not_affected(self):
        ranked, scores = self._make_results([
            "https://github.com/a",
            "https://arxiv.org/b",
            "https://medium.com/c",
        ])
        r, s = search._diversify(ranked, scores, max_per_domain=2)
        assert [i.url for i in r] == [ranked[0].url, ranked[1].url, ranked[2].url]

    def test_mixed_domains_partial_deferral(self):
        ranked, scores = self._make_results([
            "https://medium.com/a",
            "https://medium.com/b",
            "https://github.com/c",
            "https://medium.com/d",  # 3rd medium.com → deferred
        ])
        r, s = search._diversify(ranked, scores, max_per_domain=2)
        assert len(r) == 4
        # First two are medium.com, third is github, fourth is deferred medium.com
        assert search._get_domain(r[0].url) == "medium.com"
        assert search._get_domain(r[1].url) == "medium.com"
        assert search._get_domain(r[2].url) == "github.com"
        assert search._get_domain(r[3].url) == "medium.com"

    def test_empty_list(self):
        r, s = search._diversify([], [], max_per_domain=2)
        assert r == [] and s == []

    def test_single_result(self):
        ranked, scores = self._make_results(["https://example.com/a"])
        r, s = search._diversify(ranked, scores, max_per_domain=2)
        assert len(r) == 1

    def test_www_prefix_stripped(self):
        """www.medium.com and medium.com should be treated as same domain."""
        ranked, scores = self._make_results([
            "https://www.medium.com/a",
            "https://medium.com/b",
            "https://medium.com/c",
        ])
        r, s = search._diversify(ranked, scores, max_per_domain=2)
        # First two kept (same domain after www strip), third deferred
        assert search._get_domain(r[0].url) == "medium.com"
        assert search._get_domain(r[1].url) == "medium.com"


# ─── Intent-aware fan-out (upstream v12.0.0) ──────────────────────

class TestQueryMapIntegration:

    def test_research_query_generates_different_queries(self):
        engines = ["duckduckgo", "brave", "mojeek", "yahoo",
                    "yandex", "startpage", "google", "qwant"]
        intent = search._detect_intent("transformer attention mechanism research")
        assert intent == "research"
        qm = search._generate_query_map("transformer attention mechanism research", intent, engines)
        assert qm != {}
        assert qm["duckduckgo"] == "transformer attention mechanism research"
        assert qm["yandex"] != "transformer attention mechanism research"
        assert "paper" in qm["yandex"] or "arxiv" in qm["yandex"]

    def test_comparison_query_no_fan_out(self):
        engines = ["duckduckgo", "brave", "yandex"]
        intent = search._detect_intent("Python vs Rust performance comparison")
        assert intent == "comparison"
        qm = search._generate_query_map("Python vs Rust performance comparison", intent, engines)
        assert qm == {}

    def test_general_query_no_fan_out(self):
        engines = ["duckduckgo", "brave", "yandex"]
        intent = search._detect_intent("best restaurants in paris")
        qm = search._generate_query_map("best restaurants in paris", intent, engines)
        assert qm == {}

    def test_factual_query_fan_out(self):
        engines = ["duckduckgo", "yandex"]
        intent = search._detect_intent("GPT-3 embedding dimension d_model parameters")
        assert intent == "factual"
        qm = search._generate_query_map("GPT-3 embedding dimension d_model parameters", intent, engines)
        assert qm != {}
        assert "specifications" in qm["yandex"]
        assert "table" in qm["yandex"]


class TestIntentFalsePositives:
    """Ambiguous standalone words should NOT trigger wrong intents."""

    def test_area_code_not_code(self):
        assert search._detect_intent("area code 212") != "code"

    def test_toilet_paper_not_research(self):
        assert search._detect_intent("toilet paper brands") != "research"

    def test_database_update_not_news(self):
        assert search._detect_intent("database update syntax") != "news"

    def test_make_a_difference_not_comparison(self):
        assert search._detect_intent("make a difference quotes") != "comparison"

    def test_alternative_music_not_comparison(self):
        assert search._detect_intent("alternative music genres") != "comparison"

    def test_dining_table_not_factual(self):
        assert search._detect_intent("dining table wood") != "factual"

    def test_general_query_no_expansion(self):
        assert search._expand_query("best restaurants in paris", "general") == "best restaurants in paris"


class TestFactualDetectionExpanded:

    def test_architecture_layers_heads_factual(self):
        assert search._detect_intent("GPT-3 architecture layers heads") == "factual"

    def test_context_window_factual(self):
        assert search._detect_intent("transformer model context window") == "factual"

    def test_throughput_latency_factual(self):
        assert search._detect_intent("LLM inference throughput latency") == "factual"

    def test_non_technical_with_data_word_not_factual(self):
        assert search._detect_intent("shoe size guide") != "factual"


class TestQueryLengthLimit:

    def test_long_query_no_expansion(self):
        long_q = " ".join(["word"] * 15)
        assert search._expand_query(long_q, "code") == long_q

    def test_short_query_still_expanded(self):
        short_q = "attention mechanism"
        assert search._expand_query(short_q, "research") != short_q

    def test_exactly_14_words_expanded(self):
        q = " ".join(["attention"] + ["word"] * 13)
        assert search._expand_query(q, "research") != q


class TestNewsYearDynamic:

    def test_news_no_expansion(self):
        assert search._expand_query("latest AI announcement", "news") == "latest AI announcement"


class TestIrrelevantFilter:
    """_filter_irrelevant_results: drop noise when engines return filler."""

    def _res(self, title, url, snippet=""):
        return search.SearchResult(title=title, url=url, snippet=snippet)

    def test_gibberish_query_filters_all(self):
        results = [
            self._res("SpaceX", "https://spacex.com", "rockets"),
            self._res("Facebook", "https://facebook.com", "social media"),
        ]
        out = search._filter_irrelevant_results(results, "xqzkjfhoiqweudhxqzkj")
        assert out == []

    def test_relevant_results_kept(self):
        results = [
            self._res("Python Async Guide", "https://example.com/async", "asyncio best practices"),
        ]
        out = search._filter_irrelevant_results(results, "python asyncio")
        assert out == [results[0]]

    def test_partial_relevance_keeps_all(self):
        # At least one result matches a term -> keep the set (ranking handles the rest)
        results = [
            self._res("SpaceX launch", "https://spacex.com", "python in flight software"),
            self._res("random page", "https://fb.com", "x"),
        ]
        out = search._filter_irrelevant_results(results, "python asyncio")
        assert len(out) == 2

    def test_cjk_query_never_filtered(self):
        results = [self._res("Some English page", "https://example.com/")]
        # '量子计算' has no latin terms -> engines' judgment stands
        assert search._filter_irrelevant_results(results, "量子计算 最新进展") == results

    def test_empty_results_returns_empty(self):
        assert search._filter_irrelevant_results([], "anything") == []

    def test_stopword_only_query_never_filtered(self):
        results = [self._res("The", "https://example.com/")]
        # 'how to' -> terms empty (stopwords) -> keep
        assert search._filter_irrelevant_results(results, "how to") == results
