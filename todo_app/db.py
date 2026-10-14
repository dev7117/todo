"""SQLite connection and schema migrations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Each entry runs once, in order; PRAGMA user_version records how many have run.
# Never edit a shipped migration; append a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE projects (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        area        TEXT NOT NULL CHECK (area IN ('work', 'personal')),
        customer    TEXT,
        description TEXT NOT NULL DEFAULT '',
        archived    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE UNIQUE INDEX projects_name ON projects (lower(name));

    CREATE TABLE tasks (
        id           INTEGER PRIMARY KEY,
        title        TEXT NOT NULL,
        notes        TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'todo'
                     CHECK (status IN ('inbox', 'todo', 'in_progress', 'waiting', 'done', 'cancelled')),
        area         TEXT NOT NULL CHECK (area IN ('work', 'personal')),
        project_id   INTEGER REFERENCES projects (id) ON DELETE SET NULL,
        priority     INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 3),
        due_on       TEXT,
        today_on     TEXT,
        waiting_on   TEXT,
        source       TEXT NOT NULL DEFAULT 'app',
        external_id  TEXT,
        external_url TEXT,
        sort_key     REAL NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        completed_at TEXT
    );
    CREATE UNIQUE INDEX tasks_external ON tasks (source, external_id) WHERE external_id IS NOT NULL;
    CREATE INDEX tasks_status ON tasks (status);
    CREATE INDEX tasks_project ON tasks (project_id);

    CREATE TABLE task_updates (
        id         INTEGER PRIMARY KEY,
        task_id    INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
        kind       TEXT NOT NULL CHECK (kind IN ('created', 'change', 'note')),
        body       TEXT NOT NULL DEFAULT '',
        source     TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX task_updates_task ON task_updates (task_id, id);

    -- rowid = tasks.id; maintained by Store._reindex.
    CREATE VIRTUAL TABLE tasks_fts USING fts5 (title, notes, updates, tokenize = 'porter unicode61');

    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # 2: customers become records (projects.customer text → customers + projects.customer_id).
    """
    CREATE TABLE customers (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        notes      TEXT NOT NULL DEFAULT '',
        archived   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX customers_name ON customers (lower(name));
    INSERT INTO customers (name, created_at, updated_at)
        SELECT trim(customer), MIN(created_at), MIN(created_at) FROM projects
        WHERE customer IS NOT NULL AND trim(customer) <> ''
        GROUP BY lower(trim(customer));
    ALTER TABLE projects ADD COLUMN customer_id INTEGER REFERENCES customers (id) ON DELETE SET NULL;
    UPDATE projects SET customer_id =
        (SELECT c.id FROM customers c WHERE lower(c.name) = lower(trim(projects.customer)));
    ALTER TABLE projects DROP COLUMN customer;
    CREATE INDEX projects_customer ON projects (customer_id);
    """,
    # 3: customer hub: profile, overview, meetings (+ the tasks they produced), topics, links/launchers.
    """
    ALTER TABLE customers ADD COLUMN website TEXT;
    ALTER TABLE customers ADD COLUMN logo TEXT;
    ALTER TABLE customers ADD COLUMN overview TEXT NOT NULL DEFAULT '';
    ALTER TABLE customers ADD COLUMN overview_source TEXT;
    ALTER TABLE customers ADD COLUMN overview_updated_at TEXT;

    -- A meeting is either upcoming (status 'scheduled', synced from the calendar, with prep
    -- notes) or a recap ('held'). Logging a recap for a calendar event upgrades the same row.
    CREATE TABLE meetings (
        id           INTEGER PRIMARY KEY,
        customer_id  INTEGER NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
        project_id   INTEGER REFERENCES projects (id) ON DELETE SET NULL,
        status       TEXT NOT NULL DEFAULT 'held' CHECK (status IN ('scheduled', 'held', 'cancelled')),
        title        TEXT NOT NULL,
        held_on      TEXT NOT NULL,
        starts_at    TEXT,
        ends_at      TEXT,
        calendar_id  TEXT UNIQUE,
        location     TEXT,
        attendees    TEXT NOT NULL DEFAULT '',
        prep         TEXT NOT NULL DEFAULT '',
        summary      TEXT NOT NULL DEFAULT '',
        decisions    TEXT NOT NULL DEFAULT '',
        external_url TEXT,
        source       TEXT NOT NULL DEFAULT 'app',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX meetings_customer ON meetings (customer_id, held_on);
    CREATE INDEX meetings_upcoming ON meetings (status, starts_at);

    -- Claude's proposed task changes, reviewed in the app before they apply.
    CREATE TABLE changesets (
        id          INTEGER PRIMARY KEY,
        source      TEXT NOT NULL,
        summary     TEXT NOT NULL DEFAULT '',
        meeting_id  INTEGER REFERENCES meetings (id) ON DELETE SET NULL,
        customer_id INTEGER REFERENCES customers (id) ON DELETE SET NULL,
        status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'partial', 'rejected')),
        created_by  TEXT NOT NULL DEFAULT 'mcp',
        created_at  TEXT NOT NULL,
        decided_at  TEXT
    );
    CREATE INDEX changesets_status ON changesets (status, id);

    CREATE TABLE changes (
        id           INTEGER PRIMARY KEY,
        changeset_id INTEGER NOT NULL REFERENCES changesets (id) ON DELETE CASCADE,
        seq          INTEGER NOT NULL,
        action       TEXT NOT NULL CHECK (action IN ('create', 'update', 'note', 'complete', 'add_link')),
        task_id      INTEGER REFERENCES tasks (id) ON DELETE SET NULL,
        payload      TEXT NOT NULL,           -- JSON: the proposed fields / note / link
        before       TEXT,                    -- JSON: the task as it was when proposed
        reason       TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'applied', 'rejected', 'failed')),
        result_id    INTEGER,                 -- task (or link) id once applied
        error        TEXT
    );
    CREATE INDEX changes_set ON changes (changeset_id, seq);

    CREATE TABLE meeting_tasks (
        meeting_id INTEGER NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
        task_id    INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
        action     TEXT NOT NULL,
        PRIMARY KEY (meeting_id, task_id)
    );
    CREATE INDEX meeting_tasks_task ON meeting_tasks (task_id);

    CREATE TABLE customer_topics (
        id                INTEGER PRIMARY KEY,
        customer_id       INTEGER NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
        name              TEXT NOT NULL,
        summary           TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'watching', 'resolved')),
        mentions          INTEGER NOT NULL DEFAULT 1,
        last_mentioned_on TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );
    CREATE UNIQUE INDEX customer_topics_name ON customer_topics (customer_id, lower(name));

    CREATE TABLE customer_links (
        id          INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK (kind IN ('link', 'launcher')),
        label       TEXT NOT NULL,
        url         TEXT,
        command     TEXT,
        cwd         TEXT,
        mode        TEXT NOT NULL DEFAULT 'terminal' CHECK (mode IN ('terminal', 'background')),
        sort_key    REAL NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX customer_links_customer ON customer_links (customer_id, sort_key);
    """,
    # 4: desktop agents (todo-agent on the Mac / Omarchy) and the launcher runs they execute.
    """
    ALTER TABLE customer_links ADD COLUMN agent TEXT;  -- preferred machine; NULL = pick when running

    CREATE TABLE agents (
        name       TEXT PRIMARY KEY,
        platform   TEXT NOT NULL,
        version    TEXT NOT NULL DEFAULT '',
        last_seen  TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE launcher_runs (
        id           INTEGER PRIMARY KEY,
        link_id      INTEGER REFERENCES customer_links (id) ON DELETE SET NULL,
        customer_id  INTEGER REFERENCES customers (id) ON DELETE SET NULL,
        agent        TEXT NOT NULL,
        label        TEXT NOT NULL,
        command      TEXT NOT NULL,
        cwd          TEXT,
        mode         TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                     ('queued', 'claimed', 'running', 'succeeded', 'failed', 'declined', 'expired', 'cancelled')),
        exit_code    INTEGER,
        output       TEXT NOT NULL DEFAULT '',
        error        TEXT,
        requested_at TEXT NOT NULL,
        claimed_at   TEXT,
        started_at   TEXT,
        finished_at  TEXT
    );
    CREATE INDEX launcher_runs_queue ON launcher_runs (agent, status, id);
    CREATE INDEX launcher_runs_link ON launcher_runs (link_id, id);
    """,
    # 5: focus modes: a proposal belongs to work or personal (NULL = mixed, shown in both).
    """
    ALTER TABLE changesets ADD COLUMN area TEXT;
    UPDATE changesets SET area = 'work' WHERE customer_id IS NOT NULL;
    """,
    # 6: ideas, the not-yet-tasks backlog. Never on boards / today / the bar; promoted into a task.
    """
    CREATE TABLE ideas (
        id          INTEGER PRIMARY KEY,
        title       TEXT NOT NULL,
        summary     TEXT NOT NULL DEFAULT '',   -- a line or two; the body lives in idea_blocks
        area        TEXT NOT NULL CHECK (area IN ('work', 'personal')),
        customer_id INTEGER REFERENCES customers (id) ON DELETE SET NULL,
        project_id  INTEGER REFERENCES projects (id) ON DELETE SET NULL,
        status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'promoted', 'dropped')),
        task_id     INTEGER REFERENCES tasks (id) ON DELETE SET NULL,
        source      TEXT NOT NULL DEFAULT 'app',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        decided_at  TEXT
    );
    CREATE INDEX ideas_scope ON ideas (status, area, customer_id, project_id);

    -- Notebooks: ordered markdown blocks, each a small document of its own, belonging to an
    -- idea or a task. On tasks a block can be a subtask (kind 'subtask', with done / done_at).
    -- People you work with: assign tasks to them, have them follow tasks, keep 1:1 notes on them.
    CREATE TABLE people (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        email       TEXT,
        title       TEXT NOT NULL DEFAULT '',
        customer_id INTEGER REFERENCES customers (id) ON DELETE SET NULL,  -- their employer; NULL = your side
        area        TEXT NOT NULL DEFAULT 'work' CHECK (area IN ('work', 'personal')),
        notes       TEXT NOT NULL DEFAULT '',
        archived    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE UNIQUE INDEX people_email ON people (lower(email)) WHERE email IS NOT NULL;
    ALTER TABLE tasks ADD COLUMN assignee_id INTEGER REFERENCES people (id) ON DELETE SET NULL;  -- NULL = you
    CREATE INDEX tasks_assignee ON tasks (assignee_id);
    -- Followers: people a task concerns (to discuss, to keep in the loop). The task stays the
    -- assignee's; followers see it on their page. @mentions in a task's text add followers.
    CREATE TABLE task_people (
        task_id   INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
        person_id INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
        PRIMARY KEY (task_id, person_id)
    );
    CREATE INDEX task_people_person ON task_people (person_id);

    CREATE TABLE blocks (
        id         INTEGER PRIMARY KEY,
        idea_id    INTEGER REFERENCES ideas (id) ON DELETE CASCADE,
        task_id    INTEGER REFERENCES tasks (id) ON DELETE CASCADE,
        person_id  INTEGER REFERENCES people (id) ON DELETE CASCADE,   -- 1:1 notes
        position   REAL NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'note' CHECK (kind IN ('note', 'subtask')),
        title      TEXT NOT NULL DEFAULT '',
        body       TEXT NOT NULL DEFAULT '',
        collapsed  INTEGER NOT NULL DEFAULT 0,
        done       INTEGER NOT NULL DEFAULT 0,
        done_at    TEXT,
        source     TEXT NOT NULL DEFAULT 'app',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK ((idea_id IS NOT NULL) + (task_id IS NOT NULL) + (person_id IS NOT NULL) = 1)
    );
    CREATE INDEX blocks_idea ON blocks (idea_id, position);
    CREATE INDEX blocks_task ON blocks (task_id, position);
    CREATE INDEX blocks_person ON blocks (person_id, position);

    -- What a history entry was about, for the task timeline (status, today, subtask_done…).
    ALTER TABLE task_updates ADD COLUMN event TEXT;

    -- promote_idea joins the proposal actions (CHECK constraints can't be altered in place).
    CREATE TABLE changes_new (
        id           INTEGER PRIMARY KEY,
        changeset_id INTEGER NOT NULL REFERENCES changesets (id) ON DELETE CASCADE,
        seq          INTEGER NOT NULL,
        action       TEXT NOT NULL CHECK (action IN ('create', 'update', 'note', 'complete', 'add_link', 'promote_idea',
                                                     'add_subtask', 'check_subtask', 'follow')),
        task_id      INTEGER REFERENCES tasks (id) ON DELETE SET NULL,
        payload      TEXT NOT NULL,
        before       TEXT,
        reason       TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'applied', 'rejected', 'failed')),
        result_id    INTEGER,
        error        TEXT
    );
    INSERT INTO changes_new SELECT * FROM changes;
    DROP TABLE changes;
    ALTER TABLE changes_new RENAME TO changes;
    CREATE INDEX changes_set ON changes (changeset_id, seq);
    """,
    # 7: board order. Dragging a card up or down a column ranks that column; unranked tasks
    # (new ones) come after the ranked ones. Separate from sort_key, which orders today.
    """
    ALTER TABLE tasks ADD COLUMN board_rank REAL;
    """,
    # 8: task groups. Drag one task onto another (like an iOS folder) and both become children
    # of a new parent task; drop more onto the parent to add them. Boards show only top-level tasks.
    """
    ALTER TABLE tasks ADD COLUMN parent_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL;
    ALTER TABLE tasks ADD COLUMN created_via TEXT;  -- 'group' for parents made by grouping
    CREATE INDEX tasks_parent ON tasks(parent_id);
    """,
    # 9: cadences: a customer's recurring meetings (weekly ops, monthly reviews) and how to prep
    # them. The cadence is the template: schedule, agenda topics, prep steps (each optionally a
    # desktop tool, with the files it produces). Each occurrence is one meeting: its prep steps as
    # real tasks (a group), talking points per agenda topic, notes and attached files. A Claude
    # Code skill reads and fills these over MCP; the app stores, schedules and shows them.
    """
    CREATE TABLE cadences (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        purpose TEXT NOT NULL DEFAULT '',
        schedule TEXT NOT NULL,
        duration_min INTEGER NOT NULL DEFAULT 60,
        prep_days INTEGER NOT NULL DEFAULT 3,
        agenda TEXT NOT NULL DEFAULT '[]',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX cadences_customer ON cadences(customer_id);
    CREATE TABLE cadence_steps (
        id INTEGER PRIMARY KEY,
        cadence_id INTEGER NOT NULL REFERENCES cadences(id) ON DELETE CASCADE,
        position REAL NOT NULL,
        title TEXT NOT NULL,
        instructions TEXT NOT NULL DEFAULT '',
        link_id INTEGER REFERENCES customer_links(id) ON DELETE SET NULL,
        due_hours_before INTEGER NOT NULL DEFAULT 24,
        outputs TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX cadence_steps_cadence ON cadence_steps(cadence_id, position);
    CREATE TABLE cadence_occurrences (
        id INTEGER PRIMARY KEY,
        cadence_id INTEGER NOT NULL REFERENCES cadences(id) ON DELETE CASCADE,
        meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
        held_on TEXT NOT NULL,
        starts_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'upcoming' CHECK (status IN ('upcoming', 'ready', 'held', 'skipped')),
        prep_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (cadence_id, starts_at)
    );
    CREATE INDEX cadence_occurrences_when ON cadence_occurrences(held_on);
    CREATE TABLE occurrence_topics (
        id INTEGER PRIMARY KEY,
        occurrence_id INTEGER NOT NULL REFERENCES cadence_occurrences(id) ON DELETE CASCADE,
        position REAL NOT NULL,
        title TEXT NOT NULL,
        guidance TEXT NOT NULL DEFAULT '',
        points TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'app',
        updated_at TEXT NOT NULL
    );
    CREATE INDEX occurrence_topics_occurrence ON occurrence_topics(occurrence_id, position);
    CREATE TABLE attachments (
        id INTEGER PRIMARY KEY,
        occurrence_id INTEGER REFERENCES cadence_occurrences(id) ON DELETE CASCADE,
        step_id INTEGER REFERENCES cadence_steps(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        file TEXT NOT NULL,
        content_type TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'app',
        created_at TEXT NOT NULL
    );
    CREATE INDEX attachments_occurrence ON attachments(occurrence_id, created_at);
    ALTER TABLE launcher_runs ADD COLUMN occurrence_id INTEGER REFERENCES cadence_occurrences(id) ON DELETE SET NULL;
    ALTER TABLE launcher_runs ADD COLUMN step_id INTEGER REFERENCES cadence_steps(id) ON DELETE SET NULL;
    """,
    # 10: topics become the customer's living picture, one card per thread: a "where things
    # stand" (summary, rewritten as things change) plus an append-only timeline of updates, mostly
    # from meetings. Replaces the single overview that every run rewrote. Each topic's current
    # summary seeds its timeline; customers.overview is kept (shown as the earlier overview).
    """
    ALTER TABLE customer_topics ADD COLUMN stand_source TEXT;
    ALTER TABLE customer_topics ADD COLUMN stand_updated_at TEXT;
    CREATE TABLE topic_updates (
        id INTEGER PRIMARY KEY,
        topic_id INTEGER NOT NULL REFERENCES customer_topics(id) ON DELETE CASCADE,
        body TEXT NOT NULL,
        happened_on TEXT NOT NULL,
        meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
        source TEXT NOT NULL DEFAULT 'app',
        created_at TEXT NOT NULL
    );
    CREATE INDEX topic_updates_topic ON topic_updates(topic_id, happened_on);
    INSERT INTO topic_updates (topic_id, body, happened_on, source, created_at)
        SELECT id, summary, COALESCE(last_mentioned_on, substr(updated_at, 1, 10)), 'migrated', updated_at
        FROM customer_topics WHERE trim(summary) <> '';
    UPDATE customer_topics SET stand_source = 'mcp', stand_updated_at = updated_at WHERE trim(summary) <> '';
    """,
    # 11: agent assignees. An agent is a person (kind 'agent') with a profile naming the Claude
    # Code agent file it runs as (.claude/agents/<name>.md in the project's repo). Assigning a
    # task to one queues a task run: todo-agent makes a worktree of the project's repo and runs
    # `claude -p --agent <name>` there, talking back over /mcp/agent with a token that only
    # works for that run. launcher_runs is rebuilt so command can be empty (task runs build
    # theirs on the machine) and gains the task, the agent, the token hash and the session.
    """
    ALTER TABLE people ADD COLUMN kind TEXT NOT NULL DEFAULT 'human' CHECK (kind IN ('human', 'agent'));
    CREATE TABLE agent_profiles (
        person_id     INTEGER PRIMARY KEY REFERENCES people (id) ON DELETE CASCADE,
        claude_agent  TEXT NOT NULL,             -- .claude/agents/<claude_agent>.md in the repo
        machine       TEXT,                      -- preferred todo-agent machine; NULL = the only one online
        model         TEXT,                      -- override the agent file's model
        max_turns     INTEGER,
        allowed_tools TEXT NOT NULL DEFAULT '',  -- claude --allowedTools
        projects      TEXT NOT NULL DEFAULT '[]', -- JSON project ids it may work on; [] = any with a repo
        auto_dispatch INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    ALTER TABLE projects ADD COLUMN repo_path TEXT;
    ALTER TABLE projects ADD COLUMN default_branch TEXT NOT NULL DEFAULT 'main';

    CREATE TABLE launcher_runs_new (
        id            INTEGER PRIMARY KEY,
        kind          TEXT NOT NULL DEFAULT 'tool' CHECK (kind IN ('tool', 'task', 'check')),
        link_id       INTEGER REFERENCES customer_links (id) ON DELETE SET NULL,
        customer_id   INTEGER REFERENCES customers (id) ON DELETE SET NULL,
        agent         TEXT NOT NULL,
        label         TEXT NOT NULL,
        command       TEXT NOT NULL DEFAULT '',
        cwd           TEXT,
        mode          TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                      ('queued', 'claimed', 'running', 'succeeded', 'failed', 'declined', 'expired', 'cancelled')),
        exit_code     INTEGER,
        output        TEXT NOT NULL DEFAULT '',
        error         TEXT,
        requested_at  TEXT NOT NULL,
        claimed_at    TEXT,
        started_at    TEXT,
        finished_at   TEXT,
        occurrence_id INTEGER REFERENCES cadence_occurrences (id) ON DELETE SET NULL,
        step_id       INTEGER REFERENCES cadence_steps (id) ON DELETE SET NULL,
        task_id       INTEGER REFERENCES tasks (id) ON DELETE CASCADE,
        person_id     INTEGER REFERENCES people (id) ON DELETE SET NULL,
        project_id    INTEGER REFERENCES projects (id) ON DELETE SET NULL,
        message       TEXT,      -- the user's reply this run picks up from
        token_hash    TEXT,      -- sha256 of the run's MCP token, issued at claim
        session_id    TEXT,      -- Claude session, for --resume
        branch        TEXT,
        pr_url        TEXT,
        cost_usd      REAL,
        outcome       TEXT       -- review | question | ready (check runs) | NULL
    );
    INSERT INTO launcher_runs_new (id, link_id, customer_id, agent, label, command, cwd, mode, status, exit_code,
        output, error, requested_at, claimed_at, started_at, finished_at, occurrence_id, step_id)
        SELECT id, link_id, customer_id, agent, label, command, cwd, mode, status, exit_code,
        output, error, requested_at, claimed_at, started_at, finished_at, occurrence_id, step_id FROM launcher_runs;
    DROP TABLE launcher_runs;
    ALTER TABLE launcher_runs_new RENAME TO launcher_runs;
    CREATE INDEX launcher_runs_queue ON launcher_runs (agent, status, id);
    CREATE INDEX launcher_runs_link ON launcher_runs (link_id, id);
    CREATE INDEX launcher_runs_task ON launcher_runs (task_id, id);
    CREATE UNIQUE INDEX launcher_runs_token ON launcher_runs (token_hash) WHERE token_hash IS NOT NULL;
    """,
    # 12: files on tasks. What an agent produces (or you drop on a task) is attached to the task,
    # not left in a folder on some machine: the attachments table, which held cadence meeting
    # files, now also belongs to a task.
    """
    ALTER TABLE attachments ADD COLUMN task_id INTEGER REFERENCES tasks (id) ON DELETE CASCADE;
    CREATE INDEX attachments_task ON attachments (task_id, created_at);
    """,
    # 13: agent runs count tokens rather than dollars: on a subscription the API price is
    # notional. tokens = new input (incl. cache writes) + output; cached_tokens = cache reads
    # (the context re-read each turn, kept apart so it doesn't swamp the number). cost_usd stays
    # unused.
    """
    ALTER TABLE launcher_runs ADD COLUMN tokens INTEGER;
    ALTER TABLE launcher_runs ADD COLUMN cached_tokens INTEGER;
    """,
    # 14: the source ledger and client sync (todo_app/ledger.py). Every proposed item can carry a
    # ref (gmail:<thread>, jira:ACME-1, gcal:<event>…): changes.ref remembers each proposal of it
    # and what you decided, task_refs what it became. A scheduled sync then can't bring back what
    # you rejected or closed. customer_sync holds each client's sync profile (sources and their
    # filters, rules); sync_state where each source's last run stopped.
    """
    ALTER TABLE changes ADD COLUMN ref TEXT;
    CREATE INDEX changes_ref ON changes (ref, status) WHERE ref IS NOT NULL;
    CREATE TABLE task_refs (
        task_id    INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
        ref        TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    );
    CREATE INDEX task_refs_task ON task_refs (task_id);
    CREATE TABLE customer_sync (
        customer_id        INTEGER PRIMARY KEY REFERENCES customers (id) ON DELETE CASCADE,
        enabled            INTEGER NOT NULL DEFAULT 1,
        sources            TEXT NOT NULL DEFAULT '{}',   -- JSON: {gmail: {domains: [...]}, jira: {...}, ...}
        rules              TEXT NOT NULL DEFAULT '',     -- markdown: client-specific guidance
        default_project_id INTEGER REFERENCES projects (id) ON DELETE SET NULL,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    );
    CREATE TABLE sync_state (
        customer_id  INTEGER NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
        source       TEXT NOT NULL,
        cursor       TEXT,
        last_run_at  TEXT,
        last_summary TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (customer_id, source)
    );
    INSERT OR IGNORE INTO task_refs (task_id, ref, created_at)
        SELECT id, source || ':' || external_id, created_at FROM tasks WHERE external_id IS NOT NULL;
    """,
    # 15: sync feeds. A client can have several queries per source (two JQLs, a few Slack
    # channels handled differently, separate Gmail searches), each with its own filters, rules
    # and cursor. Each source's old settings and cursor become one feed.
    """
    CREATE TABLE sync_feeds (
        id           INTEGER PRIMARY KEY,
        customer_id  INTEGER NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
        source       TEXT NOT NULL CHECK (source IN ('gmail', 'calendar', 'jira', 'slack', 'teams')),
        name         TEXT NOT NULL,
        filters      TEXT NOT NULL DEFAULT '{}',   -- JSON, the source's fields (ledger.SOURCE_FIELDS)
        rules        TEXT NOT NULL DEFAULT '',     -- markdown, just for this feed
        enabled      INTEGER NOT NULL DEFAULT 1,
        position     REAL NOT NULL DEFAULT 0,
        cursor       TEXT,
        last_run_at  TEXT,
        last_summary TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE UNIQUE INDEX sync_feeds_name ON sync_feeds (customer_id, lower(name));
    INSERT INTO sync_feeds (customer_id, source, name, filters, position, cursor, last_run_at, last_summary,
                            created_at, updated_at)
        SELECT s.customer_id, j.key,
               CASE j.key WHEN 'gmail' THEN 'Gmail' WHEN 'calendar' THEN 'Calendar' WHEN 'jira' THEN 'Jira'
                          WHEN 'slack' THEN 'Slack' ELSE 'Teams' END,
               j.value, 0, st.cursor, st.last_run_at, COALESCE(st.last_summary, ''), s.created_at, s.updated_at
        FROM customer_sync s, json_each(s.sources) j
        LEFT JOIN sync_state st ON st.customer_id = s.customer_id AND st.source = j.key
        WHERE j.type = 'object';
    DROP TABLE sync_state;
    ALTER TABLE customer_sync DROP COLUMN sources;
    """,
    # 16: opt-in interactive Codex assignments and durable launch receipts.
    """
    ALTER TABLE agent_profiles ADD COLUMN executor TEXT NOT NULL DEFAULT 'claude'
        CHECK (executor IN ('claude', 'codex-herdr'));
    ALTER TABLE launcher_runs ADD COLUMN executor TEXT NOT NULL DEFAULT 'claude';
    ALTER TABLE launcher_runs ADD COLUMN launch TEXT NOT NULL DEFAULT '{}';
    """,

    # 17: native app/Remote executor; preserve existing profiles and legacy executor defaults.
    """
    ALTER TABLE agent_profiles RENAME TO agent_profiles_v16;
    CREATE TABLE agent_profiles (
        person_id INTEGER PRIMARY KEY REFERENCES people (id) ON DELETE CASCADE,
        claude_agent TEXT NOT NULL,
        machine TEXT,
        model TEXT,
        max_turns INTEGER,
        allowed_tools TEXT NOT NULL DEFAULT '',
        projects TEXT NOT NULL DEFAULT '[]',
        auto_dispatch INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        executor TEXT NOT NULL DEFAULT 'claude' CHECK (executor IN ('claude', 'codex-herdr', 'codex-app'))
    );
    INSERT INTO agent_profiles SELECT * FROM agent_profiles_v16;
    DROP TABLE agent_profiles_v16;
    """,

]


def connect(path: Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {index}; COMMIT;")
