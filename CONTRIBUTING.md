# Contributing

Thanks for looking at Hound. This is a small, opinionated MCP server; the notes
below are what you need to get a green local run.

## Setup

```bash
git clone https://github.com/ouli-1242/hound-mcp.git
cd hound-mcp
python -m pip install -e ".[dev]"
```

Optional extras (browser engine, PDF, OCR, local file parsing) are in `[all]`:

```bash
python -m pip install -e ".[dev,all]"
playwright install chromium          # required for the anti-bot browser path
```

Without `[all]`, hound degrades to HTTP-only mode instead of failing — that
degradation is a supported configuration and is covered by tests.

## Tests

```bash
pytest                    # default run; e2e tests are deselected
pytest -m e2e             # spawns a real hound.exe subprocess
pytest tests/test_search.py -q
```

Two things worth knowing before you debug a "test doesn't see my change":

1. **`pythonpath = ["src"]` in `pyproject.toml` is load-bearing.** The dev venv
   often has a *built* wheel installed rather than an editable install. Without
   that setting, pytest silently imports the stale installed copy and your edits
   to `src/` go untested. This has bitten the project more than once.
2. `addopts = ["-m", "not e2e"]` deselects the e2e tests by default. They are
   slower and need a built executable.

## Lint

```bash
ruff check src tests      # config lives in pyproject.toml [tool.ruff]
```

CI runs `pytest` (Ubuntu 3.11–3.14, Windows 3.11) and `ruff check` on every push
to `master` and every pull request.

## Layout

```
src/hound_mcp/
  server.py              MCP tool schemas + dispatch + orchestration
  fetcher.py             primp-based HTTP session and Response
  browser.py             patchright stealth browser
  search*.py             keyless metasearch backends, proxy pool, reranker
  crawl.py, sitemap.py   same-domain crawling
  pdf_extractor.py, ocr.py, parse.py, structured.py, metadata.py
  extractor.py, trafilatura_extractor.py    HTML -> markdown paths
  security.py            URL validation, SSRF checks, credential redaction
  envelope.py, errors.py, cache.py          cross-cutting helpers
tests/                   unit + integration tests (no live network by default)
```

## Conventions

- Commit messages follow `type(scope): summary` — e.g. `fix(search): ...`,
  `feat(server): ...`, `docs: ...`, `chore: ...`.
- Never commit credentials. API keys are read from environment variables only;
  see the note in `src/hound_mcp/search_metasearch.py`. Error text and logs must
  go through `security.redact_api_key()` before being surfaced.
- Every behaviour change ships with a test that fails without it.
- Keep tool definitions token-lean — the tool descriptions are part of the
  prompt budget for every agent session.

## Reporting bugs

Include the `hound -v` output, the exact tool call, and whether the browser
extras were installed. `HOUND_DEBUG=1` (or `-v`) increases log verbosity.
