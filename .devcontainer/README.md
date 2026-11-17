# adorm demo

The demo starts in the terminal below once the codespace is ready, and opens in a new browser tab. Its PostgreSQL
server runs beside the codespace; the first start creates, migrates and seeds the demo database.

- Tab did not open: **Ports** panel → port 5173 → **Open in Browser**.
- Demo not running: run `uv run python demo.py` in the terminal.
- Stop it: Ctrl-C in the terminal where it runs.
- Tests: `uv run pytest` uses the same server (`ADORM_TEST_DATABASE_URL`).
