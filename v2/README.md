# Todo v2

The v2 rebuild, growing beside v1 (the repo root) until cutover. FastAPI + SQLite, React 18 / Vite 5 / Tailwind 3. It publishes `todo:v2`; v1 keeps `latest`.

## Checks

```bash
cd v2
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
cd frontend && npm ci && npx tsc --noEmit
```

The repo-root v1 `pytest` run skips this tree (`v2/conftest.py`).

## Run locally

`TODO_V2_DB=/tmp/todo-v2.db .venv/bin/python main.py` (port 7680), and in another terminal `cd frontend && npm run dev` (Vite proxies `/api` and `/health`).

Container: `docker build -f v2/Dockerfile -t todo-v2:local v2`, then run it with a volume on `/data`. Settings are all `TODO_V2_*` (`TODO_V2_DB`, default `/data/todo-v2.db` in the image; `TODO_V2_PORT`, default 7680) so v2 never opens v1's database.

`GET /health` returns `{"ok": true, "version": ..., "commit": ..., "schema_version": ...}`.

## Migrations

`todo_v2/migrations/NNNN_name.sql`, numbered from 0001 without gaps, forward-only. Never edit a shipped migration; add the next number. Each runs in its own transaction and is recorded in `schema_migrations` (`PRAGMA user_version` mirrors the latest).

On startup, if anything is pending, the runner first takes one online backup (SQLite backup API) to `<db>.pre-migrate-<UTC stamp>-v<current>.bak` beside the database, then applies every pending migration. Nothing pending means no backup.

## Design

`frontend/src/index.css` is v1's stylesheet copied verbatim, and `frontend/src/theme.tsx` is v1's theme (the six Underground accent presets, ink by luminance, light/dark/system via `.dark` on `<html>`). Restyle both apps together or not at all.
