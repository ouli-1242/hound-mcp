"""Pytest fixtures for Hound tests."""

import pytest
import tempfile
from pathlib import Path


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test artifacts."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def sample_urls():
    """Sample URLs for testing."""
    return {
        "valid_http": "https://example.com/page",
        "valid_https": "https://api.github.com/repos",
        "with_path": "https://example.com/path/to/page?q=1&b=2",
        "with_port": "https://example.com:8080/path",
        "internal_ipv4": "http://127.0.0.1/admin",
        "internal_ipv4_10": "http://10.0.0.1/api",
        "internal_ipv4_192": "http://192.168.1.1/",
        "internal_ipv4_172": "http://172.16.0.1/",
        "internal_ipv6": "http://[::1]:8080/path",
        "localhost": "http://localhost:3000/api",
        "metadata": "http://169.254.169.254/latest/meta-data/",
        "file_scheme": "file:///etc/passwd",
        "javascript_scheme": "javascript:alert(1)",
        "data_scheme": "data:text/html,<script>alert(1)</script>",
        "gopher_scheme": "gopher://evil.com/1",
        "no_scheme": "example.com/path",
        "malformed": "not a url at all",
        "empty": "",
        "oversized": "https://example.com/" + "a" * 9000,
    }


@pytest.fixture
def sample_selectors():
    """Sample CSS selectors for testing."""
    return {
        "valid": "div.content > p",
        "complex": "div.container article.main p.text",
        "oversized": "div > " * 3000,
        "with_script": "script[src='evil.js']",
        "with_style": "style > *",
        "with_js_protocol": "a[href='javascript:alert(1)']",
        "valid_none": None,
        "empty": "",
    }


@pytest.fixture
def sample_headers():
    """Sample HTTP headers for testing."""
    return {
        "valid": {"X-Custom": "value", "Accept": "text/html"},
        "with_newline_name": {"X-Evil\nHeader": "value"},
        "with_newline_value": {"X-Header": "value\r\nInjected: true"},
        "forbidden": {"Host": "evil.com"},
        "empty": {},
        "none": None,
        "too_many": {f"X-Header-{i}": "value" for i in range(100)},
    }
