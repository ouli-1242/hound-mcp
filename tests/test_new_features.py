"""Tests for new features: fetch_content, schema auto, batch structured extraction."""


from hound_mcp.structured import extract_structured


# ─── Schema auto mode ─────────────────────────────────────────────────────────

SAMPLE_HTML_WITH_TABLE = """
<html><body>
<table>
  <tr><th>Name</th><th>Price</th></tr>
  <tr><td>Widget</td><td>$10</td></tr>
  <tr><td>Gadget</td><td>$20</td></tr>
</table>
</body></html>
"""

SAMPLE_HTML_WITH_LIST = """
<html><body>
<ul>
  <li>First item</li>
  <li>Second item</li>
  <li>Third item</li>
</ul>
</body></html>
"""

SAMPLE_HTML_WITH_JSONLD = """
<html><head>
<script type="application/ld+json">
{"@type": "Product", "name": "Widget", "price": "$10"}
</script>
</head><body><p>Hello</p></body></html>
"""


class TestSchemaAuto:
    """Test schema={"type": "auto"} detection."""

    def test_auto_detects_table(self):
        result = extract_structured(SAMPLE_HTML_WITH_TABLE, {"type": "auto"})
        assert result.get("_mode") == "auto"
        assert "tables" in result
        assert len(result["tables"]) >= 1

    def test_auto_detects_list(self):
        result = extract_structured(SAMPLE_HTML_WITH_LIST, {"type": "auto"})
        assert "lists" in result
        # lists is a list of extracted list structures
        assert len(result["lists"]) >= 1
        # Each list contains items (structure depends on implementation)
        first_list = result["lists"][0]
        assert first_list  # non-empty

    def test_auto_detects_jsonld(self):
        result = extract_structured(SAMPLE_HTML_WITH_JSONLD, {"type": "auto"})
        assert "json_ld" in result
        assert result["json_ld"][0]["name"] == "Widget"

    def test_auto_mode_via_mode_key(self):
        result = extract_structured(SAMPLE_HTML_WITH_TABLE, {"mode": "auto"})
        assert result.get("_mode") == "auto"

    def test_auto_empty_html(self):
        result = extract_structured("", {"type": "auto"})
        assert result.get("_mode") == "auto"

    def test_auto_no_structure(self):
        result = extract_structured("<html><body><p>Just text</p></body></html>", {"type": "auto"})
        assert result.get("_mode") == "auto"


# ─── Batch structured extraction (urls + schema) ──────────────────────────────

class TestBatchStructured:
    """Test that schema parameter works in bulk mode (urls + schema)."""

    def test_schema_passed_to_bulk(self):
        """Verify _smart_fetch_bulk accepts schema parameter."""
        import inspect
        from hound_mcp.server import MasterFetchServer
        sig = inspect.signature(MasterFetchServer._smart_fetch_bulk)
        assert "schema" in sig.parameters

    def test_smart_fetch_accepts_schema_and_urls(self):
        """Verify smart_fetch signature has both urls and schema."""
        import inspect
        from hound_mcp.server import MasterFetchServer
        sig = inspect.signature(MasterFetchServer.smart_fetch)
        assert "urls" in sig.parameters
        assert "schema" in sig.parameters


# ─── fetch_content + fetch_schema in smart_search ─────────────────────────────

class TestFetchContentSchema:
    """Test that smart_search supports fetch_content and fetch_schema."""

    def test_smart_search_has_fetch_schema_param(self):
        import inspect
        from hound_mcp.server import MasterFetchServer
        sig = inspect.signature(MasterFetchServer.smart_search)
        assert "fetch_content" in sig.parameters
        assert "fetch_schema" in sig.parameters

    def test_search_response_has_fetched_pages(self):
        from hound_mcp.search import SearchResponseModel
        resp = SearchResponseModel(query="test", results=[])
        assert hasattr(resp, "fetched_pages")
        assert resp.fetched_pages == []


# ─── source_type detection ────────────────────────────────────────────────────

class TestSourceType:
    """Test _source_type domain classification."""

    def test_docs_domains(self):
        from hound_mcp.search import _source_type
        assert _source_type("https://docs.python.org/3/tutorial") == "docs"
        assert _source_type("https://huggingface.co/models") == "docs"
        assert _source_type("https://kubernetes.io/docs/") == "docs"

    def test_paper_domains(self):
        from hound_mcp.search import _source_type
        assert _source_type("https://arxiv.org/abs/2301.00001") == "paper"
        assert _source_type("https://nature.com/articles/xyz") == "paper"

    def test_repo_domains(self):
        from hound_mcp.search import _source_type
        assert _source_type("https://github.com/user/repo") == "repo"
        assert _source_type("https://pypi.org/project/hound-mcp/") == "repo"

    def test_forum_domains(self):
        from hound_mcp.search import _source_type
        assert _source_type("https://stackoverflow.com/questions/123") == "forum"
        assert _source_type("https://zhihu.com/question/456") == "forum"

    def test_news_domains(self):
        from hound_mcp.search import _source_type
        assert _source_type("https://bbc.com/news/article") == "news"
        assert _source_type("https://36kr.com/p/123") == "news"

    def test_blog_domains(self):
        from hound_mcp.search import _source_type
        assert _source_type("https://medium.com/@user/post") == "blog"
        assert _source_type("https://csdn.net/article/123") == "blog"

    def test_path_heuristic(self):
        from hound_mcp.search import _source_type
        assert _source_type("https://example.com/docs/api") == "docs"
        assert _source_type("https://example.com/blog/post-1") == "blog"

    def test_unknown_returns_other(self):
        from hound_mcp.search import _source_type
        assert _source_type("https://randomsite12345.com/page") == "other"


# ─── ddg alias ────────────────────────────────────────────────────────────────

class TestDdgAlias:
    """Test that 'ddg' is accepted as engine name."""

    def test_ddg_in_validate_engines(self):
        from hound_mcp.search import _validate_engines
        # Should not raise
        result = _validate_engines(["ddg"])
        assert result == ["ddg"]

    def test_ddg_maps_to_duckduckgo(self):
        from hound_mcp.search_metasearch import _HOUND_TO_BACKEND
        assert _HOUND_TO_BACKEND["ddg"] == "duckduckgo"
