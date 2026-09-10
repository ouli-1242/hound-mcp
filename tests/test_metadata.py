"""``hound_mcp.metadata`` 单元测试（纯函数：正则 + json，无网络）。

该模块此前无测试覆盖。它给每个 HTML 抓取结果补齐 title/description/
canonical/lang/published_time 等字段，agent 据此判断相关性与引用来源，
字段缺失或解析错的代价是 agent 拿到误导性元数据。
"""

from __future__ import annotations

from hound_mcp.metadata import extract_image_urls, extract_metadata


class TestExtractMetadata:
    def test_empty_html_returns_empty_dict(self):
        assert extract_metadata("", "https://example.com/") == {}

    def test_title_tag_is_used_as_fallback(self):
        html = "<html><head><title>  Hello   World </title></head></html>"
        meta = extract_metadata(html, "https://example.com/")

        assert meta["title"] == "Hello World"

    def test_opengraph_takes_priority_over_twitter_and_title(self):
        html = """<html><head>
            <title>title tag</title>
            <meta property="og:title" content="OG Title">
            <meta name="twitter:title" content="Twitter Title">
            <meta property="og:description" content="A description">
            <meta property="og:site_name" content="Example Site">
            <meta property="og:type" content="article">
            <meta property="og:image" content="https://cdn.example.com/a.png">
        </head></html>"""

        meta = extract_metadata(html, "https://example.com/")

        assert meta["title"] == "OG Title"
        assert meta["description"] == "A description"
        assert meta["site_name"] == "Example Site"
        assert meta["type"] == "article"
        assert meta["image"] == "https://cdn.example.com/a.png"

    def test_reversed_attribute_order_is_parsed(self):
        """content 写在 property 之前也要能解析。"""
        html = '<meta content="Rev Title" property="og:title">'

        assert extract_metadata(html, "https://example.com/")["title"] == "Rev Title"

    def test_meta_without_content_is_ignored(self):
        html = '<meta property="og:title" content="">'

        assert "title" not in extract_metadata(html, "https://example.com/")

    def test_canonical_is_made_absolute(self):
        html = '<link rel="canonical" href="/page">'

        meta = extract_metadata(html, "https://example.com/blog/post")

        assert meta["canonical"] == "https://example.com/page"

    def test_html_lang_is_extracted(self):
        assert (
            extract_metadata('<html lang="zh-CN">', "https://example.com/")["lang"]
            == "zh-CN"
        )

    def test_jsonld_fills_fields(self):
        html = """<script type="application/ld+json">
        {"@type": "Article", "headline": "LD Headline",
         "datePublished": "2026-01-02T03:04:05Z",
         "description": "LD desc", "author": {"name": "Ada"}}
        </script>"""

        meta = extract_metadata(html, "https://example.com/")

        assert meta["title"] == "LD Headline"
        assert meta["published_time"] == "2026-01-02T03:04:05Z"
        assert meta["author"] == "Ada"
        assert meta["description"] == "LD desc"

    def test_jsonld_author_list_uses_first_entry(self):
        html = """<script type="application/ld+json">
        {"author": [{"name": "First"}, {"name": "Second"}]}
        </script>"""

        assert (
            extract_metadata(html, "https://example.com/")["author"] == "First"
        )

    def test_malformed_jsonld_is_skipped(self):
        html = '<script type="application/ld+json">{ not json </script>'

        assert extract_metadata(html, "https://example.com/") == {}

    def test_meta_values_are_truncated(self):
        html = f'<meta property="og:description" content="{"x" * 900}">'

        assert len(extract_metadata(html, "https://example.com/")["description"]) == 500


class TestExtractImageUrls:
    def test_relative_src_is_made_absolute(self):
        html = '<img src="/img/a.png">'

        assert extract_image_urls(html, "https://example.com/blog/") == [
            "https://example.com/img/a.png"
        ]

    def test_data_uris_are_skipped(self):
        html = '<img src="data:image/png;base64,AAAA"><img src="/a.png">'

        assert extract_image_urls(html, "https://example.com/") == [
            "https://example.com/a.png"
        ]

    def test_duplicates_are_collapsed_order_preserved(self):
        html = '<img src="/b.png"><img src="/a.png"><img src="/b.png">'

        assert extract_image_urls(html, "https://example.com/") == [
            "https://example.com/b.png",
            "https://example.com/a.png",
        ]

    def test_max_n_caps_the_result(self):
        html = "".join(f'<img src="/{i}.png">' for i in range(10))

        assert len(extract_image_urls(html, "https://example.com/", max_n=3)) == 3

    def test_empty_html_returns_empty_list(self):
        assert extract_image_urls("", "https://example.com/") == []
