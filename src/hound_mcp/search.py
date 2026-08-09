"""Hound local web search (v7 flagship: keyless, no-account, fully local).

Scrapes public search engines (DuckDuckGo, Bing, Qwant, Wikipedia) via the
hound-native engine layer in search_engines.py - no third-party API, no key, no
account. Results are merged across engines, deduped by normalized URL, and
ranked. Merging INDEPENDENT indexes gives a free authority signal: a URL
returned by several engines is a consensus hit (engines_consensus field) and
gets a ranking boost. Every result also carries a relevance_score and a
fetch_relevance tier so the agent fetches the right URLs via smart_fetch itself
(search returns URLs + ranking, NOT page content - the agent decides what to fetch).

Rerank: neural (a local ONNX cross-encoder on snippets, needs [all]; the ONLY
reranker - BM25 was removed as redundant since neural matches its speed and
ranks better), find_similar (pass url=, find pages similar to it). Lean installs
without the model fall back to cross-engine consensus + engine-position order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime
from time import time
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from hound_mcp.cache import get_cached, set_cached
from hound_mcp.security import validate_search_query, validate_url, redact_api_key, SecurityError
from hound_mcp.search_engines import (
    RawResult, multi_search, EngineReport, DEFAULT_ENGINES,
    fetch_source_for_similar, _INDEX_FAMILY,
)

logger = logging.getLogger("hound-mcp.search")


def neural_rerank(query: str, ranked: list[RawResult]):
    from hound_mcp.reranker import rerank
    return rerank(query, ranked)


def unavailable_reason() -> str:
    from hound_mcp.reranker import unavailable_reason as _unavailable_reason
    return _unavailable_reason()


def get_reranker():
    from hound_mcp.reranker import get_reranker as _get_reranker
    return _get_reranker()


async def ensure_reranker(*, download: bool = True):
    from hound_mcp.reranker import ensure_reranker as _ensure_reranker
    return await _ensure_reranker(download=download)


SEARCH_CACHE_TTL = 300  # 5 minutes


# ─── related-query mining (extractive, no LLM) ──────────────────────────────
# Mine follow-up queries from the result titles + snippets hound already has.
# Engine-agnostic and robust: no dependence on fragile per-engine "related
# searches" SERP markup (which changes often). Ranks bigrams by document
# frequency across the result set, drops ones that overlap the original query,
# and returns the top N as suggested refinements.

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "has", "have", "had", "you", "your", "its", "our", "not", "but", "can",
    "will", "into", "via", "using", "use", "how", "what", "when", "why", "who",
    "which", "about", "also", "more", "most", "than", "then", "them", "they",
}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'+-]{2,}")


def _query_tokens(query: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(query or "") if w.lower() not in _STOPWORDS}


def _related_queries(query: str, results: list["SearchResult"], *, n: int = 6) -> list[str]:
    """Mine follow-up queries from result titles + snippets.

    Ranks bigrams by document frequency (in how many results they appear),
    drops bigrams that overlap the original query or duplicate each other, and
    falls back to high-frequency unigrams if too few bigrams. Returns up to n
    phrases. Robust + cheap; never raises.
    """
    if not results:
        return []
    q_tokens = _query_tokens(query)
    docs: list[list[str]] = []
    for r in results:
        text = (f"{r.title} {r.snippet}").lower()
        words = [w for w in _WORD_RE.findall(text) if w not in _STOPWORDS]
        docs.append(words)
    if not docs:
        return []

    bigram_docfreq: Counter[str] = Counter()
    unigram_docfreq: Counter[str] = Counter()
    for words in docs:
        uniq_bi = set()
        for i in range(len(words) - 1):
            a, b = words[i], words[i + 1]
            if len(a) < 3 or len(b) < 3:
                continue
            uniq_bi.add(f"{a} {b}")
        for bi in uniq_bi:
            bigram_docfreq[bi] += 1
        for w in set(words):
            if len(w) >= 3:
                unigram_docfreq[w] += 1

    def _overlaps_query(phrase: str) -> bool:
        toks = phrase.split()
        if not toks:
            return True
        if not [t for t in toks if t not in q_tokens]:  # every token already in query
            return True
        pq, pphrase = query.lower(), phrase
        if pphrase in pq or pq in pphrase:
            return True
        return False

    scored = []
    for bi, df in bigram_docfreq.items():
        if df < 2:  # appears in only one result -> not a pattern
            continue
        if _overlaps_query(bi):
            continue
        scored.append((df, bi))
    scored.sort(key=lambda x: (-x[0], x[1]))

    out: list[str] = []
    seen_words: set[str] = set()
    for _df, bi in scored:
        toks = bi.split()
        if len(set(toks) & seen_words) >= 2:  # near-dup of a kept suggestion
            continue
        out.append(bi)
        seen_words.update(toks)
        if len(out) >= n:
            break

    if len(out) < n:  # fall back to unigrams
        for w, df in unigram_docfreq.most_common():
            if df < 2 or w in q_tokens or w in seen_words:
                continue
            out.append(w)
            seen_words.add(w)
            if len(out) >= n:
                break
    return out[:n]


# ─── zero-result query rewrite ────────────────────────────────────────────────

def _rewrite_query(query: str) -> str:
    """Attempt a simple query rewrite when zero results are returned.

    Strategies (tried in order, first that produces a different query wins):
    1. Remove site: prefix (may be too restrictive)
    2. Drop the longest word (possible typo or overly specific term)
    3. Drop the last word (may be a trailing qualifier)

    Returns the rewritten query, or the original if no rewrite is possible.
    """
    q = query.strip()
    if not q:
        return q
    # Strategy 1: remove site: filter
    if "site:" in q:
        rewritten = re.sub(r'\bsite:\S+\s*', '', q).strip()
        if rewritten and rewritten != q:
            return rewritten
    # Strategy 2: drop the longest word (possible typo)
    words = q.split()
    if len(words) >= 3:
        longest_idx = max(range(len(words)), key=lambda i: len(words[i]))
        rewritten = " ".join(w for i, w in enumerate(words) if i != longest_idx)
        if rewritten.strip() and rewritten.strip() != q:
            return rewritten.strip()
    # Strategy 3: drop the last word
    if len(words) >= 2:
        rewritten = " ".join(words[:-1])
        if rewritten.strip() and rewritten.strip() != q:
            return rewritten.strip()
    return q


# ─── response model ──────────────────────────────────────────────────────────

class SearchResult(BaseModel):
    title: str = Field(description="Result title")
    url: str = Field(description="Result URL")
    snippet: str = Field(default="", description="Result snippet from the engine")
    source: str = Field(default="", description="Backend(s) that returned this result (duckduckgo/brave/yahoo/yandex/wikipedia/grokipedia). Multiple = cross-backend consensus.")
    position: int = Field(default=0, description="1-indexed rank after merge + rerank")
    relevance_score: float = Field(default=0.0, description="0.0-1.0 relevance to the query (neural cross-encoder score in neural mode, min-max normalized), boosted by cross-backend consensus. 1.0 = most relevant in this set.")
    fetch_relevance: str = Field(default="", description="high|med|low - relative relevance hint. smart_fetch what matches your need; the tiers rank results but a lower tier can be the right one - use your judgment.")
    engines_consensus: str = Field(default="", description="How many independent indexes returned this URL (e.g. '3 of 4'). A free authority signal: a URL returned by several independent engines is more likely authoritative.")
    source_type: str = Field(default="", description="Source type from URL pattern: docs|paper|repo|blog|forum|reference|news|other. Helps pick the right source.")


class SearchResponseModel(BaseModel):
    query: str = Field(description="Search query")
    results: list[SearchResult] = Field(description="Ranked search results (URLs + ranking, not page content)")
    total_results: int = Field(default=0, description="Results returned")
    engines_used: list[str] = Field(default=[], description="Engines that returned results")
    engine_blocked: list[str] = Field(default=[], description="Engines that did NOT contribute (rate-limited/CAPTCHA'd/timed out/parsed no results). Results still came from engines_used; retry shortly for more recall.")
    rerank_mode: str = Field(default="merge", description="Rerank used: merge|neural|find_similar.")
    cached: bool = Field(default=False, description="Served from cache?")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error message (empty = ok)")
    fetch_hint: str = Field(default="", description="How many high/med/low results + which to smart_fetch first")
    related_queries: list[str] = Field(default=[], description="Follow-up queries worth searching next, mined extractively from the result titles+snippets (no LLM). Empty if none derived. Use to refine a broad query.")
    summary: str = Field(default="", description="One-line status of the search (counts + engines + rerank).")
    next_action: str = Field(default="", description="The obvious next call: fetch the high results, rephrase, retry, etc. Empty = nothing more to do.")
    fetched_pages: list[dict] = Field(default=[], description="When fetch_content=true: auto-fetched page content for top results. Each item: {url, title, content, content_ok}.")


# ─── source type detection (zero-latency URL pattern matching) ──────────────

_DOCS_DOMAINS = frozenset({
    # Python
    "docs.python.org", "realpython.com", "python.readthedocs.io",
    # AI/ML
    "docs.anthropic.com", "docs.openai.com", "platform.openai.com",
    "pytorch.org", "tensorflow.org", "huggingface.co",
    "docs.llama.com", "docs.mistral.ai", "docs.cohere.com",
    # Web
    "developer.mozilla.org", "developers.google.com", "web.dev",
    "react.dev", "vuejs.org", "angular.dev", "svelte.dev", "nextjs.org",
    "nodejs.org", "expressjs.com", "fastapi.tiangolo.com",
    # Systems
    "docs.rs", "doc.rust-lang.org", "go.dev", "docs.golang.org",
    "learn.microsoft.com", "docs.microsoft.com", "docs.github.com",
    # Cloud
    "docs.aws.amazon.com", "cloud.google.com", "learn.microsoft.com",
    "docs.docker.com", "kubernetes.io",
    # Mobile
    "developer.android.com", "developer.apple.com", "flutter.dev",
    "docs.flutter.dev", "kotlinlang.org", "swift.org",
    # Data
    "pandas.pydata.org", "numpy.org", "scipy.org", "docs.sqlalchemy.org",
    # General
    "readthedocs.io", "gitbook.io", "docusaurus.io",
})
_PAPER_DOMAINS = frozenset({
    "arxiv.org", "dl.acm.org", "ieee.org", "ieeexplore.ieee.org",
    "openreview.net", "semanticscholar.org", "scholar.google.com",
    "paperswithcode.com", "biorxiv.org", "medrxiv.org",
    "nature.com", "science.org", "sciencedirect.com", "springer.com",
    "link.springer.com", "wiley.com", "pnas.org", "thelancet.com",
    "nejm.org", "cell.com", "pubmed.ncbi.nlm.nih.gov",
})
_REPO_DOMAINS = frozenset({
    "github.com", "gitlab.com", "bitbucket.org", "codeberg.org",
    "sourceforge.net", "gitee.com", "gitea.com", "sr.ht",
    "npmjs.com", "pypi.org", "crates.io", "pkg.go.dev",
})
_FORUM_DOMAINS = frozenset({
    "stackoverflow.com", "serverfault.com", "superuser.com", "askubuntu.com",
    "stackexchange.com", "mathoverflow.com",
    "reddit.com", "news.ycombinator.com", "discuss.python.org",
    "forum.rust-lang.org", "internals.rust-lang.org",
    "v2ex.com", "segmentfault.com", "zhihu.com",
    "discord.com",
})
_BLOG_DOMAINS = frozenset({
    "medium.com", "substack.com", "dev.to", "hashnode.com",
    "freecodecamp.org", "blogspot.com", "wordpress.com",
    "ghost.io", "notion.so", "telegra.ph",
    "juejin.cn", "csdn.net", "cnblogs.com", "jianshu.com",
})
_REFERENCE_DOMAINS = frozenset({
    "wikipedia.org", "wikimedia.org", "britannica.com",
    "w3schools.com", "geeksforgeeks.org", "tutorialspoint.com",
    "mdn.io", "caniuse.com", "compat-table.github.io",
})
_NEWS_DOMAINS = frozenset({
    "reuters.com", "bloomberg.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "wired.com", "cnet.com", "zdnet.com",
    "bbc.com", "bbc.co.uk", "nytimes.com", "theguardian.com",
    "cnbc.com", "ft.com", "wsj.com", "apnews.com",
    "36kr.com", "ithome.com", "huxiu.com", "pingwest.com",
})


def _get_domain(url: str) -> str:
    """Extract registrable domain from URL (strip www. and subdomains for grouping)."""
    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _source_type(url: str) -> str:
    """Classify a URL by domain pattern: docs|paper|repo|forum|reference|blog|news|other."""
    host = _get_domain(url)
    if not host:
        return "other"

    def _matches(domain_set: frozenset) -> bool:
        if host in domain_set:
            return True
        return any(host.endswith(f".{d}") for d in domain_set)

    if _matches(_DOCS_DOMAINS):
        return "docs"
    if _matches(_PAPER_DOMAINS):
        return "paper"
    if _matches(_REPO_DOMAINS):
        return "repo"
    if _matches(_FORUM_DOMAINS):
        return "forum"
    if _matches(_REFERENCE_DOMAINS):
        return "reference"
    if _matches(_NEWS_DOMAINS):
        return "news"
    if _matches(_BLOG_DOMAINS):
        return "blog"
    # Path-based heuristics
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        path = ""
    if "/docs/" in path or "/api/" in path:
        return "docs"
    if "/blog/" in path or "/post/" in path:
        return "blog"
    return "other"


def _diversify(ranked: list, scores: list[float], max_per_domain: int = 2) -> tuple[list, list[float]]:
    """Cap same-domain results in top positions. Excess deferred to bottom (not dropped)."""
    if not ranked:
        return ranked, scores
    domain_counts: Counter = Counter()
    kept, kept_scores = [], []
    deferred, deferred_scores = [], []
    for r, s in zip(ranked, scores):
        domain = _get_domain(r.url)
        if domain_counts[domain] < max_per_domain:
            kept.append(r)
            kept_scores.append(s)
            domain_counts[domain] += 1
        else:
            deferred.append(r)
            deferred_scores.append(s)
    return kept + deferred, kept_scores + deferred_scores


# ─── tier derivation + hint ──────────────────────────────────────────────────

def _tier(score: float, rank: int, total: int) -> str:
    """Derive high|med|low from relevance score + rank. Top result is never 'low'."""
    if score >= 0.5 or rank == 1:
        return "high"
    if score >= 0.15:
        return "med"
    if rank <= max(2, total // 3):
        return "med"
    return "low"


def compute_fetch_hint(results: list[SearchResult]) -> str:
    if not results:
        return ""
    high = sum(1 for r in results if r.fetch_relevance == "high")
    med = sum(1 for r in results if r.fetch_relevance == "med")
    low = sum(1 for r in results if r.fetch_relevance == "low")
    return (f"{high} high, {med} med, {low} low. Ranked by relevance_score; "
            f"smart_fetch what fits your need (high first, but a lower tier can be the right call).")


def _search_summary(query: str, results: list[SearchResult], engines_used: list[str],
                    rerank_mode: str) -> str:
    """One-line status for the agent (counts + engines + rerank mode)."""
    high = sum(1 for r in results if r.fetch_relevance == "high")
    med = sum(1 for r in results if r.fetch_relevance == "med")
    low = sum(1 for r in results if r.fetch_relevance == "low")
    eng = ",".join(engines_used) if engines_used else "none"
    return (f"Searched {query[:60]!r} -> {len(results)} results "
            f"({high} high, {med} med, {low} low) from {eng}; rerank={rerank_mode}.")


def _search_next_action(results: list[SearchResult], engine_blocked: list[str],
                         error: str, engines_used: list[str] | None = None) -> str:
    """A judgment-empowering nudge, not a rigid directive. The ranking is a HINT:
    the agent may legitimately need a lower-ranked result, so we point it at the
    signals (relevance_score + fetch_relevance) and trust it to pick, instead of
    prescribing 'fetch N'. This avoids the LLM stressing over whether to 'break'
    the instruction when a lower-ranked result is the one it actually needs."""
    if not results:
        if error and ("rate-limited" in error.lower() or "timed out" in error.lower() or engine_blocked):
            return ("No results (engines rate-limited/timed out). Retry in a moment, "
                    "or set HOUND_SEARCH_PROXY for sustained heavy use.")
        return "No results. Rephrase (more specific / different terms) or try mode=neural for semantic matching."
    high = [r for r in results if r.fetch_relevance == "high"]
    base = ("Results are ranked by relevance + cross-engine consensus (engines_consensus = how many independent indexes agree). "
            "smart_fetch the ones that match what you actually need - the ranking is a hint, "
            "not a directive; a lower-ranked result can be the right one, so trust your judgment.")
    if not high:
        base += " No 'high' matches - if none of these fit, rephrase (more specific) or try mode=neural."
    if engine_blocked:
        total_engines = len(engine_blocked) + len(engines_used or [])
        blocked_ratio = len(engine_blocked) / max(1, total_engines)
        if blocked_ratio >= 0.6:
            base += (f" WARNING: {len(engine_blocked)} of {total_engines} "
                    f"engines were rate-limited/blocked - results have LOW diversity "
                    f"(only from {', '.join(engines_used or ['unknown'])}). "
                    f"For better recall: set HOUND_SEARCH_PROXY, retry in 60s, or rephrase the query.")
        else:
            base += " Some engines didn't contribute; retry shortly for more recall."
    return base


# ─── filter validation (site/exclude/location/language/page) ─────────────────

def _validate_filters(site, exclude_sites, location, language, page):
    import re
    _domain_re = re.compile(r"^(?!-)[A-Za-z0-9.-]{1,253}(?<!-)$")
    if site is not None:
        if not isinstance(site, str) or not _domain_re.match(site) or "." not in site:
            raise SecurityError(f"Invalid site filter: {site!r} (must be a domain like 'docs.python.org')")
    if exclude_sites is not None:
        if not isinstance(exclude_sites, list) or len(exclude_sites) > 20:
            raise SecurityError("exclude_sites must be a list of <= 20 domains")
        for d in exclude_sites:
            if not isinstance(d, str) or not _domain_re.match(d) or "." not in d:
                raise SecurityError(f"Invalid exclude_sites entry: {d!r}")
    if location is not None:
        if not isinstance(location, str) or not re.match(r"^[A-Za-z]{2}(-[A-Za-z]{2})?$", location):
            raise SecurityError(f"Invalid location: {location!r} (e.g. 'US' or 'us-en')")
    if language is not None:
        if not isinstance(language, str) or not re.match(r"^[a-z]{2}$", language):
            raise SecurityError(f"Invalid language: {language!r} (2-letter code, e.g. 'en')")
    if page is not None:
        if isinstance(page, bool) or not isinstance(page, int) or page < 0 or page > 10:
            raise SecurityError(f"Invalid page: {page!r} (0-10)")


def _validate_engines(engines):
    if engines is None:
        return None
    if not isinstance(engines, list) or not engines:
        raise SecurityError("engines must be a non-empty list")
    if len(engines) > 9:
        raise SecurityError("engines list too long (max 9)")
    valid = set(DEFAULT_ENGINES) | {"wikipedia", "grokipedia", "yahoo", "bing", "ddg"}
    for e in engines:
        if not isinstance(e, str) or e.lower() not in valid:
            raise SecurityError(f"Invalid engine: {e!r} (one of {sorted(valid)})")
    return [e.lower() for e in engines]


def _validate_freshness(freshness):
    if freshness is None:
        return None
    if freshness not in ("day", "week", "month", "year"):
        raise SecurityError(f"Invalid freshness: {freshness!r} (day|week|month|year)")
    return freshness


# Implemented rerank modes (find_similar = URL->similar). Unknown modes are
# rejected so the schema does not advertise a mode that is not wired.
_IMPLEMENTED_MODES = ("auto", "neural", "find_similar")


def _validate_mode(mode):
    if mode is None:
        return "auto"
    if not isinstance(mode, str) or mode.lower() not in _IMPLEMENTED_MODES:
        raise SecurityError(f"Invalid mode: {mode!r} (auto|neural|find_similar)")
    return mode.lower()


def _rank(query: str, ranked: list[RawResult], mode: str):
    """Apply neural rerank (the ONLY reranker; BM25 was removed as redundant -
    neural matches its speed and ranks better). Returns (ranked_list, scores,
    mode_used, note).

    mode='auto'/'neural': use the local ONNX cross-encoder if available
    (hound-mcp[all] + model cached), else fall back to cross-engine consensus +
    engine-position order (no lexical rerank). 'neural' surfaces a note when
    unavailable; 'auto' is silent (expected on lean installs).
    """
    note = ""
    if mode in ("neural", "auto"):
        pairs = neural_rerank(query, ranked)
        if pairs is not None:
            return [r for r, _ in pairs], [s for _, s in pairs], "neural", note
        if mode == "neural":
            note = ("neural rerank unavailable - using consensus + engine-position order. " +
                    (unavailable_reason() or "install hound-mcp[all] and retry"))
    # Fallback (lean install / model missing): no lexical rerank. Score by position
    # so tiers derive sensibly; the caller's consensus boost adds the authority
    # signal on top.
    n = len(ranked)
    scores = [1.0 - (i / max(n, 1)) for i in range(n)]
    return list(ranked), scores, "merge", note


def _build_results(query: str, ranked: list[RawResult], scores: Optional[list[float]] = None,
                   total_families: int = 1) -> list[SearchResult]:
    """Convert RawResults (already ranked) into SearchResults with tiers + consensus."""
    total = len(ranked)
    out: list[SearchResult] = []
    for i, r in enumerate(ranked):
        score = scores[i] if scores and i < len(scores) else 0.0
        src = ",".join(r.sources) if r.sources else (r.source or "")
        consensus = f"{max(1, getattr(r, 'consensus', 1))} of {max(1, total_families)}"
        out.append(SearchResult(
            title=r.title, url=r.url, snippet=r.snippet, source=src,
            position=i + 1, relevance_score=round(score, 4),
            fetch_relevance=_tier(score, i + 1, total),
            engines_consensus=consensus,
            source_type=_source_type(r.url),
        ))
    return out


def _quality_filter(results: list[SearchResult], min_keep: int = 3) -> list[SearchResult]:
    """Drop low-relevance results instead of padding to max_results with garbage.
    A result is 'low' if fetch_relevance == 'low'. If dropping all low leaves at
    least min_keep results, drop them; otherwise keep everything (don't go below
    min_keep). Re-numbers positions 1..N after the drop. Niche/ambiguous queries
    thus return fewer good results instead of 6 padded with garbage; clear queries
    keep all (none are 'low'). No quality sacrifice - only garbage is dropped."""
    if len(results) <= min_keep:
        return results
    kept = [r for r in results if r.fetch_relevance != "low"]
    if len(kept) < min_keep:
        return results  # not enough good ones -> keep all rather than go below min_keep
    for i, r in enumerate(kept):
        r.position = i + 1
    return kept


def _filter_irrelevant_results(results: list[SearchResult], query: str) -> list[SearchResult]:
    """Drop ALL results when none contains any meaningful query term.

    Search engines sometimes answer gibberish/random queries with unrelated
    filler (e.g. DDG returns SpaceX for a random string). When the query has
    latin terms and NO result's title/snippet/url contains any of them, the
    results are pure noise — return empty so the agent doesn't chase garbage.

    Queries with no latin terms (CJK, symbols) are never filtered — the
    engines' own judgment stands, since we can't tokenize them reliably.
    """
    terms = _query_terms(query)
    if not terms:
        return results
    blob = " ".join(f"{r.title} {r.snippet} {r.url}".lower() for r in results)
    if any(t in blob for t in terms):
        return results
    return []


# ─── six-signal quality boost (zero-latency, no extra fetches) ───────────────

_TECH_QUERY_SIGNALS = frozenset({
    "model", "architecture", "api", "code", "implement", "benchmark",
    "paper", "arxiv", "github", "algorithm", "neural", "transformer",
    "layer", "parameter", "config", "schema", "table", "comparison",
    "tensor", "precision", "throughput", "latency", "inference", "training",
})

_DIGIT_RE = re.compile(r"\d{3,}")
_TABLE_MARKERS = ("table", "|", "column", "row", "\t")
_CODE_MARKERS = ("def ", "func ", "class ", "import ", "const ", "```", "=> ", "fn ")
_COMPARISON_MARKERS = ("vs", "compared", "while", "however", "whereas", "better", "faster")


def _is_technical_query(query: str) -> bool:
    q_lower = query.lower()
    return any(sig in q_lower for sig in _TECH_QUERY_SIGNALS)


def _query_terms(query: str) -> set:
    return {w.lower() for w in _WORD_RE.findall(query or "") if w.lower() not in _STOPWORDS and len(w) >= 3}


def _domain_boost(url: str, query: str, is_technical: bool) -> float:
    """Boost authoritative domains. +0.15 tech domains, +0.05 reference, +0.05 feedback. Not a blocklist."""
    host = _get_domain(url)
    if not host:
        return 0.0
    def _matches(ds):
        return host in ds or any(host.endswith(f".{d}") for d in ds)
    boost = 0.0
    if is_technical:
        if _matches(_DOCS_DOMAINS) or _matches(_REPO_DOMAINS) or _matches(_PAPER_DOMAINS):
            boost += 0.15
        elif _matches(_FORUM_DOMAINS):
            boost += 0.08
    if _matches(_REFERENCE_DOMAINS):
        boost += 0.05
    # Feedback boost: domains the agent previously found useful
    if host in _feedback_domains():
        boost += 0.05
    return boost


# ─── search feedback (implicit domain preference learning) ───────────────────

_FEEDBACK_FILE = os.path.join(os.path.expanduser("~"), ".hound", "search_feedback.json")
_feedback_cache: Optional[frozenset] = None
_feedback_mtime: float = 0.0


def _feedback_domains() -> frozenset:
    """Load domains the agent found useful (from fetch_content successes).
    Cached in memory; re-reads file only if modified."""
    global _feedback_cache, _feedback_mtime
    try:
        import os as _os
        if not _os.path.exists(_FEEDBACK_FILE):
            return frozenset()
        mt = _os.path.getmtime(_FEEDBACK_FILE)
        if _feedback_cache is not None and mt == _feedback_mtime:
            return _feedback_cache
        import json as _json
        with open(_FEEDBACK_FILE, "r") as f:
            data = _json.load(f)
        _feedback_cache = frozenset(data.get("domains", []))
        _feedback_mtime = mt
        return _feedback_cache
    except Exception:
        return frozenset()


def record_search_feedback(url: str) -> None:
    """Record a domain as useful (called when fetch_content successfully fetches a page).
    Best-effort, never raises. Atomic write."""
    try:
        domain = _get_domain(url)
        if not domain:
            return
        import json as _json
        import tempfile
        os.makedirs(os.path.dirname(_FEEDBACK_FILE), exist_ok=True)
        # Load existing
        domains = set()
        if os.path.exists(_FEEDBACK_FILE):
            with open(_FEEDBACK_FILE, "r") as f:
                domains = set(_json.load(f).get("domains", []))
        domains.add(domain)
        # Cap at 500 domains
        if len(domains) > 500:
            domains = set(list(domains)[-500:])
        # Atomic write
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_FEEDBACK_FILE), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                _json.dump({"domains": sorted(domains)}, f)
            os.replace(tmp, _FEEDBACK_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    except Exception:
        pass


def _answer_signal_score(query: str, snippet: str, is_technical: bool) -> float:
    """Detect if snippet CONTAINS answer data vs just discusses topic. Up to +0.30."""
    if not snippet:
        return 0.0
    boost = 0.0
    q_lower = query.lower()
    s_lower = snippet.lower()
    if is_technical or any(w in q_lower for w in ("dimension", "size", "parameters", "count", "number", "how many")):
        if _DIGIT_RE.search(snippet):
            boost += 0.15
    if "table" in q_lower or "comparison" in q_lower:
        if any(w in s_lower for w in _TABLE_MARKERS):
            boost += 0.15
    if any(w in q_lower for w in ("vs", "compare", "comparison", "difference", "better")):
        if any(w in s_lower for w in _COMPARISON_MARKERS):
            boost += 0.10
    if any(w in q_lower for w in ("code", "api", "function", "example")):
        if any(w in s_lower for w in _CODE_MARKERS):
            boost += 0.10
    return min(boost, 0.30)


def _title_relevance(query: str, title: str) -> float:
    """Query terms in title: up to +0.10."""
    if not title:
        return 0.0
    q_terms = _query_terms(query)
    if not q_terms:
        return 0.0
    title_lower = title.lower()
    hits = sum(1 for t in q_terms if t in title_lower)
    return min(hits / len(q_terms), 1.0) * 0.10 if hits else 0.0


def _url_relevance(query: str, url: str) -> float:
    """Query terms in URL path: up to +0.08."""
    if not url:
        return 0.0
    q_terms = _query_terms(query)
    if not q_terms:
        return 0.0
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        return 0.0
    if not path or path == "/":
        return 0.0
    hits = sum(1 for t in q_terms if t in path)
    return min(hits / len(q_terms), 1.0) * 0.08 if hits else 0.0


# ─── intent-aware multi-query fan-out (upstream v12.0.0) ────────────────
# Detect query intent and give the diversity engines (Yandex, Startpage, Google,
# Qwant) an expanded query variant while core engines keep the original. Same
# request count, zero added latency (all parallel), but higher recall because
# different query variants surface different pages. Cross-variant consensus: a URL
# surfaced by different queries from different engines is a STRONGER authority
# signal than one from a single query.

_INTENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Ambiguous standalone words removed ("code" → "area code", etc). False
    # negatives are cheap (no expansion); false positives are expensive (dilute
    # diversity engines with irrelevant expansion terms).
    ("comparison", re.compile(r"\b(?:vs\.?|versus|compare|comparison|difference\s+between|pros\s+and\s+cons|which\s+is\s+better)\b", re.I)),
    ("howto", re.compile(r"\b(?:how\s+to|how\s+do\s+i|guide|tutorial|step\s+by\s+step|walkthrough)\b", re.I)),
    ("research", re.compile(r"\b(?:arxiv|research|benchmark|literature|state\s+of\s+the\s+art|case\s+study|white\s+paper)\b", re.I)),
    ("code", re.compile(r"\b(?:implement|function|api|method|class|snippet|script|debug|error|exception|source\s+code|code\s+example)\b", re.I)),
    ("reference", re.compile(r"\b(?:what\s+is|definition|explain|meaning|overview|introduction|understand)\b", re.I)),
    ("news", re.compile(r"\b(?:latest|newest|recent|announcement|breaking|changelog|release\s+notes|new\s+release|just\s+released)\b", re.I)),
]

_INTENT_EXPANSIONS: dict[str, str] = {
    "research": " paper arxiv benchmark results",
    "factual": " specifications table data parameters",
    # Other intents do NOT get expansion (testing showed expanded terms returned
    # tutorial spam instead of primary sources).
    "comparison": "",
    "howto": "",
    "code": "",
    "reference": "",
    "news": "",
    "general": "",
}

# Data/spec signal words for factual intent (catch queries asking for concrete
# numbers/config, not just general tech discussion).
_FACTUAL_DATA_WORDS = frozenset({
    "dimension", "size", "parameters", "count", "value", "spec",
    "specification", "d_model", "architecture", "layer",
    "config", "hidden", "precision", "vocab", "vocabulary",
    "encoder", "decoder", "context", "window", "throughput",
    "latency", "memory", "token", "batch", "sequence", "flops",
    "compute", "gpu", "head", "heads", "embedding", "optimizer",
})


def _detect_intent(query: str) -> str:
    """Detect query intent for multi-query fan-out. Returns one of:
    comparison, howto, research, code, reference, news, factual, general.
    Rule-based pattern matching (no LLM). Priority: comparison > howto > research
    > code > reference > news > factual > general."""
    q_lower = query.lower()
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(q_lower):
            return intent
    if _is_technical_query(query) and any(w in q_lower for w in _FACTUAL_DATA_WORDS):
        return "factual"
    return "general"


def _expand_query(query: str, intent: str) -> str:
    """Generate an expanded query variant by appending intent-specific terms.
    Only appends terms NOT already in the query. Returns the original query
    unchanged if no expansion applies (general intent, all terms present, or
    query too long)."""
    expansion = _INTENT_EXPANSIONS.get(intent, "")
    if not expansion:
        return query
    # Don't expand very long queries — could exceed engine query-length limits.
    if len(query.split()) >= 15:
        return query
    if intent == "news":
        expansion = expansion.replace("{year}", str(datetime.now().year))
    q_lower = query.lower()
    new_terms = [t for t in expansion.split() if t.lower() not in q_lower]
    if not new_terms:
        return query
    return query + " " + " ".join(new_terms)


def _generate_query_map(query: str, intent: str, engines: list[str] | None) -> dict[str, str]:
    """Assign per-engine query variants for multi-query fan-out.

    Core engines (DDG, Brave, Mojeek, Yahoo) get the original query; diversity
    engines (Yandex, Startpage, Google, Qwant) get the expanded query. Returns {}
    if no expansion applies (all engines get the same query = backward-compatible).
    """
    expanded = _expand_query(query, intent)
    if expanded == query:
        return {}
    core = {"duckduckgo", "brave", "mojeek", "yahoo"}
    engs = engines or []
    query_map: dict[str, str] = {}
    for eng in engs:
        # Map hound engine name to its backend name before matching.
        query_map[eng] = query if eng in core else expanded
    return query_map


def _apply_quality_boost(ranked: list, scores: list[float], query: str
                         ) -> tuple[list, list[float]]:
    """Six-signal composite boost: consensus + domain + answer + title + URL.
    All additive, zero-latency. Re-sorts and renormalizes to 0..1."""
    if not ranked:
        return ranked, scores
    is_tech = _is_technical_query(query)
    boosted = []
    for r, s in zip(ranked, scores):
        c = max(1, getattr(r, "consensus", 1))
        total_boost = (
            0.2 * (c - 1) +                          # consensus
            _domain_boost(r.url, query, is_tech) +   # domain reputation
            _answer_signal_score(query, r.snippet, is_tech) +  # answer signal
            _title_relevance(query, r.title) +        # title relevance
            _url_relevance(query, r.url)              # URL relevance
        )
        boosted.append((r, s + total_boost))
    order = {id(r): i for i, (r, _) in enumerate(boosted)}
    boosted.sort(key=lambda rs: (-rs[1], -getattr(rs[0], "consensus", 1), rs[0].position, order[id(rs[0])]))
    mx = max((s for _, s in boosted), default=0.0)
    if mx > 1.0:
        boosted = [(r, round(s / mx, 4)) for r, s in boosted]
    else:
        boosted = [(r, round(s, 4)) for r, s in boosted]
    return [r for r, _ in boosted], [s for _, s in boosted]


# ─── main entry ───────────────────────────────────────────────────────────────

async def smart_search(
    server,
    query: str,
    max_results: int = 6,
    cache_ttl: int = SEARCH_CACHE_TTL,
    mode: str = "auto",
    engines: Optional[list[str]] = None,
    url: Optional[str] = None,
    site: Optional[str] = None,
    exclude_sites: Optional[list[str]] = None,
    location: Optional[str] = None,
    language: Optional[str] = None,
    region: Optional[str] = None,
    page: int = 0,
    freshness: Optional[str] = None,
) -> SearchResponseModel:
    """Local keyless web search (no API key, no account). The default pool
    (duckduckgo, brave, yahoo, yandex - four independent indexes, all HTTP,
    no browser; add 'wikipedia' or 'grokipedia')
    is scraped in parallel, merged, deduped, and ranked. A URL returned
    by several independent engines is a consensus hit (engines_consensus field) and
    gets a ranking boost - a free authority signal. Returns URLs + ranking (NOT
    page content) so the agent smart_fetches the ones it wants itself.

    mode: auto (neural rerank if [all]+model present, else consensus + engine-
    position order), neural (same, explicit - surfaces a note if unavailable),
    find_similar (pass url=; fetches the source page, derives a query, and reranks
    candidates against the source content - Exa find-similar, local).
    """
    t0 = time()

    try:
        query = validate_search_query(query)
        _validate_filters(site, exclude_sites, location, language, page)
        engines = _validate_engines(engines)
        freshness = _validate_freshness(freshness)
        mode = _validate_mode(mode)
    except Exception as e:
        return SearchResponseModel(
            query=query, results=[], total_results=0,
            duration_ms=0, error=str(e),
        )

    max_results = max(1, min(max_results, 50))

    # find_similar: the target is a URL, not a query. Derive it early so the cache
    # key is keyed on the source URL.
    find_sim_url = ""
    if mode == "find_similar":
        cand = (url or "").strip() or (query if query.startswith("http") else "")
        try:
            find_sim_url = validate_url(cand) if cand else ""
        except Exception:
            find_sim_url = ""
        if not find_sim_url:
            return SearchResponseModel(
                query=query, results=[], total_results=0,
                duration_ms=(time() - t0) * 1000,
                error="find_similar requires a url (pass url=, or the URL as query).",
                next_action="Pass url= with a page URL to find pages similar to it (or pass the URL as the query).")

    # Preserve an explicit region for cache identity before deriving the
    # location/language default used by search engines.
    cache_region = str(region).strip().lower() if region is not None else None
    # region derives from location/language if not given (e.g. "US" -> "us-en").
    if region is None:
        loc = (location or "US").lower()
        lang = (language or "en").lower()
        region = f"{loc}-{lang}" if len(loc) == 2 else "us-en"

    cache_query = find_sim_url or query
    cache_type = (f"search:v5:{max_results}:{site or ''}:{','.join(exclude_sites or [])}:"f"{location or ''}:{language or ''}:{page or 0}:{','.join(engines or [])}:"f"{freshness or ''}:{mode}:{cache_query}")
    # An explicit region is passed through to the engines, so keep it in the
    # cache identity. Omitted regions retain the existing cache key behavior.
    if cache_region is not None:
        cache_type = f"{cache_type}:region={cache_region}"
    if cache_ttl > 0:
        cached = await get_cached(cache_query, cache_type, None, ttl=cache_ttl, scope="search")
        if cached and cached.get("content"):
            try:
                data = json.loads(cached["content"][0])
                results_list = [SearchResult(**r) for r in data.get("results", [])]
                _eu = data.get("engines_used", [])
                _eb = data.get("engine_blocked", [])
                _rm = data.get("rerank_mode", "merge")
                _rq = data.get("related_queries", [])
                return SearchResponseModel(
                    query=cache_query, results=results_list,
                    total_results=len(results_list), cached=True,
                    engines_used=_eu,
                    engine_blocked=_eb,
                    rerank_mode=_rm,
                    related_queries=_rq,
                    duration_ms=(time() - t0) * 1000,
                    fetch_hint=compute_fetch_hint(results_list),
                    summary=_search_summary(cache_query, results_list, _eu, _rm),
                    next_action=_search_next_action(results_list, _eb, "", _eu),
                )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Corrupt search cache for '{cache_query[:50]}': {e}")

    # Live local search
    error = ""
    ranked: list[RawResult] = []
    reports: list[EngineReport] = []
    rerank_used = "merge"
    rerank_note = ""

    # Start the reranker load in parallel with the engine fetch so the cold ONNX
    # model load (~1-2s) overlaps the ~2s diversity quorum instead of stacking
    # AFTER it (the old path paid engine_fetch + model_load sequentially = ~6-7s
    # on the first search). Race-safe via ensure_reranker's lock — shares ONE
    # load with the startup prewarm; awaited below before the rerank step so the
    # result is warm by then (usually already done, loaded during the fetch).
    _rerank_task = asyncio.create_task(ensure_reranker()) if mode in ("neural", "auto", "find_similar") else None

    if mode == "find_similar":
        src_title, src_text = await fetch_source_for_similar(find_sim_url, timeout=6)
        if not src_text:
            return SearchResponseModel(
                query=find_sim_url, results=[], total_results=0,
                duration_ms=(time() - t0) * 1000,
                error="could not fetch the source URL for find_similar (blocked or offline).",
                next_action="Retry, or smart_fetch the source URL first to confirm it is reachable, then call smart_search with mode=find_similar.")
        derived_query = src_title or " ".join(src_text.split()[:8]) or query
        try:
            ranked, reports = await multi_search(
                derived_query, max_results, engines=engines, site=site,
                exclude_sites=exclude_sites, region=region, freshness=freshness,
                page=page, server=server,
            )
        except Exception as e:
            error = redact_api_key(str(e)[:200])
        # Rerank candidates against the SOURCE page content (Exa find-similar,
        # local: the cross-encoder scores (source_content, candidate)).
        if _rerank_task:
            try:
                await _rerank_task
            except Exception:
                pass
        rer = get_reranker()
        if rer is not None and ranked:
            docs = [f"{r.title} {r.snippet}" for r in ranked]
            try:
                scores = rer.score(src_text[:2000], docs)
                pairs = sorted(zip(ranked, scores), key=lambda rs: (-rs[1], rs[0].position))
                ranked_list = [r for r, _ in pairs]
                scores = [s for _, s in pairs]
                rerank_used = "find_similar"
            except Exception:
                ranked_list, scores, _, _ = _rank(derived_query, ranked, "auto")
                rerank_used = "find_similar"
        else:
            ranked_list, scores, _, _ = _rank(derived_query, ranked, "auto")
            rerank_used = "find_similar"
            if ranked and get_reranker() is None:
                rerank_note = ("find_similar used consensus + position order (neural unavailable). " +
                               (unavailable_reason() or "install hound-mcp[all]"))
        _efams = {_INDEX_FAMILY.get(r.name, r.name) for r in reports if r.ok}
        total_families = len(_efams) or 1
        ranked_list, scores = _apply_quality_boost(ranked_list, scores, query)
        ranked_list, scores = ranked_list[:max_results], scores[:max_results]
        results_list = _build_results(cache_query, ranked_list, scores, total_families)
        results_list = _quality_filter(results_list)
        sim_note = f"find_similar to {find_sim_url} (searched: {derived_query[:60]!r})"
        fetch_hint = compute_fetch_hint(results_list)
        fetch_hint = (fetch_hint + " | " + sim_note) if fetch_hint else sim_note
        if rerank_note:
            fetch_hint = (fetch_hint + " | " + rerank_note) if fetch_hint else rerank_note
        sim_related = _related_queries(derived_query, results_list)
    else:
        # Intent-aware multi-query fan-out (upstream v12.0.0): detect intent
        # and give diversity engines an expanded query variant while core engines
        # keep the original. Same request count, zero added latency, higher recall.
        _intent = _detect_intent(query)
        _fan_engines = list(engines) if engines is not None else list(DEFAULT_ENGINES)
        _qmap = _generate_query_map(query, _intent, _fan_engines)
        try:
            ranked, reports = await multi_search(
                query, max_results, engines=engines, site=site,
                exclude_sites=exclude_sites, region=region, freshness=freshness,
                page=page, server=server, query_map=_qmap if _qmap else None,
            )
        except Exception as e:
            error = redact_api_key(str(e)[:200])

        if not ranked and not error:
            blocked_any = bool([r for r in reports if r.blocked])
            all_blocked = blocked_any and not bool([r for r in reports if r.ok])
            # Auto query rewrite: when zero results, try a simplified query.
            # Always try when site filter is set (site may not exist);
            # otherwise only try when NOT all engines are blocked.
            should_rewrite = (not all_blocked) or (site is not None)
            if should_rewrite:
                rewritten = _rewrite_query(query)
                if rewritten and rewritten != query:
                    try:
                        ranked2, reports2 = await multi_search(
                            rewritten, max_results, engines=engines, site=None,
                            exclude_sites=exclude_sites, region=region,
                            freshness=freshness, page=page, server=server,
                        )
                        if ranked2:
                            ranked = ranked2
                            reports = reports2
                            # Label results as coming from a rewritten query
                            # so the agent knows these may be less precise.
                            query = rewritten  # update query for downstream (related_queries, summary)
                            error = f"NOTE: Original query returned 0 results. Showing results for rewritten query: '{rewritten}'"
                    except Exception:
                        pass
            if not ranked and not error:
                error = (
                    "No results from any engine. " +
                    ("Engines were rate-limited/CAPTCHA'd; retry in a moment, rephrase, or set HOUND_SEARCH_PROXY for sustained heavy use. "
                     if blocked_any else "Try rephrasing the query.")
                )

        if _rerank_task:
            try:
                await _rerank_task
            except Exception:
                pass
        ranked_list, scores, rerank_used, rerank_note = _rank(query, ranked[:max(2 * max_results, 12)], mode)
        _efams = {_INDEX_FAMILY.get(r.name, r.name) for r in reports if r.ok}
        total_families = len(_efams) or 1
        ranked_list, scores = _apply_quality_boost(ranked_list, scores, query)
        # Diversity: cap same-domain results at 2 in top positions
        if not site:
            ranked_list, scores = _diversify(ranked_list, scores, max_per_domain=2)
        ranked_list, scores = ranked_list[:max_results], scores[:max_results]
        results_list = _build_results(query, ranked_list, scores, total_families)
        results_list = _quality_filter(results_list)
        results_list = _filter_irrelevant_results(results_list, query)
        fetch_hint = compute_fetch_hint(results_list)
        if rerank_note:
            fetch_hint = (fetch_hint + " | " + rerank_note) if fetch_hint else rerank_note
        main_related = _related_queries(query, results_list)

    # engines_used = contributed; engine_blocked = did NOT contribute (blocked /
    # timed out / parsed no results / consent page). Surfacing non-contributing
    # engines means an opt-in engine like google that CAPTCHAs is visible to the
    # agent (in engine_blocked), not silently absent from both lists.
    engines_used = list(dict.fromkeys(r.name for r in reports if r.ok))
    engine_blocked = list(dict.fromkeys(r.name for r in reports if r.blocked))

    # Agent QoL: when some engines didn't contribute but results came back from
    # the rest, say so plainly so the agent knows the results are partial + a
    # retry may add recall (instead of looking like a failure).
    if engine_blocked and results_list:
        _blk_note = (f"Engines {', '.join(engine_blocked)} didn't contribute (rate-limited/timed out/no results); "
                     f"results are from the rest - retry shortly for more recall.")
        fetch_hint = (fetch_hint + " | " + _blk_note) if fetch_hint else _blk_note

    # Low-confidence warning: when only 1 index family contributed and others
    # were blocked, cross-engine consensus is unavailable — tell the agent.
    if total_families <= 1 and engine_blocked and results_list:
        _low_conf = ("LOW CONFIDENCE: only 1 index family contributed "
                     f"({len(engine_blocked)} engines blocked). Cross-engine consensus "
                     "is unavailable - verify results via smart_fetch before relying on them.")
        fetch_hint = (fetch_hint + " | " + _low_conf) if fetch_hint else _low_conf

    # Cache successful results (+ engine metadata + related queries for cache hits)
    if cache_ttl > 0 and results_list:
        _rq_cache = sim_related if mode == "find_similar" else main_related
        cache_data = json.dumps({
            "results": [r.model_dump() for r in results_list],
            "engines_used": engines_used,
            "engine_blocked": engine_blocked,
            "rerank_mode": rerank_used,
            "related_queries": _rq_cache,
        })
        await set_cached(cache_query, cache_type, [cache_data], 200, None, cache_ttl, scope="search")

    return SearchResponseModel(
        query=cache_query, results=results_list, total_results=len(results_list),
        engines_used=engines_used, engine_blocked=engine_blocked,
        rerank_mode=rerank_used,
        related_queries=(sim_related if mode == "find_similar" else main_related),
        duration_ms=(time() - t0) * 1000, error=error,
        fetch_hint=fetch_hint,
        summary=_search_summary(cache_query, results_list, engines_used, rerank_used),
        next_action=_search_next_action(results_list, engine_blocked, error, engines_used),
    )
