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
children. Ideas, markdown blocks, Today and search are described below.

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

## Projects (core tracker stack PR 2)

Migration 0004 adds projects (`active`, `paused`, `done`, `archived`), optional
`target_on` dates and nullable task `project_id` with `ON DELETE SET NULL`.
Existing tasks remain unassigned. The migration rebuilds the history CHECK to
allow `project_id`, preserving all rows and the append-only triggers. The
invented schema-3 fixture `tests/fixtures/tasks-v3.sqlite3` is copied before
migration; tests verify its checksum, preserved tasks/history and backup.

`GET/POST /api/projects` lists or creates projects. `GET /api/projects/{id}`
includes assigned tasks and progress; `PATCH /api/projects/{id}` edits title,
Markdown description, status and target date. Titles are trimmed and must be
non-empty. Dates must be `YYYY-MM-DD` or null. There is no delete endpoint.
Missing projects return 404; invalid fields return 422. Identical edits leave
`updated_at` alone. New timestamps use `todo_v2.db.now()`.
Creating a project with `status: "done"` returns 422; complete it after creation.

The UI's **Mark done** button and status editor use `PATCH /api/projects/{id}`
with `status: "done"`. This is a UI-facing REST surface, not a caller identity
check: there is no MCP server, agent tool or agent completion route. The router
surface is pinned by a test so a later agent/MCP addition must revisit that rule.

Assign or unassign using `PATCH /api/tasks/{id}` with `project_id` or null.
Unknown project IDs return 422. Changes record old/new IDs in task history in
the same transaction; identical assignments change neither history nor timestamps.
New subtasks inherit their parent’s `project_id` inside the creation transaction;
the initial history snapshot includes that assignment. Create requests do not
accept explicit `project_id`; use PATCH to override or clear it afterward.
Moving a parent later does not move its existing children. Completing a parent
leaves children alone. Cancelled tasks have no completion timestamp and an unchecked, disabled
checkbox; change their status using the status picker to resume them.

The shared `projects.progress` helper implements PRD §4.4 for one project: count
only its non-cancelled tasks without non-cancelled children in the same project,
then divide done by total. A parent with only cancelled children counts as a leaf
and contributes to done only when the parent is done. Children on other projects
do not replace the parent. Zero counted tasks yields zero progress in the API
and “Nothing linked yet” in the UI.
Project status never changes automatically from task progress.

`/projects` and `/projects/{id}` reuse the header’s History API navigation and
small pathname switch; project creation uses the same navigation helper.
The existing SPA fallback supports direct links.
The task page and project page reuse the task rows and their project picker.
Descriptions are stored as Markdown and displayed as plain text in this slice;
Blocks use the safe Markdown subset described below. No dependencies were added.

## Ideas and blocks (core tracker stack PR 3)

Migration 0005 adds ideas and owner-scoped blocks, and extends task history's
CHECK with `blocks` while preserving its rows and append-only triggers.
The invented `tests/fixtures/projects-v4.sqlite3` contains only sample projects,
tasks and audit rows from migrations 0001–0004. Tests copy it before upgrading,
verify one pre-upgrade online backup, and check its unchanged source checksum.

`GET/POST /api/ideas` lists or creates ideas. List filters are `status` and
`project_id` (combined with AND); results are ordered by ID. The default status
is `seed`; other statuses are `exploring`, `parked`, `promoted`, `dropped`.
`GET /api/ideas/{id}` includes ordered blocks; PATCH edits title, summary,
status and nullable project assignment. Unknown projects return 422; missing
ideas return 404. There is no delete or promote action. An ordinary status edit
to `promoted` creates nothing. Summaries display as plain text.

For either `/api/tasks/{id}` or `/api/ideas/{id}`:

- `GET /blocks` reads the ordered blocks; `POST /blocks` adds `{kind, body}`.
- `PUT /blocks/order` accepts `{ids: [...]}`, a complete permutation of that
  owner's current block IDs. Stale, duplicate, missing or foreign IDs return 422.
- `PATCH /api/blocks/{id}` edits `body` and/or a strict boolean `done`.
- `DELETE /api/blocks/{id}` returns 204.

Kinds are `note` and `subtask` (shown as “Checklist” in the UI). Kind and owner
are immutable. Only checklist blocks accept `done`; checking sets `done_at`,
unchecking clears it, and identical edits are no-ops. SQL enforces exactly one
owner, unique nonnegative positions, and valid completion state. Block IDs are
never reused, so audit snapshots cannot confuse deleted blocks with new ones.
Missing owners/blocks return 404. All writes serialize validation and changes
in one immediate transaction through the existing connection helper.

Task block changes append one `blocks` history entry with ordered before/after
JSON snapshots in the same transaction and update the task timestamp. Ideas
have no audit table; block changes update the idea timestamp. The project
progress query is unchanged: ideas and checklist blocks never count as tasks.

`/ideas` groups ideas by status and filters by status/project. `/ideas/{id}`
shows the summary, project link and shared blocks editor. Task rows expose
“Notes & checklist” separately from their existing notes and parent subtasks.
Project pages list linked ideas as context. Navigation uses the existing
History API helper and styles reuse the existing component classes.

No Markdown dependency was added. `todo_v2.markdown` implements a deliberately
small subset: paragraphs/line breaks, headings, unordered lists, fenced/inline
code, strong/emphasis and links. Raw HTML always becomes escaped text. Links
allow only HTTP(S), mailto and fragment targets; unsafe destinations are removed.
The API derives `body_html` from stored Markdown on every response, and only
that derived HTML reaches the UI rendering sink. XSS and syntax tests run in
the regular v2 pytest job. This is a subset, not a full CommonMark parser.

Run the v2 pytest and TypeScript commands above, and repo-root pytest for v1.
Conversion/promotion actions and goals remain outside this slice of issue #18
(tracking issue #27).


## Today, quick add and search (core tracker stack PR 4)

Migration 0006 adds nullable `tasks.today_on`, extends append-only task history
with `today_on`, and backfills three FTS5 external-content indexes. Source-table
insert, update and delete triggers keep task titles/notes, idea titles/summaries
and block bodies searchable. Startup probes FTS5 on every boot and aborts with
an explicit error if the runtime SQLite lacks it; there is no scan fallback.
The invented schema-5 fixture `tests/fixtures/ideas-v5.sqlite3` is copied before
migration. Tests pin its checksum, one pre-upgrade backup, preserved source and
history rows, and searchable backfilled content. Earlier migrations are unchanged.

`TODO_V2_TZ` defaults to `America/Detroit`. A server-side clock supplies the local
date; tests inject UTC instants, including local midnight. `POST /api/tasks` and
`PATCH /api/tasks/{id}` accept strict boolean `today`: true sets the server's
current date, false clears it. Each changed date records `today_on` history in
the same transaction. Responses expose the stored date and read-time `is_today`.
`GET /api/tasks/today` includes only today's unfinished, non-cancelled tasks.
Old dates drop off at read time only, with no clearing write or background job.
The Today page refreshes when reopened or the window regains focus.

The Home and Today add boxes are title-only quick add using `POST /api/tasks`.
The default remains **`todo`**, matching the existing API and SQL default.
Only the Today box adds `today: true`; Home does not schedule the task.
Empty/whitespace titles return 422. There is no natural-language parsing.
Task rows include a Today toggle, including on project and task detail pages.

`GET /api/search?q=...` accepts up to 500 characters and returns at most **50**
hits, ordered by FTS rank, kind and ID. Input is escaped as one quoted literal
phrase, so quotes, punctuation and reserved words never become MATCH operators.
FTS tokenization still applies (punctuation alone has no tokens). Blank queries
return no hits. Results show source kind and a plain-text excerpt (240 characters),
linking to `/tasks/{id}` or `/ideas/{id}`; block hits link to their owner. Task
detail links open notes and history, including finished tasks. Search URLs retain
the query across reload and History API navigation. No keyboard map is added.

Markdown rendering limits preview input to **100,000 characters** and appends a
truncation notice. Stored text remains intact and editable. Link labels, including
quotes and HTML, are escaped independently of URL validation. The existing XSS
coverage remains in place; no new dependencies are required.
