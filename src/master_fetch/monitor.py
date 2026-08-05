"""Page monitoring for Hound MCP.

Detects content changes on web pages by comparing snapshots stored in SQLite.
Agent-driven (no background daemon): the agent calls smart_monitor periodically
and gets a diff summary.

Storage: reuses the cache DB (SQLite WAL) with a 'monitors' table.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("master_fetch.monitor")


class MonitorResponse(BaseModel):
    url: str = Field(description="Monitored URL")
    status: str = Field(description="unchanged | changed | new | error | removed")
    last_checked: str = Field(default="", description="ISO timestamp of this check")
    previous_check: str = Field(default="", description="ISO timestamp of previous check")
    diff_summary: str = Field(default="", description="Change summary (paragraphs added/modified/removed)")
    content_hash: str = Field(default="", description="SHA-256 of current content")
    content_snippet: str = Field(default="", description="First 500 chars of current content")
    error: str = Field(default="", description="Error message if status=error")
    total_checks: int = Field(default=0, description="Total times this URL has been checked")


async def monitor_check(server, url: str, cache_ttl: int = 0) -> MonitorResponse:
    """Check a URL for content changes against the last snapshot.

    Fetches the page, compares with stored snapshot, updates the snapshot.
    Returns a MonitorResponse with change status and diff summary.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Fetch current content
    try:
        result = await server.smart_fetch(url=url, cache_ttl=cache_ttl, max_content_chars=100000)
        if not result.content_ok:
            return MonitorResponse(
                url=url, status="error", last_checked=now_iso,
                error=result.error or f"fetch failed (status {result.status})",
            )
        current_content = "\n".join(result.content)
    except Exception as e:
        return MonitorResponse(
            url=url, status="error", last_checked=now_iso,
            error=str(e)[:200],
        )

    current_hash = hashlib.sha256(current_content.encode()).hexdigest()[:16]

    # 2. Load previous snapshot from DB
    from master_fetch.cache import get_cached, set_cached
    monitor_key = f"monitor:{url}"
    cached = await get_cached(monitor_key, "monitor", None, ttl=999999999)

    if cached is None:
        # First check — store snapshot, report as "new"
        await _save_snapshot(monitor_key, current_content, current_hash, now_iso, 1)
        return MonitorResponse(
            url=url, status="new", last_checked=now_iso,
            content_hash=current_hash,
            content_snippet=current_content[:500],
            diff_summary="First check — snapshot stored.",
            total_checks=1,
        )

    # 3. Compare with previous
    try:
        prev_data = json.loads(cached["content"][0])
        prev_content = prev_data.get("content", "")
        prev_hash = prev_data.get("hash", "")
        prev_time = prev_data.get("checked_at", "")
        total_checks = prev_data.get("total_checks", 1) + 1
    except (json.JSONDecodeError, KeyError, IndexError):
        prev_content = ""
        prev_hash = ""
        prev_time = ""
        total_checks = 1

    if current_hash == prev_hash:
        # No change
        await _save_snapshot(monitor_key, current_content, current_hash, now_iso, total_checks)
        return MonitorResponse(
            url=url, status="unchanged", last_checked=now_iso,
            previous_check=prev_time, content_hash=current_hash,
            diff_summary="No changes detected.",
            total_checks=total_checks,
        )

    # 4. Compute diff
    diff_summary = _compute_diff_summary(prev_content, current_content)

    # 5. Save new snapshot
    await _save_snapshot(monitor_key, current_content, current_hash, now_iso, total_checks)

    return MonitorResponse(
        url=url, status="changed", last_checked=now_iso,
        previous_check=prev_time, content_hash=current_hash,
        content_snippet=current_content[:500],
        diff_summary=diff_summary,
        total_checks=total_checks,
    )


async def monitor_list() -> list[dict]:
    """List all monitored URLs with their last check status."""
    from master_fetch.cache import _ensure_db
    import aiosqlite
    try:
        db_path = await _ensure_db()
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT url, content FROM cache WHERE extraction_type = 'monitor' ORDER BY url"
            )
            rows = await cursor.fetchall()
        results = []
        for row in rows:
            try:
                content_list = json.loads(row[1])
                data = json.loads(content_list[0]) if content_list else {}
                results.append({
                    "url": row[0].replace("monitor:", ""),
                    "last_checked": data.get("checked_at", ""),
                    "hash": data.get("hash", ""),
                    "total_checks": data.get("total_checks", 0),
                })
            except Exception:
                results.append({"url": row[0].replace("monitor:", ""), "error": "parse error"})
        return results
    except Exception as e:
        return [{"error": str(e)[:100]}]


async def monitor_check_all(server) -> dict:
    """Check ALL monitored URLs for changes in one batch call.

    Returns a summary: which URLs changed, which are unchanged, which errored.
    Designed for periodic agent polling: call this once to get a full status update.
    """
    items = await monitor_list()
    if not items:
        return {"total": 0, "changed": [], "unchanged": [], "errors": [],
                "summary": "No monitored URLs. Use smart_monitor(url=..., action='check') to add."}

    changed, unchanged, errors = [], [], []
    for item in items:
        url = item.get("url", "")
        if not url or "error" in item:
            continue
        try:
            result = await monitor_check(server, url)
            if result.status == "changed":
                changed.append({"url": url, "diff_summary": result.diff_summary})
            elif result.status == "error":
                errors.append({"url": url, "error": result.error})
            else:
                unchanged.append(url)
        except Exception as e:
            errors.append({"url": url, "error": str(e)[:100]})

    total = len(changed) + len(unchanged) + len(errors)
    summary_parts = []
    if changed:
        summary_parts.append(f"{len(changed)} changed")
    if unchanged:
        summary_parts.append(f"{len(unchanged)} unchanged")
    if errors:
        summary_parts.append(f"{len(errors)} errors")

    return {
        "total": total,
        "changed": changed,
        "unchanged": unchanged,
        "errors": errors,
        "summary": "; ".join(summary_parts) if summary_parts else "all clear",
    }


# Maximum content stored per snapshot (diff only needs paragraph-level comparison,
# not full text). Prevents unbounded DB growth with many monitored URLs.
_MAX_STORED_CONTENT = 10000


async def _save_snapshot(monitor_key: str, content: str, content_hash: str,
                         checked_at: str, total_checks: int) -> None:
    """Save a monitor snapshot to the cache DB."""
    from master_fetch.cache import set_cached
    snapshot = json.dumps({
        "content": content[:_MAX_STORED_CONTENT],  # cap for diff; hash uses full content
        "hash": content_hash,
        "checked_at": checked_at,
        "total_checks": total_checks,
    })
    await set_cached(monitor_key, "monitor", [snapshot], 200, None, 999999999)


def _compute_diff_summary(old: str, new: str) -> str:
    """Compute a human-readable diff summary between old and new content.

    Splits by paragraphs (double newline) and counts additions/removals/changes.
    """
    old_paras = [p.strip() for p in old.split("\n\n") if p.strip()]
    new_paras = [p.strip() for p in new.split("\n\n") if p.strip()]

    old_set = set(old_paras)
    new_set = set(new_paras)

    added = len(new_set - old_set)
    removed = len(old_set - new_set)
    unchanged = len(old_set & new_set)

    parts = []
    if added:
        parts.append(f"{added} paragraph(s) added")
    if removed:
        parts.append(f"{removed} paragraph(s) removed")
    if unchanged:
        parts.append(f"{unchanged} unchanged")

    if not parts:
        return "Content format changed (same paragraph count but minor edits)"

    summary = ", ".join(parts)

    # Add size change
    size_diff = len(new) - len(old)
    if size_diff > 0:
        summary += f" (total length +{size_diff} chars)"
    elif size_diff < 0:
        summary += f" (total length {size_diff} chars)"

    return summary
