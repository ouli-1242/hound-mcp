"""Hound — Web research for AI agents.

$0 forever. Fetch any page with anti-bot bypass plus web search.
"""

__version__ = "13.14"

# Lazy imports — server pulls in heavy deps (patchright, playwright, etc.)
# Other modules (cache, security) are lightweight and can be imported directly
# for testing without the full dependency chain.


def __getattr__(name: str):
    """Lazy attribute access for server-level exports."""
    _lazy_exports = {
        "MasterFetchServer",
        "ResponseModel",
        "BulkResponseModel",
        "SessionInfo",
        "SessionCreatedModel",
        "SessionClosedModel",
        "CacheInfoModel",
        "main",
    }
    if name in _lazy_exports:
        # F401：这些名字通过下方 locals()[name] 返回，是刻意的惰性再导出，
        # ruff 看不到用法，故显式豁免。
        from hound_mcp.server import (  # noqa: F401
            MasterFetchServer,
            ResponseModel,
            BulkResponseModel,
            SessionInfo,
            SessionCreatedModel,
            SessionClosedModel,
            CacheInfoModel,
            main,
        )
        return locals()[name]
    raise AttributeError(f"module 'hound_mcp' has no attribute '{name}'")


__all__ = [
    "MasterFetchServer",
    "ResponseModel",
    "BulkResponseModel",
    "SessionInfo",
    "SessionCreatedModel",
    "SessionClosedModel",
    "CacheInfoModel",
    "main",
]