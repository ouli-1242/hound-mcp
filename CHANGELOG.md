# Changelog

This file was (re)introduced at 11.1.8. Releases before that are **not**
backfilled here — see `git log` and the GitHub releases page for history.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

> **Versioning note.** From 13.14 on this is a personal derivative work and no
> longer tracks the upstream project's releases, so its version numbers are its
> own and are not comparable to upstream's. `__version__` in
> `src/hound_mcp/__init__.py` is the single source of truth.

## [Unreleased]

### Fixed
- **Credential leak in logs.** `fetcher.py` wrote raw exception text into
  `logger.warning` on retry, so a proxy URL containing `user:pass@` ended up in
  the logs. It now goes through `security.redact_api_key()`, which the rest of
  the codebase already used.
- **`RuntimeWarning: coroutine 'ProxyPool.health_check' was never awaited`.**
  `_kick_health_check()` evaluated `pool.health_check()` before calling
  `asyncio.create_task()`, so whenever there was no running event loop the
  coroutine was built and then discarded. The loop is now checked first.
- **Stale type annotations.** `server.py` still annotated two helpers with
  `_ScraplingResponse`, a name that no longer exists anywhere; `ocr.py` and
  `server.py` referenced `PdfResult` / `CrawlResponseModel` without importing
  them. Annotations are lazy here, so nothing crashed, but the types were wrong.
- Removed dead assignments (`browser.py`, `pdf_extractor.py`,
  `search_engines.py`, `server.py`) and renamed ambiguous `l` variables.

### Changed
- **Single source of truth for the version.** `pyproject.toml` no longer
  hardcodes `99.0.0`; it reads `__version__` from `src/hound_mcp/__init__.py`
  via `[tool.hatch.version]`. Previously `pip show` reported `99.0.0` while the
  CLI reported `11.1.8` and `package.json` said `11.1.6`.
- Declared dependencies that were imported but not listed:
  `beautifulsoup4` (imported in `search_engines.py` — a missing install silently
  degraded that code path to an empty result), plus `h2` and `httpcore`, which
  were only resolving transitively through `httpx[http2,socks]`.

### Added
- `.github/workflows/test.yml` and `.github/workflows/lint.yml` — CI was absent
  from the repository, so nothing enforced the test suite or lint.
- `CONTRIBUTING.md` and this changelog.
- `[tool.ruff]` config and `ruff` in the `dev` extras.

### Known gaps
- `auth` / `proxy_auth` on the fetch tools are validated but never applied:
  `HTTPSession` / `http_get` do not accept them. Preserved behaviour for now;
  see the comment at `src/hound_mcp/server.py` in `bulk_get`.
- `package.json` still points at `pi-extension/extensions/hound.ts`, which is
  not present in the repository.
