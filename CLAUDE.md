# todo

A todo list for personal and work projects, served from the Synology NAS. One FastAPI process serves:
- the web UI (React SPA, house style)
- the REST API at `/api/*`
- **customer hubs**: topics (where things stand + a timeline of updates), upcoming meetings with prep, meeting recaps, links and desktop tools, per customer
- a first-party **MCP server** at `/mcp`, for Claude: recaps, calendar sync and prep, topic updates, and *proposed* task changes

Everything shares one SQLite database. Tasks come from the app, the bar's quick-add hotkey, sync scripts (`POST /api/ingest`) and Claude (MCP).

## Model

- **Area**: `work` | `personal`. **Projects** belong to an area and optionally a **customer** (`customers` table; a task's customer comes from its project). Passing a customer *name* to project create/update finds it case-insensitively or creates it. A task's area follows its project.
- **Status**: `inbox` → `todo` → `in_progress` → `waiting` (`waiting_on`) → `done` / `cancelled`. Untriaged captures land in `inbox`.
- **Today**: a flag stored as the date it was set (`today_on`), so it carries over and shows "since Thu". The bar and the Today page show today ∪ in-progress, ordered by `sort_key` (drag to reorder).
- **History** (`task_updates`): `created` / `change` (readable field diffs) / `note` (progress), each tagged with a `source` (`app`, `intake`, `mcp`, `meeting: Acme weekly 2026-10-04`, `jira-acme`…). Every write goes through `Store`, so all entry points log the same way.
- **Customer hub** (`todo_app/hub.py`, `/customers/:id`):
  - Customer profile: website, logo (uploaded, or fetched from the website into `<data>/logos/`), and notes. (`overview` is the retired single overview; see Topics.)
  - **Topics** (`customer_topics` + `topic_updates`, migration 10) are how the hub shows where things stand. The Overview tab's "Topics" section (`components/hub/TopicsSection.tsx`) has one card per topic:
    - **Where things stand** (`summary`, with `stand_source` / `stand_updated_at`) is rewritten as that topic changes. Only that topic's is touched.
    - **Timeline** (`topic_updates`: body, `happened_on`, optional `meeting_id`, source) is append-only for Claude. Only the user can delete an entry.
    - **Status**: active / watching / resolved.

    `Hub.topics_view(days)` lists the topics updated in the window (Today / This week / Two weeks / One month in the UI; `GET /api/customers/{id}/topics?days=`), busiest first. The rest count as "quieter".
  - Claude writes topics through `log_meeting(topics=[{name, update, where_things_stand?, status?}])` or `log_topic_updates`. `set_customer_overview` and the old `update_topics` are gone, because each run rewrote everything.
  - `customers.overview` is the retired single overview. It shows read-only as "Earlier overview" until removed, and `get_customer` returns it as `earlier_overview` for Claude to fold into topics.
  - Migration 10 seeded each topic's timeline with its old summary (`source='migrated'`, labelled "from the old summary").
  - Each card shows only today's updates, or the latest one. Clicking the topic opens `/topics/:id` (`pages/TopicPage.tsx`, `GET /api/topics/{id}`), which has the full timeline by month, plus editing where it stands, status, rename and delete.
  - Duplicate topics can be merged (`POST /api/topics/{id}/merge`): the timeline moves over, and the merged topic's where-things-stand goes into the timeline.
  - `meetings`: `scheduled` rows synced from the calendar (keyed by `calendar_id`, with `prep`) or `held` recaps (summary, decisions). Logging a recap with a `calendar_id` upgrades the scheduled row. `meeting_tasks` links a meeting to the tasks it produced.
  - `customer_links`: bookmarks, plus **desktop tools** (commands run by todo-agent; see below).
- **Desktop tools and todo-agent** (`todo_app/agents.py`, `todo_app/agent_dist/`):
  - A hub tool is a command that runs on one of the user's machines: the Mac (launchd) or Omarchy (systemd user service), via `todo-agent`, a single stdlib-only Python file (3.9+).
  - The agent long-polls `POST /api/agent/poll` with the API token. **Run** queues a `launcher_runs` row for a machine: the one picked, else the tool's `agent`, else the only one online. The agent claims the run, executes it, and reports status and output to `POST /api/agent/runs/{id}`.
  - **Interactive** mode opens a terminal (macOS: Terminal / iTerm / Ghostty from `agent.json`; Linux: `xdg-terminal-exec`) and records the TTY session with `script`. **Headless** mode streams output every 2s and sends a notification when done.
  - The agent fingerprints each command itself (sha256 of command + cwd) and asks before running anything new or changed: a native dialog on macOS, a terminal prompt on Linux. "Always allow" goes in `~/.config/todo/agent-approvals.json`.
  - Runs nobody claims expire after 2 minutes. Claimed runs that never start expire after 15.
  - Install: `curl -fsSL <url>/agent/install.sh | sh -s -- "<name>"`. The app serves the installer and agent, and the installer prompts for the token. Settings → Machines lists the connected machines.
- **Proposals** (`todo_app/review.py`, `/review`): Claude never edits tasks directly.
  - `propose_changes` stores a `changesets` row plus `changes` rows (create / update / note / complete / add_link), each with a payload in Store terms and a snapshot of the task.
  - The Review page renders them as a git-style diff, flags tasks changed since the proposal, and applies the approved ids one by one. A failure doesn't block the rest, and new-task titles can be edited before applying. New tasks (and promoted ideas) have a **Today** toggle: the edit `{today: true}` puts the task on your list today and makes it yours (clears any proposed assignee).
  - Applied changes take the changeset's source (e.g. `meeting: Weekly sync 2026-10-05`) and link to its meeting.
  - The setting `review_claude_changes` (default on) controls review; when it's off, proposals apply at once.
- **Agents** (`todo_app/dispatch.py`, `mcp_worker.py`, `agent_templates/`, migration 11): assign a task to an agent and Claude Code works it on your machine.
  - An agent is a person with `kind = 'agent'` plus an `agent_profiles` row: `claude_agent` (it runs as `.claude/agents/<claude_agent>.md` in the repo, which holds its role), `machine`, `model`, `allowed_tools`, `projects`, `auto_dispatch`. Projects have `repo_path` (`~` allowed) and `default_branch`.
  - Assigning one (`Store.on_assigned` hook, any entry point) queues a `launcher_runs` row with `kind = 'task'`. Task runs never expire in the queue; they wait for their machine. If it can't start (no repo, no machine), the task gets a note saying why.
  - todo-agent 1.2+ claims it (`Dispatch.claim`, which issues a run token and stores only its sha256). It makes a worktree (`<state>/worktrees/task-<id>`, branch `agent/<id>-<slug>` from `origin/<default_branch>`) and runs `claude -p --agent <name> --mcp-config … --strict-mcp-config --output-format stream-json`, with `--resume <session>` when going back. The approval fingerprint covers agent + repo + tools + model, never the prompt.
  - The agent talks back over **`/mcp/agent`**, a separate MCP server. Its token works only while the run is live, and only on that task and its children. Tools: `get_assignment`, `report_progress`, `set_status` (in_progress / waiting), `add_subtask`, `check_subtask`, `ask`, `request_review`. These are direct writes with source `agent: <name>`, the same reasoning as `complete_prep_step`. **Done is still a proposal**: `request_review` records the PR, leaves the task waiting on "your review", and proposes `complete`.
  - A run that ends without asking for review puts the task back to waiting on "you", with the log tail. On the task page, `AgentPanel` shows the run, log, branch, PR and tokens used (new input + output; cache reads in the tooltip — no dollars, since it runs on your subscription), with Start / Stop / Send to agent (your reply resumes the session). Stop marks the run cancelled, and the machine kills Claude on its next report.
  - Setup from Claude Code in any repo: the MCP prompt `/mcp__todo__setup_agent`, then `get_agent_templates`, `set_project_repo`, `list_machines`, `save_agent`, `check_agent_setup`, and `test_agent` (a `kind = 'check'` smoke run) with `get_run`. These need the full API token. The person page of an agent shows the same checks.
  - **A repo is optional.** A task with no project, or whose project has no `repo_path`, is general work. The agent runs in a scratch folder (`<state>/workspaces/task-<id>`, kept between runs, so resume works). Its agent file then comes from `~/.claude/agents/<name>.md` on the machine, and it finishes with a summary and no PR. The setup prompt asks whether the agent is a repo agent or a general one.
  - `TODO_AGENT_HOME=<dir>` runs a second todo-agent with its own config, approvals and state (e.g. against the local dev server) while keeping your real home for the claude and gh logins.
  - `scripts/fake-claude.py` (set `TODO_AGENT_CLAUDE` to it) stands in for claude to test the loop without spending tokens.
- **Client sync and the source ledger** (`todo_app/ledger.py`, migration 14; hub tab **Sync**, `components/hub/SyncTab.tsx`): scheduled per-client syncs that never bring back what you decided.
  - **Refs**: every proposed item from a source can carry a `ref` (`gmail:<threadId>`, `gcal:<eventId>[#item-slug]`, `jira:<KEY>`, `slack:<channel>/<ts>`, `teams:<messageId>`). `changes.ref` keeps each proposal of it and your decision; `task_refs` maps ref → task (recorded on apply, by `upsert_external` as `<source>:<id>` plus `jira:<KEY>` for jira-* sources, and backfilled from Jira links at startup).
  - `Review.propose` runs `Ledger.screen` on every item. It **drops** items whose ref is pending, rejected, or whose task was deleted; creates for tracked refs; anything touching a closed task; and ref-less creates that look like (token-set similarity ≥ 0.8) a create you rejected in 90 days or a task closed in 30, same customer. Dropped items come back in `skipped` with a reason. Nothing left → `nothing_new`, no changeset. `note`/`update`/`complete` with a ref find their task. Overrides: `reconsider` (rejected) and `reopen` (closed), each needing a reason; they're flagged in Review ("Brought back" / "Reopens"). Review shows each change's ref chip, and the task sidebar its refs.
  - **Client sync profile** (`customer_sync`): on/off, client-wide markdown `rules` and a default project.
  - **Feeds** (`sync_feeds`, migration 15): what to read. A feed is one query against one source (gmail / calendar / jira / slack / teams): its `filters` (the source's fields in `ledger.SOURCE_FIELDS`, e.g. one JQL), its own `rules`, `enabled`, and its own `cursor` / last run / summary. A client can have any number per source. Overlapping feeds are fine, because refs dedupe. Migration 15 turned each source's old settings and cursor into one feed.
  - **Running it**: one skill, `skills/todo-sync/SKILL.md` (install in `~/.claude/skills/` on the Mac), and one Claude Code routine per client: `/todo-sync <Client>` (or `all`). The skill is a stub; the procedure is `agent_templates/sync.md`, served by `get_sync_guide` and the MCP prompt `sync`, so it always matches the app.
  - MCP: `check_refs`, `get_client_sync`, `set_client_sync`, `save_sync_feed`, `remove_sync_feed`, `set_sync_state` (per feed: name or id, or the source when there's only one feed of it), `attach_task_file`, `get_sync_guide`; `ChangeItem` has `ref` / `reconsider` / `reopen`. REST: `GET`/`PUT /api/customers/{id}/sync`, `POST /api/customers/{id}/sync/feeds`, `PATCH`/`DELETE /api/customers/{id}/sync/feeds/{feed_id}`.
- **Today page** (`pages/TodayPage.tsx`, `components/today/`):
  - **Your list**, which accepts drops from the side pane. A **Group** control (None / Customer / Project / Status / Priority / Due; `lib/grouping.ts`, remembered per browser; Customer falls back to Project in Personal focus) splits the list and the Needs attention pane alike. Dragging reorders within a group and keeps everything else in its place in the overall order.
  - **Calendar** (`components/today/Calendar.tsx`, at the bottom), with a Day / Week toggle (remembered per browser, default Day) and prev/next stepping. Data comes from `GET /api/meetings?start&end` (scheduled and held, not cancelled); blocks are tinted by customer (`.cal-block`).
    - **Day** (`DayTrack`): a horizontal track (8a–6p, stretched to fit meetings) whose accent fill shows how far through the day you are, with a now marker. Meetings sit in rows above it (drawn at least ~1.5h wide so labels read; past ones faded, the current one ringed), and a status line says what you're in or what's next ("Next: 2:00 Globex in 20m").
    - **Week**: Mon–Fri columns (weekends only when something's on them) on a fixed 240px grid with side-by-side lanes for overlaps, each day's due count, and a now line.
  - **Needs attention** pane (collapsible rail, remembered per browser): your overdue tasks, due in 7 days, waiting, and follow-ups on delegated tasks (`list_tasks(delegated=True)`). Each has a sun button to pull it onto today. Grouped, the pane drops those fixed sections and groups everything the same way as the list (overdue first, then by due date).
- **Board** (All tasks + project pages, `frontend/src/components/Board.tsx`): columns To do · In progress · Waiting · Done (last 14 days). Inbox stays on its own page. Drag a card to another column to change its status, or up and down a column to rank it (`POST /api/tasks/{id}/move` with the column's full order; `tasks.board_rank`, migration 7). The drop line shows where it lands; dropping in a lane's empty space puts it at the end of that lane. Ranked tasks come first in list order, and new (unranked) tasks after them. Done stays newest first. This is separate from `sort_key`, which orders Today and the bar. All tasks swimlanes by customer (default), by project, or not at all. The catch-all lanes are "No customer" (work) and "Personal". Board/list, grouping and collapsed lanes are kept per browser in localStorage.
- **Focus** (work / personal / everything): a per-device mode, so the Omarchy bar flipped to Personal at night never changes what the Mac's browser shows at work.
  - Web app: `frontend/src/lib/focus.tsx` (localStorage `todo-focus`; `?focus=` links set it). Every list query hook sends `area`, so the other side is never fetched. Switching drops the whole query cache. Customers pages are work-only.
  - Bar: `~/.config/todo/bar-state.json` holds the focus plus an optional customer or project filter. `/api/bar?area=&project_id=&customer_id=` returns tasks, counts, today's meetings and filter options for that scope. "Open todo" passes `?focus=`.
  - Quick add takes the caller's `area` / `project_id` as defaults unless the text names its own `@area` / `#project`.
  - Proposals get an `area` when created (from the customer, the target project or the task; `NULL` when mixed), and review counts follow the focus. MCP is unscoped: one token sees everything (agents get run-scoped tokens on `/mcp/agent` instead).
- **Task page** (`/tasks/:id`, `pages/TaskPage.tsx`): every task opens here (old `?task=` links redirect).
  - **Notebook** (`todo_app/notebook.py`): ordered markdown blocks in the shared `blocks` table (`task_id` or `idea_id`). On tasks a block can be a **subtask** (`kind = 'subtask'`, `done`, `done_at`).
  - Adding, completing, reopening and removing subtasks writes task history. Subtask counts (`subtasks_total` / `subtasks_done`) appear on rows, board cards and the page header. Blocks are part of task search.
  - **Timeline** (`todo_app/timeline.py`, `components/task/Timeline.tsx`): a horizontal track at the bottom of the page built from history (each `task_updates.event` is created / note / status / completed / cancelled / today / change / subtask_*) plus linked meetings.
    - A note from a meeting carries that meeting's id. A linked meeting with no history entry gets its own marker.
    - Markers are spaced by a blend of time and order; overlapping ones move up a row.
  - Claude changes subtasks only by proposal: `add_subtask` and `check_subtask`.
- **People** (`todo_app/people.py`, `/people`, `/people/:id`):
  - A person has a name, email, role, an optional employer (`customer_id`; NULL means "our side"), and an area.
  - Tasks have one `assignee_id` (NULL means the user) and any number of **followers** (`task_people`): people the task concerns, for example to discuss in a 1:1. The task stays the assignee's.
  - **Today, the bar and the today/in-progress counts show only the user's own tasks** (`assignee_id IS NULL`). A task assigned to someone lives on their page instead.
  - `@mentions` in a task's description, blocks or progress notes add followers (`Store.follow_mentions`; it never removes anyone). They're stored as `@[Name](#person-ID)` and render as links to the person.
  - Changes are logged as `assigned` / `followed` history events.
  - Quick add: `+name` assigns and `@name` follows (`@work` / `@personal` still set the area). Names match by email, full name, or an unambiguous first name or prefix.
  - The person page (`People.view`) is built for 1:1s:
    - **Follow up**: overdue, due within 7 days, or waiting on them.
    - **On them**: their tasks, grouped by customer and project.
    - **Following**: tasks they follow (things to discuss).
    - **Done in 30 days**.
    - **Across**: the customers and projects you share.
    - **Meetings**: attendee text matching their name or email.
    - **1:1 notes**: a notebook; blocks carry `person_id`.
  - MCP writes the directory and 1:1 notes directly. Assignment and followers are proposals (`update` with `assignee`; action `follow`).
- **Autocomplete** (`components/autocomplete/`): a popup at the caret (`lib/caret.ts`).
  - Quick add suggests `#project`, `@person` or area, `+assignee`, `^date` and `!flag`.
  - Task text fields (description, notebook blocks, full-screen block editor, Log progress) suggest `@mentions`.
  - Arrow keys move through suggestions, Enter or Tab picks, Esc closes. The caret is restored in the same commit as the inserted text, so fast typing can't land in the wrong place.
- **Ideas** (`todo_app/ideas.py`, `/ideas`): the not-yet-tasks backlog.
  - An idea is a **notebook**: a title, a short `summary`, and ordered markdown blocks (shared `blocks` table, notes only). `position` is a float so inserts land between. Each block is a small document for one part of the idea.
  - Page `/ideas/:id` (`IdeaPage.tsx` + `components/notebook/`, shared with tasks): click a block to edit; Esc finishes; Shift+Enter moves to the next block (or creates one). Blocks can be inserted between, moved, collapsed, deleted, or opened full-screen (`BlockFocus`, editor + live preview). Edits autosave (`autosave.tsx`) and are applied optimistically to the cached idea.
  - An idea sits under a customer (even with no project yet), a project, or neither, and follows the focus.
  - Ideas never appear on boards, Today, the bar or the inbox.
  - Capture with `!idea` in any quick add (bar included), the Ideas page, a hub's Ideas tab, or a project page.
  - **Promote** creates a task from the idea (its notes are the summary plus each block as a `##` section, from `Ideas.as_markdown`) and marks the idea `promoted` with a link to the task. **Drop** keeps it, so it stays searchable. Search covers titles, summaries and blocks.
  - MCP writes ideas directly: `capture_idea` (with optional starting blocks), `update_idea`, `add_idea_block`, and `update_idea_block` (`append` is preferred over rewriting the user's blocks). Claude can't delete blocks. Promotion is a proposal (`promote_idea`).
  - Migration 6 also rebuilds `changes` to allow the new action, since CHECK constraints can't be altered in place.
- **Cadences** (`todo_app/cadences.py`, `schedule.py`, `attachments.py`, migration 9; hub tab **Cadences**, `/cadences/:id`, `/prep/:occurrenceId`): a customer's recurring meetings and their prep. The app is storage plus scheduling. A Claude Code skill does the prep over MCP.
  - **Cadence** (the template): `schedule` JSON, which is one of:
    - `{"cron": "0 9 * * 4"}` (5-field, local TZ);
    - `{"nth": 2, "weekday": 1, "time": "10:00"}` (nth weekday of the month, weekday 0 = Mon);
    - `{"calendar": "<title text>"}` (use the customer's synced meetings whose title matches).

    It also has a purpose (markdown), agenda topics (title + guidance), `prep_days`, duration and project. **Steps** (`cadence_steps`) each have a title, instructions, an optional desktop tool (`link_id`), `due_hours_before` and `outputs` (paths/globs, one per line).
  - **Occurrence** (one meeting): made by `Cadences.ensure()` once the meeting is within `prep_days`. That runs hourly from the app lifespan (`cadence_loop`, span `cadence.ensure`), and after cadence edits. "Prep early" or `prepare()` makes one sooner. Each occurrence gets:
    - a scheduled `meetings` row (rule-based cadences; `source='cadence'`, `calendar_id` NULL so calendar sync never cancels it), or a link to the matched synced meeting;
    - a prep **group task** "Prep: <name> · <date>" (`created_via='cadence'`) with one child task per step, due `due_hours_before` the meeting. Each task is keyed `source "cadence: <name>"`, `external_id "occ<id>-step<id>"`;
    - talking points per agenda topic (`occurrence_topics`), notes, and files.

    Past occurrences become `held`. One whose calendar meeting was cancelled becomes `skipped`.
  - Active task queues show only the prep parent. Generated step tasks stay available inside
    the parent and prep packet. Unfinished prep disappears from active lists and counts at
    the linked meeting's current start time (or the occurrence start without a meeting),
    and for held/skipped occurrences or held/cancelled meetings. This is a read-time filter,
    not deletion or completion: task detail, prep packets, full search, and completed history
    retain the records. The open web task lists refresh every 30 seconds. Ordinary meeting
    follow-up tasks and manually grouped tasks keep their existing behavior.
  - Tasks and meetings carry `occurrence_id`, so prep tasks and the meeting dialog link to the prep page.
  - **Files** (`attachments`): stored in `<data>/files/<sha256>`, 50 MB max. `GET /api/files/{id}/{name}` serves images, PDF, txt, csv and md inline; everything else downloads as octet-stream with nosniff. Uploads come from:
    - the page (drop or Attach);
    - `POST /api/occurrences/{id}/files?name=&step_id=` (raw body);
    - MCP `attach_file`;
    - todo-agent, after a step's tool run (uploads files matching the step's `outputs` that the run wrote; runs get `TODO_OCCURRENCE_ID` / `TODO_STEP_ID`), or `todo-agent upload <occurrence> <file>… [--step N]`.
  - **MCP** (direct writes): `list_cadences`, `get_cadence`, `save_cadence`, `get_meeting_prep` (the packet; makes the next one if needed), `set_talking_points`, `update_meeting_notes`, `set_meeting_prep_status`, `complete_prep_step`, `attach_file`, `read_file`. `complete_prep_step` checks off cadence prep tasks directly. That's the one exception to "task changes are proposals", because the cadence generated those tasks.
- **Groups** (iOS-folder style; `Store.group_tasks` / `ungroup`, migration 8 `tasks.parent_id` + `created_via`):
  - On a board, hold a card over the middle of another for ~450ms. A translucent folder plate grows behind the target while it sinks into it (`.folder-card[data-merge]`). Drop it and both go into a new parent task that takes the target's place and status. The parent's name is suggested (`group_title`: words the titles share, else their project) and selected for typing over.
  - Dropping onto a group, or onto a task already in one, adds to that group (`.folder-absorb` pulse). Groups can't go inside groups. A quick drop near a card's top or bottom edge still reorders.
  - Boards ask `top_level=true`, so children live only inside their group. The group card previews up to four children plus children done/total.
  - Today and lists still show ordinary children, with a folder tag naming their group.
    Generated cadence prep steps stay inside their prep parent.
  - The task page of a group lists its tasks (check off, open, take out). A child shows "In <group> · Take out".
  - Taking the last task out of a group made by grouping deletes it, unless someone wrote notes or blocks in it. Deleting a group frees its tasks.
  - History events `grouped`. Undo is in the toast.
- **Images** (`todo_app/uploads.py`, `lib/useImagePaste.ts`): paste or drop screenshots into the task description, notebook blocks (tasks, ideas, 1:1 notes), the full-screen block editor, or Log progress. A block's toolbar also has an image picker. `POST /api/uploads` (raw body, PNG/JPEG/GIF/WebP, max 15 MB, no SVG) stores the file in `<data>/uploads/` under its content hash, and `GET /api/uploads/<name>` serves it, cached forever. The text gets `![screenshot](/api/uploads/…)`, with a placeholder while it uploads.
  - Blocks render images inline (`Markdown`, click for full size).
  - The description and timeline notes are plain text, so their images show as an `ImageStrip` of thumbnails. In the description box the image references are kept out of the text (`wordsOf` / `withImages`, always at the end), and each thumbnail has a remove button.
  - Files aren't deleted when a reference is removed (they're deduplicated and small); clean `<data>/uploads/` by hand if it ever matters.
- **Search**: FTS5 over title, notes and history (`tasks_fts`, rowid = task id, maintained by `Store._reindex`). Words are OR'd and prefix-matched, bm25 weights title > notes > history, and it includes closed tasks so Claude can tell "already done" from "new".
- **External items**: `(source, external_id)` is unique. `upsert_external` only changes the fields passed. A closed status always applies, but an open one only reopens, so a sync never undoes "in progress".

## Critical files

- [main.py](main.py): entrypoint (`telemetry.setup` first, then uvicorn)
- [todo_app/store.py](todo_app/store.py): all domain logic and history
- [todo_app/db.py](todo_app/db.py): schema + append-only migrations (`PRAGMA user_version`)
- [todo_app/api.py](todo_app/api.py): REST, incl. `/api/bar` (widget) and `/api/ingest` (scripts, token)
- [todo_app/hub.py](todo_app/hub.py): customer hub (meetings, calendar sync, topics, links/launchers, logos)
- [todo_app/review.py](todo_app/review.py): proposals: validation, diff rendering, apply/reject
- [todo_app/mcp_server.py](todo_app/mcp_server.py): MCP tools + `INSTRUCTIONS` (the trust model), bearer-token wrapper
- [todo_app/agents.py](todo_app/agents.py) + [todo_app/agent_dist/](todo_app/agent_dist/): machines, the run queue, and the todo-agent script + installer the app serves
- [todo_app/dispatch.py](todo_app/dispatch.py) + [todo_app/mcp_worker.py](todo_app/mcp_worker.py): agent assignees (task runs, run tokens, the `/mcp/agent` tools, setup checks)
- [todo_app/quickadd.py](todo_app/quickadd.py): `#project @area !today !now !high ^fri` syntax
- [todo_app/telemetry.py](todo_app/telemetry.py): JSON logs + OTel (copied from underground-bot)
- [frontend/](frontend/): React + Vite + Tailwind 3 + TanStack Query; tokens in `src/index.css` (house style)
- [desktop/](desktop/): omarchy-shell bar widget `dev.todo` + `install.sh` (SUPER+ALT+T quick add)
- [skills/todo-triage/SKILL.md](skills/todo-triage/SKILL.md): the Claude skill for meeting notes → tasks
- [scripts/example-sync.py](scripts/example-sync.py): template for customer-source sync scripts

## Environment

- `API_TOKEN`: bearer token required on `/mcp` and `/api/ingest`. Unset = open. Always set it in prod.
- `TODO_DB` (default `data/todo.db`; `/data/todo.db` in the image), `TODO_PORT` (default 7670)
- `TZ`: decides what "today" is. Set it on the stack.
- `OTEL_*`: standard OpenTelemetry vars (see Observability)

The web UI and the rest of `/api` are open on the LAN, like the user's other NAS apps.

## Development workflow (for Claude Code)

1. **Check**: `.venv/bin/python -m pytest -q` and `cd frontend && npx tsc --noEmit`
2. **Run locally**: `run-local` skill (below)
3. **Open a PR**: `open-pr` skill, once the user likes it
4. **Merge**: the user merges (or asks Claude to)
5. **Deploy**: `deploy-nas` skill

Never commit straight to `main`, and never merge or redeploy production without the user asking.

### Local run
- Compose: `docker compose -f docker-compose.dev.yml up -d --build` → http://localhost:7670, health `/health` (`{"status":"ok"}`)
- Data in `.local-data/` (git-ignored). Local `API_TOKEN` is `local-token`.
- Sample data: `.venv/bin/python scripts/seed-sample-data.py http://localhost:7670 local-token` (into an empty DB), then `scripts/seed-hub-demo.py`, which drives MCP like Claude: recap with topic updates, calendar sync with prep, and a pending proposal
- Dev server without Docker: `TODO_DB=/tmp/todo.db .venv/bin/python main.py` + `cd frontend && npm run dev` (Vite proxies `/api` to :7670)
- Rootless Docker runs the container's `python main.py` as your uid: **never `pkill -f main.py`**, it kills the container too

### Production deploy
- Build: `.github/workflows/publish-container.yml` on push to `main` → `ghcr.io/dev7117/todo:latest` + `sha-<commit>` (image named `todo`, not after the repo)
- Portainer: http://192.168.1.47:9000, stack **`todo`** (endpoint 2), web-editor stack from [portainer-stack.yml](portainer-stack.yml); stack env `API_TOKEN`
- Production: http://192.168.1.47:7670, health `/health`. Data in `/volume1/docker/todo/data/todo.db`

### Observability
- Logs: JSON on stdout → Loki (`service_name="todo"`, `stack="todo"`)
- Traces: `service.name = todo-web`. Spans:
  - FastAPI requests
  - `tools/call <tool>` → `mcp.tool_call` (MCP SDK + ours)
  - `proposal.create` (source, count, meeting) and `proposal.decide` (resulting status)
  - `calendar.sync` (created/updated/cancelled counts)
  - `customer.logo_fetch`
  - `ingest.batch` (source, created/updated/closed) `/health` and `/api/bar` (polled by the widget) aren't traced.
- `obs traces '{ resource.service.name = "todo-web" && name = "proposal.create" }' --since 1d`
- **Ledger / client sync** (logger `todo.ledger`). Every filtered item is a `ledger skipped` line with `code`, `ref`, `action`, `title`, `customer`, `proposal_source` and `why`. Codes: `pending`, `rejected`, `deleted`, `tracked`, `closed`, `looks_rejected`, `looks_closed`, `needs_reason`. Also logged:
  - a `ledger override` line for each item let back in (`event` is `brought_back` or `reopens`, with its reason);
  - a `ledger screened` summary per proposal (`kept`, `skipped`, `skipped_<code>`);
  - a `sync run` line per `set_sync_state` (`customer`, `source`, `feed`, `feed_id`, `cursor`, `summary`).

  `proposal.create` spans carry `ledger.kept`, `ledger.skipped` and `ledger.skipped.<code>`, plus a `ledger.skipped` event per item.
  - Everything filtered: `{service_name="todo"} | json | logger="todo.ledger" | message="ledger skipped"`
  - One client: add `| customer="Acme Corp"`. Per run: `| message="sync run"`.

### Desktop
- `desktop/install.sh [URL]` installs todo-agent (via the served installer; skip with `TODO_SKIP_AGENT=1`) and the widget to `~/.config/omarchy/plugins/dev.todo`, writes `~/.config/todo/bar.json` (`{"url": …}`), adds `dev.todo` after `dev.mediadeck` in `~/.config/omarchy/shell.json`, writes `~/.config/hypr/todo.lua` (SUPER+ALT+T → `omarchy-shell dev.todo add ""`), and restarts the shell. Undo it with `--uninstall`.
- After editing the QML, re-run the installer (or copy it and run `omarchy restart shell`). Never run `omarchy-shell shell rescanPlugins`; it has crashed the shell.
- The widget polls `GET /api/bar` every 30s and on open, and writes via `POST /api/tasks/quick` / `PATCH /api/tasks/{id}`.

### MCP
- Register in Claude Code: `claude mcp add --transport http --scope user todo http://192.168.1.47:7670/mcp --header "Authorization: Bearer <API_TOKEN>"`
- Stateless streamable HTTP, JSON responses. DNS-rebinding protection is off because it's reached by LAN IP; the token guards it.
- Keep the trust model: tools may write hub content, but task changes only go through `review.propose`. The exceptions are narrow and scoped: `complete_prep_step`, and the `/mcp/agent` tools on the run's own task (never done).
- Add a tool: an `async def` decorated `@_tool(READ|WRITE)` in `mcp_server.py` that calls `store`. Raise `Invalid` / `NotFound` for user-fixable errors (they become tool errors). Update `INSTRUCTIONS` and the skill if the workflow changes.
