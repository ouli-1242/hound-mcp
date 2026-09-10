"""``hound_mcp.extractor`` 冒烟测试。

提取链路是 trafilatura（主）→ markdownify（备）→ 正则剥标签（兜底），
此前无测试覆盖。这里只锁「返回形状」与「不抛异常」这两个契约：
具体抽取质量由 trafilatura 自身保证，不在此重测。
"""

from __future__ import annotations

from hound_mcp.extractor import extract_content

_ARTICLE_HTML = """<!doctype html>
<html lang="en"><head><title>Test Article</title></head>
<body>
  <article>
    <h1>Understanding Tide Pools</h1>
    <p>Intertidal zones host remarkably resilient organisms. The
    purple sea urchin withstands hours of exposure each day.</p>
    <p>A second paragraph gives trafilatura enough body text to treat this
    as real article content rather than boilerplate navigation chrome.</p>
  </article>
</body></html>"""


class _FakePage:
    """Response 的最小替身：只提供提取链路会用到的属性。"""

    def __init__(self, html: str, url: str = "https://example.com/post") -> None:
        self.body = html.encode("utf-8")
        self.content = html
        self.html_content = html
        self.encoding = "utf-8"
        self.url = url
        self.headers: dict[str, str] = {"content-type": "text/html"}
        self.status = 200

    def css(self, selector: str) -> list:
        return []


class TestExtractContent:
    def test_returns_list_of_strings(self):
        result = extract_content(_FakePage(_ARTICLE_HTML), "text")

        assert isinstance(result, list)
        assert result
        assert all(isinstance(item, str) for item in result)

    def test_extracts_article_body_text(self):
        result = extract_content(_FakePage(_ARTICLE_HTML), "article")

        joined = "\n".join(result)
        assert "purple sea urchin" in joined

    def test_empty_body_does_not_raise(self):
        result = extract_content(_FakePage(""), "markdown")

        assert isinstance(result, list)

    def test_html_type_uses_fallback_path(self):
        """extraction_type='html' 不走 trafilatura，直接走兜底提取。"""
        result = extract_content(_FakePage("<p>hello</p>"), "html")

        assert isinstance(result, list)
        assert result

    def test_css_selector_is_accepted(self):
        result = extract_content(_FakePage(_ARTICLE_HTML), "text", css_selector="article")

        assert isinstance(result, list)
