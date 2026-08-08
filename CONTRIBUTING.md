# Contributing

Issues and pull requests are welcome. AI-generated issues are fine. Just make sure they're real problems you encountered while using Hound, not hypotheticals.

## Reporting issues

Open an issue on GitHub. Include:

- What you were trying to do
- The URL you tried to fetch or query you searched
- The error or unexpected behavior
- Your Python version and OS

Bug report and feature request templates are available when you open a new issue.

## Pull requests

- Keep changes focused. One problem per PR.
- Run `pytest tests/` before submitting.
- If adding features, include tests.

## Development

```bash
git clone https://github.com/ouli-1242/hound-mcp.git
cd hound-mcp
pip install -e .[all,dev]
playwright install chromium
pytest tests/
```

The fetch engine is in `src/hound_mcp/server.py`. Search is in `src/hound_mcp/search.py`.

## License

MIT. By contributing, you agree to license your work under the same terms.
