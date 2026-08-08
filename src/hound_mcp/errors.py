"""Network error classification for agent-actionable diagnostics.

Maps raw network error strings (from primp, patchright/playwright, OS socket
layer) into categories with actionable hints so AI agents know whether to
retry, switch sources, or give up — instead of seeing opaque "fetch failed".

Pure utility: no dependencies beyond the standard library. Safe to import
anywhere (event loop, worker thread, module level).
"""

from __future__ import annotations

import re
from typing import Tuple

# ─── Pattern table ────────────────────────────────────────────────────────────
# Order matters: first match wins. Patterns are checked case-insensitively
# against the raw error string.

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Proxy errors (check FIRST: "proxy connection refused" should be proxy, not conn_refused)
    (re.compile(
        r"ERR_PROXY_CONNECTION_FAILED|proxy.*(?:refused|failed|error)|"
        r"tunnel connection failed|proxy_connect",
        re.IGNORECASE,
    ), "proxy_error"),

    # Connection refused (target actively rejecting TCP connections)
    (re.compile(
        r"ERR_CONNECTION_REFUSED|ConnectionRefusedError|os error 10061|"
        r"ECONNREFUSED|connection refused",
        re.IGNORECASE,
    ), "connection_refused"),

    # Connection reset (remote host forcibly closed an established connection)
    (re.compile(
        r"os error 10054|ERR_CONNECTION_RESET|ConnectionResetError|"
        r"ECONNRESET|远程主机强迫关闭|connection (?:was )?(?:forcibly )?(?:closed|reset)|"
        r"an existing connection was forcibly closed",
        re.IGNORECASE,
    ), "connection_reset"),

    # DNS resolution failure
    (re.compile(
        r"ERR_NAME_NOT_RESOLVED|NameResolutionError|getaddrinfo failed|"
        r"nodename nor servname|Name or service not known|"
        r"DNS (?:resolution )?(?:failed|error)|resolve.*failed",
        re.IGNORECASE,
    ), "dns_failure"),

    # Timeout
    (re.compile(
        r"TimeoutError|ERR_TIMED_OUT|ERR_CONNECTION_TIMED_OUT|"
        r"ERR_NAVIGATION_TIMEOUT|timed? ?out|Navigation timeout|"
        r"Timeout \d+ms exceeded|deadline exceeded",
        re.IGNORECASE,
    ), "timeout"),

    # TLS / SSL errors
    (re.compile(
        r"CERTIFICATE|SSL[:\s]|TLS[:\s]|ssl_error|"
        r"handshake fail|certificate verify failed|"
        r"ERR_CERT_",
        re.IGNORECASE,
    ), "tls_error"),
]

# ─── Actionable hints per category ───────────────────────────────────────────

_HINTS: dict[str, str] = {
    "connection_refused": (
        "Target refused the connection. The site may be down, or your network "
        "environment blocks outbound connections to this host. Do NOT retry the "
        "same URL - try a different source or check network connectivity."
    ),
    "connection_reset": (
        "Connection was forcibly closed by the remote host (likely anti-bot "
        "firewall or network policy). Retry once after a few seconds; if it "
        "persists, switch sources or use a proxy."
    ),
    "dns_failure": (
        "DNS resolution failed. The domain may not exist or your DNS is blocked. "
        "Verify the URL is correct; do NOT retry."
    ),
    "timeout": (
        "Request timed out. The site may be slow or your network is restricted. "
        "Retry once with a longer timeout; if it persists, switch sources."
    ),
    "tls_error": (
        "TLS/SSL handshake failed. Possible certificate issue or network "
        "interception. Do NOT retry without changing network/proxy."
    ),
    "proxy_error": (
        "Proxy connection failed. Check HOUND_SEARCH_PROXY or the proxy parameter."
    ),
    "unknown": (
        "Fetch failed. Check the error field for details; try a different "
        "source or retry with different parameters."
    ),
}


def classify_network_error(error_str: str) -> Tuple[str, str]:
    """Classify a raw network error string into (category, actionable_hint).

    Categories:
    - connection_refused: target refused TCP connection (site down / env blocked)
    - connection_reset: remote host forcibly closed connection (anti-bot / firewall)
    - dns_failure: domain name resolution failed
    - timeout: request timed out (site slow / network restricted)
    - tls_error: TLS/SSL handshake failure
    - proxy_error: proxy connection failure
    - unknown: cannot classify

    The hint is an agent-friendly sentence explaining what happened and what
    to do next (retry / switch / give up).

    Never raises. Returns ("unknown", generic_hint) for empty or unparseable input.
    """
    if not error_str:
        return "unknown", _HINTS["unknown"]

    for pattern, category in _PATTERNS:
        if pattern.search(error_str):
            return category, _HINTS[category]

    return "unknown", _HINTS["unknown"]


def get_hint(category: str) -> str:
    """Get the actionable hint for a category (fallback to 'unknown')."""
    return _HINTS.get(category, _HINTS["unknown"])
