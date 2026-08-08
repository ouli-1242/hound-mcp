"""Tests for RSS/Atom feed parsing (feed.py)."""

import pytest

from hound_mcp.feed import (
    _parse_feed_xml, _parse_date, fetch_feed, fetch_feeds,
    FeedItem, FeedResult,
)

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Channel</title>
    <item>
      <title>Old Post</title>
      <link>https://example.com/old</link>
      <pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate>
      <description>First paragraph of old post.</description>
    </item>
    <item>
      <title>New Post</title>
      <link>https://example.com/new</link>
      <pubDate>Tue, 02 Jan 2024 10:00:00 +0000</pubDate>
      <description>Fresh content here.</description>
    </item>
  </channel>
</rss>
"""

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <title>Entry One</title>
    <link href="https://example.com/one" />
    <published>2024-05-01T08:00:00Z</published>
    <summary>Summary text.</summary>
  </entry>
</feed>
"""


class TestParseDate:
    def test_rfc822(self):
        assert _parse_date("Tue, 02 Jan 2024 10:00:00 +0000").startswith("2024-01-02T10:00:00")

    def test_iso_with_z(self):
        assert _parse_date("2024-05-01T08:00:00Z").startswith("2024-05-01T08:00:00")

    def test_empty(self):
        assert _parse_date("") == ""

    def test_garbage(self):
        assert _parse_date("not a date") == ""


class TestParseRss:
    def test_parses_items_and_title(self):
        result = _parse_feed_xml(RSS_XML, "https://example.com/feed.xml")
        assert result.source_title == "Test Channel"
        assert len(result.items) == 2

    def test_newest_first(self):
        result = _parse_feed_xml(RSS_XML, "https://example.com/feed.xml")
        assert result.items[0].title == "New Post"
        assert result.items[1].title == "Old Post"

    def test_item_fields(self):
        result = _parse_feed_xml(RSS_XML, "https://example.com/feed.xml")
        item = result.items[0]
        assert item.url == "https://example.com/new"
        assert "Fresh content" in item.summary
        assert item.published.startswith("2024-01-02")


class TestParseAtom:
    def test_parses_atom(self):
        result = _parse_feed_xml(ATOM_XML, "https://example.com/atom.xml")
        assert result.source_title == "Atom Feed"
        assert len(result.items) == 1
        assert result.items[0].title == "Entry One"
        assert result.items[0].url == "https://example.com/one"
        assert result.items[0].published.startswith("2024-05-01")


class TestFetchFeed:
    @pytest.mark.asyncio
    async def test_fetch_error_isolated(self):
        result = await fetch_feed("http://127.0.0.1:1/nonexistent", timeout=2)
        assert isinstance(result, FeedResult)
        assert result.items == []
        assert result.error  # fetch failed, error surfaced

    @pytest.mark.asyncio
    async def test_fetch_feeds_batch(self, monkeypatch):
        async def fake_fetch(url, timeout=20, max_items=20):
            return FeedResult(source_url=url, source_title=url, items=[FeedItem(title="x")])
        monkeypatch.setattr("hound_mcp.feed.fetch_feed", fake_fetch)
        results = await fetch_feeds(["https://a.com/feed", "https://b.com/feed"])
        assert len(results) == 2
        assert all(r.source_title for r in results)