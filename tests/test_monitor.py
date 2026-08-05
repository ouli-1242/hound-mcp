"""Tests for page monitoring (monitor.py)."""

import pytest

from master_fetch.monitor import MonitorResponse, _compute_diff_summary


class TestMonitorResponse:
    """MonitorResponse model validation."""

    def test_default_values(self):
        resp = MonitorResponse(url="https://example.com", status="new")
        assert resp.url == "https://example.com"
        assert resp.status == "new"
        assert resp.last_checked == ""
        assert resp.previous_check == ""
        assert resp.diff_summary == ""
        assert resp.content_hash == ""
        assert resp.content_snippet == ""
        assert resp.error == ""
        assert resp.total_checks == 0

    def test_all_fields(self):
        resp = MonitorResponse(
            url="https://example.com",
            status="changed",
            last_checked="2024-01-01T00:00:00Z",
            previous_check="2023-12-31T00:00:00Z",
            diff_summary="2 paragraph(s) added",
            content_hash="abc123",
            content_snippet="Hello world",
            total_checks=5,
        )
        assert resp.status == "changed"
        assert resp.total_checks == 5


class TestComputeDiffSummary:
    """Diff summary computation."""

    def test_identical_content(self):
        old = "para1\n\npara2\n\npara3"
        new = "para1\n\npara2\n\npara3"
        summary = _compute_diff_summary(old, new)
        assert "3 unchanged" in summary

    def test_added_paragraphs(self):
        old = "para1\n\npara2"
        new = "para1\n\npara2\n\npara3\n\npara4"
        summary = _compute_diff_summary(old, new)
        assert "2 paragraph(s) added" in summary

    def test_removed_paragraphs(self):
        old = "para1\n\npara2\n\npara3"
        new = "para1"
        summary = _compute_diff_summary(old, new)
        assert "2 paragraph(s) removed" in summary

    def test_mixed_changes(self):
        old = "alpha\n\nbeta\n\ngamma"
        new = "alpha\n\ndelta\n\nepsilon"
        summary = _compute_diff_summary(old, new)
        assert "added" in summary
        assert "removed" in summary

    def test_empty_old(self):
        summary = _compute_diff_summary("", "new content\n\nmore")
        assert "added" in summary

    def test_empty_new(self):
        summary = _compute_diff_summary("old content\n\nmore", "")
        assert "removed" in summary

    def test_both_empty(self):
        summary = _compute_diff_summary("", "")
        # No paragraphs at all
        assert "format changed" in summary.lower() or summary != ""

    def test_size_increase_reported(self):
        old = "short"
        new = "short\n\n" + "x" * 100
        summary = _compute_diff_summary(old, new)
        assert "+100" in summary or "+" in summary

    def test_size_decrease_reported(self):
        old = "long content " + "x" * 100
        new = "short"
        summary = _compute_diff_summary(old, new)
        assert "-" in summary

    def test_english_output(self):
        """Verify output is in English (not Chinese)."""
        old = "para1\n\npara2"
        new = "para1\n\npara3"
        summary = _compute_diff_summary(old, new)
        # Should NOT contain Chinese characters
        assert "段" not in summary
        assert "字符" not in summary
        # Should contain English
        assert "paragraph" in summary or "unchanged" in summary


class TestMonitorListIntegration:
    """Test that monitor_list imports correctly (no ImportError)."""

    def test_monitor_list_importable(self):
        """Verify monitor_list can be imported without error."""
        from master_fetch.monitor import monitor_list
        assert callable(monitor_list)

    def test_monitor_check_importable(self):
        """Verify monitor_check can be imported without error."""
        from master_fetch.monitor import monitor_check
        assert callable(monitor_check)
