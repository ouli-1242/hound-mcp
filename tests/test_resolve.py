"""Tests for resolve_url (server.py)."""

import pytest

from hound_mcp.server import MasterFetchServer


class FakeResp:
    def __init__(self, status=200, final_url="https://final.example/x", headers=None):
        self.status = status
        self.url = final_url
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}


class FakeSession:
    def __init__(self, proxy=None, stealthy_headers=True, retries=1, timeout=30):
        self._proxy = proxy

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, follow_redirects="safe"):
        if "blocked" in url:
            raise ConnectionError("tunnel failed")
        return FakeResp()


@pytest.mark.asyncio
async def test_resolve_url_returns_final_url(monkeypatch):
    server = MasterFetchServer()
    monkeypatch.setattr("hound_mcp.fetcher.HTTPSession", FakeSession)
    result = await server.resolve_url("https://t.co/abc123")
    assert result["final_url"] == "https://final.example/x"
    assert result["status"] == 200
    assert result["content_type"] == "text/html; charset=utf-8"
    assert result["error"] == ""
    assert result["original_url"] == "https://t.co/abc123"


@pytest.mark.asyncio
async def test_resolve_url_surfaces_error(monkeypatch):
    server = MasterFetchServer()
    monkeypatch.setattr("hound_mcp.fetcher.HTTPSession", FakeSession)
    result = await server.resolve_url("https://example.com/blocked")
    assert result["error"]
    assert result["final_url"] == ""


@pytest.mark.asyncio
async def test_resolve_url_rejects_ssrf(monkeypatch):
    server = MasterFetchServer()
    result = await server.resolve_url("http://169.254.169.254/latest/meta-data")
    assert result["error"]  # blocked by validate_url
    assert result["status"] == 0