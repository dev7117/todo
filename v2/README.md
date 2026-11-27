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

## Optional v1 view

Set `TODO_V2_V1_DB` to a v1 database to show its open
tasks in the read-only "From v1" section. Unset or missing files hide the section;
`GET /api/from-v1` returns `{"enabled": false, "items": []}`. Enabled responses contain
an array of titles, excluding `done` and `cancelled`. An unreadable or incompatible
database returns 503 without exposing its path.

For a preview container, add these arguments to `docker run`:

```bash
--mount type=bind,src=/path/to/v1-data,dst=/legacy,readonly \
--env TODO_V2_V1_DB=/legacy/todo.db
```

Mount the whole v1 data folder read-only so the `-wal` and `-shm` files are visible
when present. The connection uses SQLite URI `mode=ro` when a `-wal` file exists
beside the database, making committed WAL rows visible. Otherwise it uses
`mode=ro&immutable=1` to read the main file without creating sidecars, including
for a WAL-mode file in a read-only directory. The view never writes or checkpoints
the source database. This view does not import or edit v1 tasks; #20 owns the
import, within the #27 rebuild.

## Design

`frontend/src/index.css` is v1's stylesheet copied verbatim, and `frontend/src/theme.tsx` is v1's theme (the six Underground accent presets, ink by luminance, light/dark/system via `.dark` on `<html>`). Restyle both apps together or not at all.

## Tasks (core tracker stack PR 1)

Migration 0003 copies `items` to the constrained `tasks` table and drops `items`.
IDs, titles, notes and timestamps survive unchanged; `open` becomes `todo` and
`done` stays `done`. Existing rows have no parent. The invented schema-2 fixture
`tests/fixtures/items-v2.sqlite3` exercises this upgrade, including its backup;
tests copy it before migration and check its source checksum afterward.

`GET/POST /api/tasks` lists or creates tasks (default `todo`).
`PATCH /api/tasks/{id}` accepts title, notes, status and nullable `parent_id`.
Statuses are `inbox`, `todo`, `in_progress`, `waiting`, `done` and `cancelled`.
`POST /api/tasks/{id}/complete` sets `done`; `/reopen` sets `todo`.
Only entering `done` sets `completed_at`; leaving it clears that timestamp.
Identical edits are no-ops, including timestamps and history. Missing tasks or
parents return 404; invalid fields, empty titles and parent cycles return 422.
There is no delete endpoint or old `/api/items` alias.

`GET /api/tasks/{id}/history` returns changes in insertion order. Creation records
a JSON snapshot; subsequent entries contain the field and its old/new text value
(parent IDs are text, or null). The items era had no history, so migrated rows
begin recording on their next edit. History inserts and task changes share a
transaction, and SQL triggers reject history updates/deletes. Parent validation
runs within the same immediate write transaction to prevent concurrent cycles.

Every v2 connection uses WAL, foreign keys and a 5000 ms busy timeout through
`todo_v2.db.connect`. New task, history and migration timestamps use UTC ISO-8601
with six fractional digits and a trailing `Z`; copied timestamps are untouched.
The separate read-only From v1 connection policy is unchanged.

The home page supports inline title/notes edits, status changes, parent selection,
subtask creation and history. Done/cancelled tasks start collapsed. A child whose
parent belongs to the other section stays visible in its own status section,
with the parent named on its row. Changing a parent's status does not change its
children. Projects, Ideas, Today, markdown blocks and search belong to later PRs.
