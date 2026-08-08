"""Local file parsing for Hound MCP.

Converts local files (.html, .docx, .xlsx, .csv) to Markdown so agents can
read documents without a web fetch. Part of the [all] extra (python-docx,
openpyxl). Graceful degradation: if deps are missing, returns a clear error.

Supported formats:
- .html / .htm  → trafilatura + markdownify (reuses existing extraction)
- .docx         → python-docx: headings, paragraphs, tables → Markdown
- .xlsx         → openpyxl: sheets → Markdown tables
- .csv          → stdlib csv: → Markdown table
- .pdf          → hint to use smart_fetch (already handles PDFs)
"""

from __future__ import annotations

import csv
import logging
import os
from typing import Optional

logger = logging.getLogger("hound_mcp.parse")

# Supported extensions
SUPPORTED_EXTENSIONS = {".html", ".htm", ".xhtml", ".docx", ".xlsx", ".csv", ".pdf"}


# Maximum file size for parsing (50 MB) to prevent OOM on huge files
MAX_PARSE_FILE_SIZE = 50 * 1024 * 1024


def parse_file(file_path: str) -> tuple[str, str]:
    """Parse a local file to Markdown.

    Returns (content: str, error: str). On success error is empty.
    On failure content is empty and error explains what went wrong.
    Never raises.
    """
    if not file_path:
        return "", "file_path is required"

    # Expand ~ and resolve to absolute
    file_path = os.path.expanduser(file_path)
    if not os.path.isabs(file_path):
        file_path = os.path.abspath(file_path)

    if not os.path.exists(file_path):
        return "", f"File not found: {file_path}"

    # Guard against OOM: reject files larger than 50 MB
    file_size = os.path.getsize(file_path)
    if file_size > MAX_PARSE_FILE_SIZE:
        return "", (
            f"File too large ({file_size / 1024 / 1024:.1f} MB). "
            f"Maximum supported size is {MAX_PARSE_FILE_SIZE // 1024 // 1024} MB."
        )

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return "", (
            f"Unsupported file type '{ext}'. Supported: "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    try:
        if ext in (".html", ".htm", ".xhtml"):
            return _parse_html(file_path), ""
        elif ext == ".docx":
            return _parse_docx(file_path), ""
        elif ext == ".xlsx":
            return _parse_xlsx(file_path), ""
        elif ext == ".csv":
            return _parse_csv(file_path), ""
        elif ext == ".pdf":
            return "", (
                "PDF files are handled by smart_fetch (with OCR support). "
                "Use: smart_fetch(url='file:///path/to/file.pdf') or pass the PDF URL."
            )
    except ImportError as e:
        return "", (
            f"Missing dependency for {ext} parsing: {e}. "
            f"Install with: pip install hound-mcp[all]"
        )
    except Exception as e:
        return "", f"Parse error ({ext}): {type(e).__name__}: {str(e)[:200]}"

    return "", f"Unsupported format: {ext}"


def _parse_html(file_path: str) -> str:
    """Parse HTML file using trafilatura + markdownify (existing chain)."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    from hound_mcp.trafilatura_extractor import extract_content_from_html
    result = extract_content_from_html(html, file_path, "markdown")
    if result:
        return result
    # Fallback: markdownify on the raw HTML
    try:
        from markdownify import markdownify
        return markdownify(html, heading_style="ATX").strip()
    except Exception:
        return html[:50000]  # last resort: raw HTML truncated


def _parse_docx(file_path: str) -> str:
    """Parse .docx to Markdown using python-docx."""
    from docx import Document
    from docx.table import Table

    doc = Document(file_path)
    lines: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            lines.append("")
            continue
        # Map heading styles to Markdown headings
        style = (para.style.name or "").lower()
        if "heading 1" in style or style == "title":
            lines.append(f"# {text}")
        elif "heading 2" in style:
            lines.append(f"## {text}")
        elif "heading 3" in style:
            lines.append(f"### {text}")
        elif "heading 4" in style:
            lines.append(f"#### {text}")
        elif "list" in style:
            lines.append(f"- {text}")
        else:
            lines.append(text)

    # Extract tables
    for table in doc.tables:
        lines.append("")
        lines.append(_table_to_markdown(table))
        lines.append("")

    return "\n".join(lines).strip()


def _parse_xlsx(file_path: str) -> str:
    """Parse .xlsx to Markdown tables using openpyxl."""
    from openpyxl import load_workbook

    wb = load_workbook(file_path, read_only=True, data_only=True)
    sections: list[str] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        # Stream rows with a cap to prevent OOM on huge sheets
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 200:  # header + 100 data + margin
                break
            rows.append(row)
        if not rows:
            continue
        # Filter out completely empty rows
        rows = [r for r in rows if any(cell is not None for cell in r)]
        if not rows:
            continue

        section = f"## {sheet_name}\n\n"
        # Convert to markdown table
        header = rows[0]
        col_count = len(header)
        header_str = "| " + " | ".join(str(c) if c is not None else "" for c in header) + " |"
        sep_str = "| " + " | ".join("---" for _ in range(col_count)) + " |"
        data_strs = []
        for row in rows[1:100]:  # cap at 100 data rows
            cells = list(row) + [None] * (col_count - len(row))  # pad short rows
            data_strs.append("| " + " | ".join(str(c) if c is not None else "" for c in cells[:col_count]) + " |")

        section += "\n".join([header_str, sep_str] + data_strs)
        if len(rows) > 101:
            section += f"\n\n... ({len(rows) - 101} more rows shown, sheet may have more)"
        sections.append(section)

    wb.close()
    return "\n\n".join(sections) if sections else "(empty workbook)"


def _parse_csv(file_path: str) -> str:
    """Parse .csv to a Markdown table using stdlib csv.

    Streams rows with a cap (101) to prevent OOM on huge CSV files.
    """
    rows = []
    has_more = False
    with open(file_path, "r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i >= 101:  # header + 100 data rows
                has_more = True
                break
            rows.append(row)

    if not rows:
        return "(empty CSV)"

    header = rows[0]
    col_count = len(header)
    header_str = "| " + " | ".join(header) + " |"
    sep_str = "| " + " | ".join("---" for _ in range(col_count)) + " |"
    data_strs = []
    for row in rows[1:]:
        cells = row + [""] * (col_count - len(row))
        data_strs.append("| " + " | ".join(cells[:col_count]) + " |")

    result = "\n".join([header_str, sep_str] + data_strs)
    if has_more:
        result += "\n\n... (100+ rows, truncated for display)"
    return result


def _table_to_markdown(table) -> str:
    """Convert a python-docx Table to a Markdown table string."""
    rows_data: list[list[str]] = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        rows_data.append(cells)

    if not rows_data:
        return ""

    col_count = max(len(r) for r in rows_data)
    # Pad rows to same length
    for r in rows_data:
        while len(r) < col_count:
            r.append("")

    header_str = "| " + " | ".join(rows_data[0]) + " |"
    sep_str = "| " + " | ".join("---" for _ in range(col_count)) + " |"
    data_strs = ["| " + " | ".join(r) + " |" for r in rows_data[1:]]

    return "\n".join([header_str, sep_str] + data_strs)
