"""Tests for structured data extraction (structured.py)."""


from hound_mcp.structured import extract_structured


SAMPLE_HTML = """
<html>
<head>
    <title>Test Product</title>
    <script type="application/ld+json">
    {"name": "Widget", "price": "$29.99", "description": "A fine widget"}
    </script>
</head>
<body>
    <h1 class="title">Widget Pro</h1>
    <span class="price">$29.99</span>
    <ul class="features">
        <li>Fast</li>
        <li>Reliable</li>
        <li>Compact</li>
    </ul>
    <p>Contact: support@example.com</p>
</body>
</html>
"""


class TestExtractBySelector:
    """CSS selector-based extraction."""

    def test_single_element(self):
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string", "selector": "h1.title"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        assert result["title"] == "Widget Pro"

    def test_array_elements(self):
        schema = {
            "type": "object",
            "properties": {
                "features": {"type": "array", "selector": ".features li"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        assert result["features"] == ["Fast", "Reliable", "Compact"]

    def test_count_type_returns_element_count(self):
        schema = {
            "type": "object",
            "properties": {
                "li_count": {"type": "count", "selector": ".features li"},
                "h1_count": {"type": "count", "selector": "h1.title"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        assert result["li_count"] == 3
        assert result["h1_count"] == 1

    def test_count_missing_selector_returns_none(self):
        schema = {
            "type": "object",
            "properties": {
                "ghost_count": {"type": "count", "selector": ".does-not-exist"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        assert result["ghost_count"] in ("", None)

    def test_missing_selector_returns_empty(self):
        schema = {
            "type": "object",
            "properties": {
                "nonexistent": {"type": "string", "selector": ".does-not-exist"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        assert result["nonexistent"] == ""

    def test_invalid_selector_graceful(self):
        schema = {
            "type": "object",
            "properties": {
                "bad": {"type": "string", "selector": ">>>invalid[[["},
            }
        }
        # Should not raise
        result = extract_structured(SAMPLE_HTML, schema)
        assert result["bad"] == ""


class TestExtractFromJsonLd:
    """JSON-LD extraction."""

    def test_jsonld_field(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        assert result["name"] == "Widget"

    def test_jsonld_price(self):
        schema = {
            "type": "object",
            "properties": {
                "price": {"type": "string"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        # Could come from JSON-LD or selector
        assert "$29.99" in result["price"]


class TestExtractFromMetadata:
    """Metadata matching."""

    def test_metadata_match(self):
        metadata = {"og:title": "OG Title", "author": "John"}
        schema = {
            "type": "object",
            "properties": {
                "author": {"type": "string"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema, metadata=metadata)
        assert result["author"] == "John"


class TestExtractByPattern:
    """Regex pattern fallback."""

    def test_email_pattern(self):
        schema = {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        assert "support@example.com" in result["email"]

    def test_price_pattern(self):
        html = "<html><body><p>Only $19.99 today!</p></body></html>"
        schema = {
            "type": "object",
            "properties": {
                "price": {"type": "string"},
            }
        }
        result = extract_structured(html, schema)
        assert "$19.99" in result["price"]


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_empty_schema(self):
        result = extract_structured(SAMPLE_HTML, {})
        assert result == {}

    def test_none_schema(self):
        result = extract_structured(SAMPLE_HTML, None)
        assert result == {}

    def test_empty_html(self):
        schema = {"properties": {"title": {"type": "string", "selector": "h1"}}}
        result = extract_structured("", schema)
        assert result["title"] == ""

    def test_no_properties(self):
        result = extract_structured(SAMPLE_HTML, {"type": "object"})
        assert result == {}

    def test_field_spec_not_dict(self):
        schema = {"properties": {"title": "string"}}
        result = extract_structured(SAMPLE_HTML, schema)
        # Should treat as {"type": "string"}
        assert "title" in result

    def test_multiple_fields(self):
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string", "selector": "h1"},
                "features": {"type": "array", "selector": ".features li"},
                "price": {"type": "string", "selector": ".price"},
            }
        }
        result = extract_structured(SAMPLE_HTML, schema)
        assert result["title"] == "Widget Pro"
        assert len(result["features"]) == 3
        assert "$29.99" in result["price"]
