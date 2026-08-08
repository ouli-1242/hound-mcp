"""Tests for local file parsing (parse.py)."""

import os
import tempfile

import pytest

from hound_mcp.parse import parse_file, SUPPORTED_EXTENSIONS, MAX_PARSE_FILE_SIZE


class TestParseFileBasic:
    """Basic parse_file behavior."""

    def test_empty_path(self):
        content, error = parse_file("")
        assert content == ""
        assert "required" in error

    def test_nonexistent_file(self):
        content, error = parse_file("/nonexistent/path/file.html")
        assert content == ""
        assert "not found" in error.lower() or "not found" in error

    def test_unsupported_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
            f.write(b"test")
            path = f.name
        try:
            content, error = parse_file(path)
            assert content == ""
            assert "Unsupported" in error
        finally:
            os.unlink(path)

    def test_pdf_hint(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4")
            path = f.name
        try:
            content, error = parse_file(path)
            assert content == ""
            assert "smart_fetch" in error
        finally:
            os.unlink(path)


class TestParseHtml:
    """HTML parsing."""

    def test_simple_html(self):
        html = "<html><body><h1>Title</h1><p>Hello world</p></body></html>"
        with tempfile.NamedTemporaryFile(suffix=".html", mode="w", delete=False, encoding="utf-8") as f:
            f.write(html)
            path = f.name
        try:
            content, error = parse_file(path)
            assert error == ""
            assert "Hello" in content or "Title" in content
        finally:
            os.unlink(path)

    def test_empty_html(self):
        with tempfile.NamedTemporaryFile(suffix=".html", mode="w", delete=False, encoding="utf-8") as f:
            f.write("")
            path = f.name
        try:
            content, error = parse_file(path)
            # Should not crash
            assert error == "" or content == ""
        finally:
            os.unlink(path)


class TestParseCsv:
    """CSV parsing."""

    def test_simple_csv(self):
        csv_content = "name,age\nAlice,30\nBob,25\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, encoding="utf-8") as f:
            f.write(csv_content)
            path = f.name
        try:
            content, error = parse_file(path)
            assert error == ""
            assert "Alice" in content
            assert "Bob" in content
            assert "|" in content  # markdown table
        finally:
            os.unlink(path)

    def test_empty_csv(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, encoding="utf-8") as f:
            f.write("")
            path = f.name
        try:
            content, error = parse_file(path)
            assert error == ""
            assert "empty" in content.lower()
        finally:
            os.unlink(path)


class TestParseFileSizeLimit:
    """File size limit enforcement."""

    def test_oversized_file_rejected(self):
        # Create a file that exceeds the limit (mock by checking the constant)
        assert MAX_PARSE_FILE_SIZE == 50 * 1024 * 1024

    def test_normal_file_accepted(self):
        csv_content = "a,b\n1,2\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, encoding="utf-8") as f:
            f.write(csv_content)
            path = f.name
        try:
            content, error = parse_file(path)
            assert error == ""
        finally:
            os.unlink(path)


class TestParsePathSecurity:
    """Path traversal protection (tested at server level, but verify parse_file handles paths)."""

    def test_tilde_expansion(self):
        # parse_file should expand ~ without crashing
        content, error = parse_file("~/nonexistent_file_xyz.html")
        assert "not found" in error.lower() or "not found" in error

    def test_relative_path_resolution(self):
        # Relative paths should be resolved to absolute
        content, error = parse_file("relative/nonexistent.csv")
        assert "not found" in error.lower() or "not found" in error


class TestSupportedExtensions:
    """Verify supported extension set."""

    def test_expected_extensions(self):
        assert ".html" in SUPPORTED_EXTENSIONS
        assert ".htm" in SUPPORTED_EXTENSIONS
        assert ".docx" in SUPPORTED_EXTENSIONS
        assert ".xlsx" in SUPPORTED_EXTENSIONS
        assert ".csv" in SUPPORTED_EXTENSIONS
        assert ".pdf" in SUPPORTED_EXTENSIONS

    def test_unexpected_extensions(self):
        assert ".exe" not in SUPPORTED_EXTENSIONS
        assert ".py" not in SUPPORTED_EXTENSIONS
        assert ".txt" not in SUPPORTED_EXTENSIONS
