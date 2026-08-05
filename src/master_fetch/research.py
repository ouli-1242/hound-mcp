"""Rule-driven research tool for Hound MCP.

Combines search + fetch + focus extraction into a single research call.
No LLM required — pure rule-based pipeline:
1. Search the query (multi-engine)
2. Fetch top N high-relevance results with focus=query
3. Merge and deduplicate relevant paragraphs
4. Return a structured research report

Design: $0, no API key, no LLM, local execution, graceful degradation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("master_fetch.research")


class ResearchSource(BaseModel):
    url: str = Field(description="Source URL")
    title: str = Field(default="", description="Page title")
    relevant_content: str = Field(default="", description="Focus-extracted relevant paragraphs")
    content_ok: bool = Field(default=False, description="Whether content was successfully extracted")


class ResearchResponse(BaseModel):
    query: str = Field(description="Research query")
    sources: List[ResearchSource] = Field(default=[], description="Fetched sources with relevant content")
    merged_paragraphs: List[str] = Field(default=[], description="Deduplicated relevant paragraphs across all sources")
    total_sources: int = Field(default=0, description="Number of sources successfully fetched")
    total_paragraphs: int = Field(default=0, description="Total unique paragraphs collected")
    search_summary: str = Field(default="", description="One-line search status")
    error: str = Field(default="", description="Error message (empty = ok)")
    duration_ms: float = Field(default=0, description="Total duration in ms")


async def smart_research(
    server,
    query: str,
    max_sources: int = 3,
    max_paragraphs: int = 20,
    cache_ttl: int = 300,
) -> ResearchResponse:
    """Execute a rule-driven research pipeline.

    1. Search the query via smart_search
    2. Fetch top N high-relevance results with focus=query
    3. Merge and deduplicate paragraphs
    4. Return structured report

    Never raises — returns ResearchResponse with error field on failure.
    """
    from time import time as _time
    t0 = _time()

    # 1. Search
    try:
        search_result = await server.smart_search(
            query=query, max_results=max_sources + 3, cache_ttl=cache_ttl,
        )
    except Exception as e:
        return ResearchResponse(
            query=query, error=f"search failed: {str(e)[:150]}",
            duration_ms=(_time() - t0) * 1000,
        )

    if not search_result.results:
        return ResearchResponse(
            query=query,
            error=search_result.error or "no search results",
            search_summary=search_result.summary,
            duration_ms=(_time() - t0) * 1000,
        )

    # 2. Pick top N high-relevance URLs
    high = [r for r in search_result.results if r.fetch_relevance == "high"]
    candidates = (high or search_result.results)[:max_sources]

    # 3. Fetch each with focus=query
    sources: List[ResearchSource] = []
    all_paragraphs: List[str] = []
    seen_paragraphs: set = set()

    for sr in candidates:
        try:
            page_result = await server.smart_fetch(
                url=sr.url, focus=query, cache_ttl=cache_ttl,
                max_content_chars=10000, timeout=20000,
            )
            content = "\n".join(page_result.content) if page_result.content else ""
            sources.append(ResearchSource(
                url=sr.url,
                title=sr.title,
                relevant_content=content[:5000],
                content_ok=page_result.content_ok,
            ))

            # Collect paragraphs for merging
            if page_result.content_ok and content:
                for para in content.split("\n\n"):
                    para_clean = para.strip()
                    # Dedup by first 100 chars (catches near-duplicates)
                    key = para_clean[:100].lower()
                    if para_clean and len(para_clean) > 20 and key not in seen_paragraphs:
                        seen_paragraphs.add(key)
                        all_paragraphs.append(para_clean)
                        if len(all_paragraphs) >= max_paragraphs:
                            break
        except Exception:
            sources.append(ResearchSource(
                url=sr.url, title=sr.title,
                relevant_content="", content_ok=False,
            ))

    elapsed = (_time() - t0) * 1000

    return ResearchResponse(
        query=query,
        sources=sources,
        merged_paragraphs=all_paragraphs[:max_paragraphs],
        total_sources=sum(1 for s in sources if s.content_ok),
        total_paragraphs=len(all_paragraphs),
        search_summary=search_result.summary,
        duration_ms=elapsed,
    )
