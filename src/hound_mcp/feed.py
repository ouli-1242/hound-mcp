"""RSS/Atom feed fetching for Hound.

Fetches and parses RSS 2.0 / Atom feeds so an agent can track what a source
*has published* (fresh items), as opposed to fetching a page and reading it.

No caching: feeds are inherently fresh content; a cached feed defeats the
purpose. Uses the existing fetcher.HTTPSession (TLS impersonation) so feeds
behind basic bot protection still parse.

Parser is lenient (lxml, tolerant of malformed XML): a feed that partially
fails still returns whatever items parsed, with the error surfaced per-source.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("hound_mcp.feed")

_MAX_FEED_BYTES = 2 * 1024 * 1024  # 2MB cap — feeds are small text; larger is junk


class FeedItem(BaseModel):
    """A single entry from an RSS/Atom feed."""
    title: str = Field(default="", description="Entry title")
    url: str = Field(default="", description="Entry link")
    published: str = Field(default="", description="ISO-8601 publish date (empty if unknown)")
    summary: str = Field(default="", description="Entry summary/description (trimmed)")


class FeedResult(BaseModel):
    """Parsed feed for one source URL."""
    source_url: str = Field(description="The feed URL requested")
    source_title: str = Field(default="", description="Feed/channel title")
    items: list[FeedItem] = Field(default=[], description="Entries, newest first")
    error: str = Field(default="", description="Fetch/parse error (empty = ok)")


def _parse_date(value: str) -> str:
    """Best-effort RFC822 / ISO-8601 parse to ISO-8601. Returns '' if unparseable."""
    if not value:
        return ""
    v = value.strip()
    # RFC 822 (RSS): "Tue, 05 Aug 2026 12:00:00 +0000"
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(v)
        if dt:
            return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    # ISO-8601 (Atom): "2026-08-05T12:00:00Z"
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def _local_name(tag: str) -> str:
    """Strip XML namespace: '{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(elem: Any, *tags: str) -> str:
    """First non-empty text of the given child tags (namespace-agnostic)."""
    for child in elem:
        if _local_name(child.tag) in tags and child.text and child.text.strip():
            return child.text.strip()
    return ""


def _parse_feed_xml(raw: str, source_url: str) -> FeedResult:
    """Parse RSS 2.0 or Atom XML into a FeedResult (newest-first)."""
    from lxml import etree
    root = etree.fromstring(raw.encode("utf-8", errors="replace"))
    root_name = _local_name(root.tag)

    source_title = _text(root, "title", "subtitle")
    items: list[FeedItem] = []

    if root_name == "feed":  # Atom
        for entry in root.iterfind("*"):
            if _local_name(entry.tag) != "entry":
                continue
            link = ""
            for child in entry:
                if _local_name(child.tag) == "link":
                    link = child.get("href", "") or link
            items.append(FeedItem(
                title=_text(entry, "title"),
                url=link,
                published=_parse_date(_text(entry, "published", "updated", "date")),
                summary=_text(entry, "summary", "content")[:500],
            ))
    else:  # RSS 2.0 (channel/item), also handles rdf:RDF
        channel = next((c for c in root if _local_name(c.tag) == "channel"), None)
        if channel is not None:
            source_title = _text(channel, "title")
        for item in root.iter():
            if _local_name(item.tag) != "item":
                continue
            items.append(FeedItem(
                title=_text(item, "title"),
                url=_text(item, "link"),
                published=_parse_date(_text(item, "pubDate", "published", "date")),
                summary=_text(item, "description")[:500],
            ))

    # Newest first: items without a date sort last (stable, keeps feed order).
    def _sort_key(it: FeedItem) -> tuple[int, str]:
        return (1, "") if not it.published else (0, it.published)

    items.sort(key=_sort_key, reverse=True)
    return FeedResult(source_url=source_url, source_title=source_title, items=items)


async def fetch_feed(url: str, timeout: int = 20, max_items: int = 20) -> FeedResult:
    """Fetch and parse a single RSS/Atom feed. Never raises.

    Returns a FeedResult with items parsed so far; on fetch failure the error
    field carries the reason and items is empty.
    """
    try:
        from hound_mcp.fetcher import HTTPSession
        async with HTTPSession(stealthy_headers=False, retries=1, timeout=timeout) as session:
            resp = await session.get(url, follow_redirects="safe")
        body = getattr(resp, "body", b"") or b""
        if len(body) > _MAX_FEED_BYTES:
            return FeedResult(source_url=url, error=f"feed too large ({len(body)} bytes)")
        if resp.status >= 400:
            return FeedResult(source_url=url, error=f"HTTP {resp.status}")
        raw = body.decode(getattr(resp, "encoding", None) or "utf-8", errors="replace")
        result = _parse_feed_xml(raw, url)
        if max_items > 0:
            result.items = result.items[:max_items]
        return result
    except Exception as e:
        logger.debug("feed fetch failed for %s: %s", url, e)
        return FeedResult(source_url=url, error=f"{type(e).__name__}: {str(e)[:200]}")


async def fetch_feeds(
    urls: list[str],
    timeout: int = 20,
    max_items: int = 20,
    concurrency: int = 5,
) -> list[FeedResult]:
    """Fetch multiple feeds with bounded concurrency. Per-source errors are
    isolated (one bad feed never fails the batch)."""
    import asyncio
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(url: str) -> FeedResult:
        async with sem:
            return await fetch_feed(url, timeout=timeout, max_items=max_items)

    return list(await asyncio.gather(*(_one(u) for u in urls)))