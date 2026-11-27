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

## Read-only GitHub agent work

The Agents page (`/agents`) reads issues and pull requests directly from GitHub.
It shows open work grouped by repository, linked PRs beneath their issue, a pinned
waiting-on-you list, and a collapsed list of work closed or merged in the last
seven days. No GitHub data is stored in SQLite and no GitHub writes are made.

Server environment settings:

- `TODO_V2_GITHUB_TOKEN`: supply a fine-grained, read-only token through the server
  environment. Give it Metadata, Issues, Pull requests and Actions read access to
  the selected repositories. Never put its value in source, images or client config.
- `TODO_V2_GITHUB_REPOS=owner/name`: comma-separated repository allowlist. Invalid
  entries are ignored and duplicates removed. Both a token and a non-empty valid
  allowlist are required; otherwise Agents says “GitHub not configured”.
- `TODO_V2_GITHUB_POLL_MINUTES=5`: background polling interval, clamped to 5–15
  minutes (invalid integers use 5). Refresh has a shared 60-second floor, including
  failed attempts. Rate-limit responses, remaining quota of 200 or fewer requests, and transport errors
  extend the cooldown; cached work remains visible with a safe error message.

`GET /api/agent-work` serves the in-memory snapshot; `POST /api/agent-work/refresh` requests a
refresh subject to the floor and cooldown. `next_refresh_at` tells the UI when it
can try again. `last_poll_at` is the last attempt; `last_updated_at` is the last
complete successful snapshot. `/health` adds `github` with only `configured`,
`last_poll_at`, and `last_error`. Neither endpoint includes the credential.

Requests use ETags for every page, open PR detail, review list and Actions run list.
Closed PRs use the issue list's merge timestamp without a separate PR detail request.
CI uses the latest run per workflow for the current head, ignoring `claude.yml`.
A comment review does not revoke an approval; a dismissed approval or outstanding
change request does. A PR needs successful (or neutral/skipped) Actions runs,
approval, a non-draft state and a clean mergeable result to wait on you implicitly;
`needs-dev` explicitly flags either an issue or PR. Absent CI does not imply green.

Runtime GitHub requests always use `https://api.github.com`; the API base is not
an environment setting. Code-level tests can construct `Config` directly.
The tests inject a fake GET transport and prohibit network connections. Run the
normal v2 pytest and frontend TypeScript commands above; no extra dependencies
are needed. Home, tasks and From v1 work independently of GitHub configuration.
