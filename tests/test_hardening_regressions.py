"""回归测试：日志凭据脱敏 + 代理健康探测的未 await 协程。

两个缺陷都来自「看起来无害、实际有副作用」的写法：

1. ``fetcher.py`` 在重试告警里直接写入原始异常文本。primp/httpx 的异常会
   带上完整代理 URL（形如 ``http://user:pass@host``），于是代理凭据落进
   日志。项目里已有 ``security.redact_api_key``，那里此前漏用。
2. ``search_proxy._kick_health_check`` 把 ``asyncio.create_task(pool.health_check())``
   写在一行：协程对象先被求值，若 ``create_task`` 因「无运行中的事件循环」
   抛 RuntimeError，那个协程已被创建却从未 await，触发
   ``coroutine 'ProxyPool.health_check' was never awaited``。
"""

from __future__ import annotations

import gc
import logging
import warnings

import pytest

from hound_mcp.fetcher import HTTPSession


class _CredentialLeakingClient:
    """模拟 primp：异常文本里带出完整的带凭据代理 URL。"""

    def get(self, *args, **kwargs):
        raise RuntimeError(
            "connection failed via http://alice:s3cret@proxy.test:8080 "
            "(proxy rejected)"
        )


@pytest.mark.asyncio
async def test_retry_warning_redacts_proxy_credentials(caplog):
    """重试告警不得把 user:pass@ 形式的代理凭据写进日志。"""
    session = HTTPSession(stealthy_headers=False, retries=1, retry_delay=0)
    session._client = _CredentialLeakingClient()

    with caplog.at_level(logging.WARNING, logger="hound_mcp.fetcher"):
        with pytest.raises(RuntimeError):
            await session.get("https://example.com/", retries=1)

    assert "s3cret" not in caplog.text, "代理密码泄漏到了日志"
    assert "alice:" not in caplog.text, "代理用户名密码片段泄漏到了日志"
    assert "CREDENTIALS_REDACTED" in caplog.text, "未走 redact_api_key 脱敏"


@pytest.mark.asyncio
async def test_retry_warning_still_mentions_the_url_and_attempt(caplog):
    """脱敏不能把告警本身抹掉——排障信息要保留。"""
    session = HTTPSession(stealthy_headers=False, retries=1, retry_delay=0)
    session._client = _CredentialLeakingClient()

    with caplog.at_level(logging.WARNING, logger="hound_mcp.fetcher"):
        with pytest.raises(RuntimeError):
            await session.get("https://example.com/page", retries=1)

    assert "https://example.com/page" in caplog.text
    assert "attempt 1" in caplog.text


class _RecordingPool:
    def __init__(self) -> None:
        self.health_check_calls = 0

    async def health_check(self):
        self.health_check_calls += 1


class TestKickHealthCheckWithoutEventLoop:
    """``_kick_health_check`` 在无事件循环时必须是「什么都没发生」。"""

    def test_pool_is_not_touched_without_event_loop(self, monkeypatch):
        """必须先判断事件循环再创建协程，否则会留下未 await 的协程对象。"""
        from hound_mcp import search_proxy

        touched: list[int] = []
        monkeypatch.setattr(search_proxy, "get_proxy_pool", lambda: touched.append(1))
        monkeypatch.setattr(search_proxy, "_health_task", None)

        search_proxy._kick_health_check()

        assert touched == [], "无事件循环时应立即返回，不应创建协程/触碰代理池"
        assert search_proxy._health_task is None

    def test_no_unawaited_coroutine_warning(self, monkeypatch):
        """直接复现历史症状：不应产生 RuntimeWarning。"""
        from hound_mcp import search_proxy

        pool = _RecordingPool()
        monkeypatch.setattr(search_proxy, "get_proxy_pool", lambda: pool)
        monkeypatch.setattr(search_proxy, "_health_task", None)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            search_proxy._kick_health_check()
            gc.collect()

        leaked = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert not leaked, f"产生了未 await 的协程警告: {[str(w.message) for w in leaked]}"
        assert pool.health_check_calls == 0
