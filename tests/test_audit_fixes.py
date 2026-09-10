"""Hound 审查修复回归测试。

覆盖：
- P0: feed_fetch 不再崩溃（structured_content 合法 dict）+ URL 校验
- P1: 重定向链逐跳 SSRF 校验（fetcher 层）
- P1: max_redirects 生效
- P2: search_proxy._kick_health_check 重置（不再 no-op）
- P2: 域名 DNS 解析内网复查（HOUND_SSRF_DNS_RECHECK=1 开启时）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hound_mcp.fetcher import HTTPSession
from hound_mcp.security import SecurityError, validate_url
from hound_mcp.server import MasterFetchServer


# ---------------------------------------------------------------------------
# P0: feed_fetch 崩溃修复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_fetch_returns_dict_structured(mock_logger=None):
    """feed_fetch 分支应返回 dict structured_content（list 会 -32603）。"""

    srv = MasterFetchServer(cache_ttl=0)
    args = {"urls": ["https://example.com/feed"], "max_items": 3}

    class _FakeResp:
        source_url = "https://example.com/feed"
        source_title = "t"
        error = ""
        items = []

    with patch("hound_mcp.feed.fetch_feeds", new=AsyncMock(return_value=[_FakeResp()])):
        with patch("socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            result = await srv.feed_fetch(urls=args["urls"], max_items=3)

    assert isinstance(result, list)
    assert isinstance(result[0], dict)
    assert "source_url" in result[0]
    assert "items" in result[0]


@pytest.mark.asyncio
async def test_feed_fetch_rejects_internal_url():
    """feed_fetch 传内网 URL 应报错（SSRF 防护）。"""
    srv = MasterFetchServer(cache_ttl=0)
    with pytest.raises(ValueError, match="校验失败"):
        await srv.feed_fetch(urls=["http://127.0.0.1:8080/x"], max_items=3)


# ---------------------------------------------------------------------------
# P1: 重定向逐跳 SSRF 校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_to_internal_rejected():
    """公网 URL 重定向到内网应被 SecurityError 拒绝。"""
    session = HTTPSession(stealthy_headers=False, retries=0)
    session._client = MagicMock()

    def fake_get(url, headers=None, timeout=None, follow_redirects=False):
        resp = MagicMock()
        resp.status_code = 302
        resp.headers = {"location": "http://127.0.0.1:8080/internal.png"}
        resp.content = b""
        resp.reason = "Found"
        resp.cookies = []
        resp.url = url
        return resp

    session._client.get = MagicMock(side_effect=fake_get)
    with pytest.raises(SecurityError, match="internal/private IP"):
        await session.get("https://example.com/x.png", follow_redirects=True)


@pytest.mark.asyncio
async def test_max_redirects_bounded():
    """重定向次数受 max_redirects 限制（不会无限跟随）。"""
    session = HTTPSession(stealthy_headers=False, retries=0)
    session._client = MagicMock()
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None, follow_redirects=False):
        calls["n"] += 1
        resp = MagicMock()
        resp.status_code = 302
        resp.headers = {"location": f"http://127.0.0.1:1/hop{calls['n']}"}
        resp.content = b""
        resp.reason = "Found"
        resp.cookies = []
        resp.url = url
        return resp

    session._client.get = MagicMock(side_effect=fake_get)
    # allow_internal=True 让重定向目标通过校验；max_redirects=2 应限制为
    # 初始 + 2 跳 = 3 次请求，第 4 次不再跟随（返回 302 而非继续）
    r = await session.get("http://127.0.0.1:9/x", follow_redirects=True,
                          max_redirects=2, allow_internal=True)
    assert calls["n"] == 3, f"max_redirects=2 应产生 3 次请求，实际 {calls['n']}"
    assert r.status == 302, "超过 max_redirects 后应返回 3xx 而非继续跟随"


# ---------------------------------------------------------------------------
# P2: search_proxy health check 重置
# ---------------------------------------------------------------------------


def test_health_check_done_resets_task():
    """health_check 完成后 _health_task 应重置为 None，可再触发。"""
    import asyncio
    import hound_mcp.search_proxy as sp

    async def scenario():
        pool = MagicMock()
        pool.health_check = AsyncMock(return_value={"p": True})
        with patch.object(sp, "get_proxy_pool", return_value=pool):
            sp._health_task = None
            sp._kick_health_check()
            assert sp._health_task is not None, "应已创建任务"
            # 第二次调用应被跳过（任务仍在运行）
            sp._kick_health_check()
            await asyncio.gather(sp._health_task)
            await asyncio.sleep(0.01)
            assert sp._health_task is None, "任务完成后应重置为 None"
            # 重置后可再次触发
            sp._kick_health_check()
            assert sp._health_task is not None, "重置后应能再触发"
            await asyncio.gather(sp._health_task)
            sp._health_task = None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# P2: DNS 复查（开启时生效）
# ---------------------------------------------------------------------------


def test_dns_recheck_rejects_internal_resolution(monkeypatch):
    """HOUND_SSRF_DNS_RECHECK=1 时，域名解析到内网应拒绝。"""
    monkeypatch.setenv("HOUND_SSRF_DNS_RECHECK", "1")
    sp = __import__("hound_mcp.security", fromlist=["_dns_recheck_enabled"])
    assert sp._dns_recheck_enabled() is True
    with patch("socket.getaddrinfo",
               return_value=[(2, 1, 6, "", ("192.168.1.10", 0))]):
        with pytest.raises(SecurityError, match="resolves to internal"):
            validate_url("https://mycorp.internal/x")


def test_dns_recheck_off_by_default(monkeypatch):
    """默认关闭 DNS 复查（不误伤 DNS 污染环境）。"""
    sp = __import__("hound_mcp.security", fromlist=["_dns_recheck_enabled"])
    assert sp._dns_recheck_enabled() is False


def test_dns_recheck_accepts_public_resolution(monkeypatch):
    """开启时解析到公网 IP 应放行。"""
    monkeypatch.setenv("HOUND_SSRF_DNS_RECHECK", "1")
    with patch("socket.getaddrinfo",
               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        assert validate_url("https://example.com/x") == "https://example.com/x"

# ---------------------------------------------------------------------------
# P1: sitemap SSRF（深挖发现）
# ---------------------------------------------------------------------------


def test_robots_sitemap_internal_rejected(monkeypatch):
    """robots.txt 声明的内网 Sitemap 地址应被过滤。"""
    from hound_mcp.sitemap import _robots_sitemaps

    robots_body = (
        b"User-agent: *\n"
        b"Sitemap: http://169.254.169.254/latest/meta-data/\n"
        b"Sitemap: https://example.com/real-sitemap.xml\n"
    )

    def fake_get(url):
        return (200, robots_body)

    result, checked = _robots_sitemaps("https://example.com/", fake_get)
    assert checked is True
    assert result == ["https://example.com/real-sitemap.xml"], \
        f"内网 sitemap 应被过滤，实际: {result}"


def test_robots_sitemap_accepts_public(monkeypatch):
    """公网 Sitemap 地址应保留。"""
    from hound_mcp.sitemap import _robots_sitemaps

    robots_body = b"User-agent: *\nSitemap: https://example.com/sitemap.xml\n"

    def fake_get(url):
        return (200, robots_body)

    result, checked = _robots_sitemaps("https://example.com/", fake_get)
    assert result == ["https://example.com/sitemap.xml"]
