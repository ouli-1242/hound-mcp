"""Hound's own content extraction.

Replaces scrapling's Convertor._extract_content with direct trafilatura +
markdownify + lxml usage. The extraction chain:

1. Trafilatura (primary, for markdown/text/article/structured)
2. markdownify (fallback for markdown/html types)
3. Raw text (last resort: regex tag stripping)

CSS selector narrowing uses lxml directly.
"""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger("hound_mcp.extractor")


def extract_content(
    page,
    extraction_type: str = "markdown",
    css_selector: Optional[str] = None,
) -> List[str]:
    """Extract content from a Response object.

    Args:
        page: A Response object (hound_mcp.fetcher.Response) or compatible
              object with .body, .encoding, .url, .css()
        extraction_type: 'markdown', 'html', 'text', 'article', 'structured'
        css_selector: CSS selector to narrow extraction scope

    Returns:
        List of extracted content strings (usually one element).
    """
    from hound_mcp.trafilatura_extractor import extract_with_trafilatura, _fallback_extract

    # Trafilatura is the primary extractor for all text-like types
    if extraction_type in ("markdown", "text", "article", "structured"):
        try:
            result = extract_with_trafilatura(page, extraction_type=extraction_type, css_selector=css_selector)
            if result and any(r.strip() for r in result):
                return result
        except Exception as e:
            logger.debug(f"Trafilatura extraction failed: {e}")

    # Fallback: use markdownify for markdown/html, or raw text
    return _fallback_extract(page, extraction_type, css_selector)
