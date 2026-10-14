"""MCP server: the todo list and customer hubs as tools for Claude, served at /mcp.
(Agents working a task use the separate, run-scoped /mcp/agent: mcp_worker.py.)

Trust model: Claude reads everything and writes hub content directly (meeting recaps,
upcoming meetings from the calendar, prep notes, topic updates, customers/projects),
but every change to tasks, and every customer link, is a *proposal*. The user reviews it as
a diff in the app (Review page) and approves item by item. The setting
review_claude_changes (on by default) controls this; off, proposals apply at once.

Tools are thin adapters over Store / Hub / Review, the same code the web UI uses.
"""
from __future__ import annotations

import base64
import functools
import hmac
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from opentelemetry import trace
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from . import telemetry
from .deps import agents, attachments, cadences, cfg, dispatch, hub, ideas, ledger, notebook, people, review, store
from .dispatch import supports_tasks
from .review import diff_text
from .store import PRIORITY_LABELS, Invalid, NotFound

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])
tracer = trace.get_tracer("todo.mcp")

INSTRUCTIONS = """\
The user's todo list and customer hubs (personal + work). It feeds their desktop bar (today and
in-progress tasks) and gets tasks from the app, a hotkey intake, customer sync scripts, and you.

Model
- Area: "work" or "personal". Customers own projects; projects own tasks. The user plans work
  customer by customer, so file customer work under the right customer's project.
- Task status: inbox → todo → in_progress → waiting (waiting_on says who) → done / cancelled.
  today = "doing it today"; the user curates today and in_progress, so don't set them unless asked.
- Every task has a history (created, field changes, progress notes), each tagged with its source.

You change tasks by proposing
- propose_changes records a batch (create / update / note / complete / add_link) for the user to
  review as a diff and approve in the app. Nothing changes until they do. Give each item a short
  `reason` (the line from the notes that justifies it) and the batch a one-line `summary`.
- Show the user the returned diff and tell them it's waiting in Review. Don't re-propose the
  same changes; check list_proposals if unsure what's pending.
- Use `note` for progress ("Dana sent the contract; review Friday"), not rewriting notes.
- Tasks have a notebook of blocks; a block can be a subtask (checkbox). Break a task down with
  add_subtask (title + optional markdown body), and mark one done with check_subtask
  (block_id from get_task). Both are proposals like everything else on tasks.
- Tasks can be grouped (the user drags one onto another in the app, like an iOS folder): a
  group is a parent task whose `children` are full tasks; a child has `parent_id` / `parent`.
  Treat a group's children as the real work items.
- Notes, blocks and progress notes can hold the user's screenshots as markdown images
  (`![screenshot](/api/uploads/<name>)`). Keep them when you edit or rewrite any text.
  Put ticket links (Jira etc.) in the task's external_url; customer-wide links use add_link.

People (you write these directly)
- The user assigns tasks to people (assignee; none = the user: only the user's own tasks are on
  their Today) and adds followers: people a task concerns, to discuss or keep in the loop (the
  task stays the user's). @mentions in task text add followers. Each person has a page for 1:1s: follow-ups, what they're on, shared work across
  customers, recent wins, meetings, and 1:1 notes (a notebook). get_person reads it all.
- Keep the directory tidy: list_people before create_person (match by email or name); set
  their employer (customer) when they're client-side. Notes from a 1:1 go in their notebook
  (add_person_note).
- Assigning a task or adding a follower is a task change: propose it (update with `assignee`,
  or action "follow" with `person`).

Ideas (you write these directly)
- Ideas are not-yet-tasks: things worth keeping that aren't ready to be worked (a feature
  thought, a "we should someday…", an opportunity a customer hinted at). They never show on
  boards, Today or the bar. Capture them with capture_idea (customer and/or project when it
  has one); when one is clearly ready, propose it as a task (propose_changes, promote_idea).
- An idea is a notebook: a one-line summary plus markdown blocks, each a small document for
  one part (the problem, options, open questions, a rough plan, what the customer said). Work
  on an idea by adding a block (add_idea_block) or extending one (update_idea_block with
  append); read them with get_idea. Give blocks short titles. Never rewrite a block the user
  wrote unless asked; append instead.
- From meeting notes: commitments and asks become tasks; maybes, "it'd be nice if", and
  ideas the user floats become ideas. When unsure, it's an idea.
- Check list_ideas before capturing so you extend an existing idea instead of duplicating it.

Agent assignments
- save_agent(executor="codex-herdr" or "codex-app") selects a shared machine harness.
  Requires todo-agent 1.4+. Projects are optional restrictions, not required setup.
  Herdr uses the named default session unless overridden on the machine, and verifies
  repository identity to reuse project workspaces. Codex app uses an already connected
  local Remote daemon; Todo never enables or pairs it. Repository tasks get worktrees;
  general tasks get isolated scratch folders. Permissions are answered by the user in
  the selected harness. Agent completion requests always stay in Review.

Customer hub (you write these directly)
- get_customer reads a hub: its topics (the threads the customer keeps raising, each with
  "where things stand" and its latest updates), upcoming meetings with prep, recaps, projects,
  links, and the work needing attention.
- Topics are how the user sees where a customer stands. Each one has a short
  where_things_stand (the current state, 1-3 sentences) and a timeline of dated updates.
  Updates are append-only: add what's new, never restate or rewrite history. Change a topic's
  where_things_stand only when its picture changed, and only that topic's. Reuse existing names
  (case-insensitive match); create a topic only for a genuinely new thread. Mark one resolved
  when it's closed. If get_customer shows an earlier_overview, it's retired text: carry what
  still matters into the right topics' where_things_stand when you next touch them.
- Calendar: sync_calendar mirrors the user's customer meetings for a date window (you read their
  calendar; match each event to a customer by attendee domains / titles, skip internal ones).
  Then write prep with set_meeting_prep: open tasks to raise, waiting items to chase, active
  topics, what changed since the last meeting. Short markdown bullets.
- After a meeting: log_meeting (pass calendar_id when it was a synced event, so the scheduled
  entry becomes the recap) with summary, attendees, decisions and the topics it touched (each
  with `update`: what was said about it, and `where_things_stand` when that changed); then
  propose_changes with its meeting_id. Outside a meeting (an email, a ticket), use
  log_topic_updates.

Refs and the ledger
- Every proposed item from a source you'll read again (mail, calendar, Jira, Slack/Teams) gets a
  `ref`: gmail:<threadId>, gcal:<eventId>[#item-slug], jira:<KEY-123>, slack:<channel>/<ts>,
  teams:<messageId>. The app remembers each ref and what the user decided: propose_changes
  skips refs already in Review, refs the user rejected, creates for refs that are already a
  task (send a note/update instead; the ref finds the task), and anything touching a closed
  task. Skipped items come back in `skipped` with why. Don't fight it: `reconsider` (rejected)
  and `reopen` (closed) are for genuinely new information, with a reason.
- check_refs first, before reading items in depth. Scheduled client syncs follow
  get_sync_guide (the todo-sync skill): get_client_sync, then per feed (one query against one
  source; a client can have several per source) check_refs → hub writes → propose with refs →
  set_sync_state for that feed. save_sync_feed / remove_sync_feed manage a client's feeds.

Agents (you set these up directly)
- An agent is a person of kind agent: Claude Code running headless on one of the user's
  machines (via todo-agent) as a Claude Code agent defined in the repo
  (.claude/agents/<name>.md). Assigning a task to one (propose `update` with `assignee`; the
  user can also do it in the app) starts it: in a git worktree of the task's project repo when
  the project has one (finishing with a PR), else in a scratch folder (general work: research,
  drafts, plans). Either way it logs progress on the task, asks questions, and finishes with a
  "done" proposal. Agents talk to the app over a separate, task-scoped endpoint; they never get
  these tools.
- To set one up (for the repo you're in, or a general one), follow the setup_agent prompt: get_agent_templates →
  write the agent file and the todo-worker skill into the repo → set_project_repo →
  list_machines → save_agent → check_agent_setup → test_agent (poll get_run).
- list_agents / check_agent_setup answer "what agents do I have / why didn't it start".

Meeting cadences (you write these directly; a skill preps them)
- A cadence is a customer's recurring meeting (weekly ops, monthly exec review…): schedule,
  purpose, agenda topics, and prep steps (what to run or gather; a step may name a desktop tool
  and the files it produces). Each occurrence is one meeting: its prep steps as real tasks (a
  group "Prep: <cadence> · <date>"), talking points per agenda topic, notes and files. The app
  makes the next one `prep_days` ahead (prepare_meeting makes it sooner).
- To prep a meeting: get_meeting_prep (cadence name → the next meeting's packet: purpose,
  steps with their tool command / cwd / outputs and task status, topics with guidance and last
  meeting's points, files). Do the steps (run scripts locally when you're in Claude Code; a
  desktop tool's command is in the packet), then:
  - attach what you produce with attach_file (text, or base64 for small binaries; pass `step`).
    For big local files, `todo-agent upload <occurrence_id> <path>… [--step <step_id>]`;
  - write each topic's talking points with set_talking_points (short markdown bullets, the
    agenda's guidance tells you what belongs there; build on last meeting's points);
  - complete_prep_step for each step you finished (this checks off its task directly);
  - set_meeting_prep_status "ready" when everything's in.
- read_file reads an attached text file (CSV, markdown, JSON) so you can summarize reports.
- save_cadence sets one up (or changes it) when the user describes a recurring meeting.

Turning meeting notes into proposals
1. get_overview, then get_customer for the customer.
2. For each action item, search_tasks with its key words (names, systems, customer). Search
   covers closed tasks, so you can tell "already done" from "new". Prefer updating an existing
   task over a near-duplicate; only items the user owns or must chase become tasks.
3. Classify: create / note (progress, optional status) / complete / update (scope, due date,
   link) / nothing. Then one propose_changes call.
- Dates are YYYY-MM-DD; date-times ISO 8601 with offset. In updates, "" clears a field.
"""

mcp = MCPServer(name="todo", title="Todo", instructions=INSTRUCTIONS)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

Area = Literal["work", "personal"]
Status = Literal["inbox", "todo", "in_progress", "waiting", "done", "cancelled"]
Priority = Literal["none", "low", "medium", "high"]


def _tool(annotations: ToolAnnotations) -> Callable[[F], F]:
    """Register a tool: traced, with expected failures returned as readable tool errors."""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            with tracer.start_as_current_span(
                "mcp.tool_call", attributes={"mcp.tool.name": fn.__name__}
            ) as span:
                try:
                    return await fn(*args, **kwargs)
                except (NotFound, Invalid, ValueError) as exc:
                    span.set_attribute("mcp.tool.outcome", "error")
                    span.set_attribute("error.message", str(exc)[:200])
                    raise ToolError(str(exc)) from exc
                except Exception as exc:
                    telemetry.record_error(span, exc)
                    raise

        mcp.tool(annotations=annotations)(wrapper)
        return fn

    return decorate


# ----- shapes -----

def _task(t: dict[str, Any], notes: int | None = 200) -> dict[str, Any]:
    out = {
        "id": t["id"],
        "title": t["title"],
        "status": t["status"],
        "area": t["area"],
        "project": t["project"],
        "customer": t["customer"],
        "priority": PRIORITY_LABELS[t["priority"]],
        "due_on": t["due_on"],
        "today": t["today"],
        "overdue": t["overdue"],
        "waiting_on": t["waiting_on"],
        "source": t["source"],
        "external_id": t["external_id"],
        "external_url": t["external_url"],
        "subtasks": f"{t['subtasks_done']}/{t['subtasks_total']}" if t.get("subtasks_total") else None,
        "assignee": t.get("assignee"),
        "followers": [p["name"] for p in t.get("followers") or []] or None,
        "updated_at": t["updated_at"],
        "completed_at": t["completed_at"],
    }
    note_text = t["notes"] or ""
    out["notes"] = note_text if notes is None or len(note_text) <= notes else note_text[:notes] + "…"
    return {k: v for k, v in out.items() if v not in (None, "", False) or k in ("today", "status")}


def _project(p: dict[str, Any]) -> dict[str, Any]:
    return {
        k: p[k]
        for k in ("id", "name", "area", "customer", "description", "archived", "open_count", "overdue_count",
                  "repo_path", "default_branch")
        if k in p and p[k] not in (None, "")
    }


def _customer(c: dict[str, Any]) -> dict[str, Any]:
    return {
        k: c[k]
        for k in ("id", "name", "website", "notes", "archived", "project_count", "open_count", "overdue_count",
                  "last_meeting_on")
        if k in c and c[k] not in (None, "")
    }


def _meeting(m: dict[str, Any], summary: int | None = None) -> dict[str, Any]:
    text = m["summary"] or ""
    out = {
        "id": m["id"],
        "status": m["status"],
        "customer": m.get("customer"),
        "title": m["title"],
        "held_on": m["held_on"],
        "starts_at": m["starts_at"],
        "calendar_id": m["calendar_id"],
        "location": m["location"],
        "project": m.get("project"),
        "attendees": m["attendees"],
        "prep": m["prep"] if summary is None or m["status"] == "scheduled" else None,
        "summary": text if summary is None or len(text) <= summary else text[:summary] + "…",
        "decisions": m["decisions"] if summary is None else None,
        "external_url": m["external_url"],
        "source_label": hub.meeting_source(m) if m["status"] == "held" else None,
    }
    if "tasks" in m:
        out["tasks"] = [{"id": t["id"], "title": t["title"], "status": t["status"], "action": t["action"]}
                        for t in m["tasks"]]
    return {k: v for k, v in out.items() if v not in (None, "")}


def _topic(t: dict[str, Any]) -> dict[str, Any]:
    out = {"topic_id": t["id"], "name": t["name"], "status": t["status"], "where_things_stand": t["summary"],
           "last_update_on": t.get("last_update_on") or t.get("last_mentioned_on")}
    if "updates" in t:
        out["recent_updates"] = [
            {"on": u["happened_on"], "update": u["body"], **({"meeting": u["meeting_title"]} if u.get("meeting_title") else {})}
            for u in t["updates"][:5]
        ]
    return out


def _idea(i: dict[str, Any], blocks: bool = False) -> dict[str, Any]:
    out = {
        "id": i["id"], "title": i["title"], "summary": i["summary"], "status": i["status"], "area": i["area"],
        "customer": i["customer"], "project": i["project"], "block_count": i.get("block_count"),
        "task_id": i["task_id"], "updated_at": i["updated_at"],
    }
    if blocks:
        out["blocks"] = [
            {"id": b["id"], "title": b["title"], "body": b["body"], "updated_at": b["updated_at"]}
            for b in i.get("blocks") or ideas.blocks(i["id"])
        ]
    return {k: v for k, v in out.items() if v not in (None, "")}


def _customer_ref(ref: str | int) -> dict[str, Any]:
    return store.resolve_customer(ref, create=False)  # type: ignore[return-value]


def _proposal(cs: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cs["id"],
        "status": cs["status"],
        "source": cs["source"],
        "customer": cs["customer"],
        "review_url": f"/review?proposal={cs['id']}",
        "changes": [
            {"id": c["id"], "action": c["action"], "task_id": c["task_id"], "title": c["task_title"],
             "status": c["status"], **({"ref": c["ref"]} if c.get("ref") else {}),
             **({"error": c["error"]} if c["error"] else {})}
            for c in cs["changes"]
        ],
        "diff": diff_text(cs),
    }


# ----- reading -----

@_tool(READ)
async def get_overview() -> dict[str, Any]:
    """Start here: customers and projects (with open counts), what's on today / in progress, the
    inbox, upcoming customer meetings this week, pending proposals, and counts."""
    view = store.today_view()
    return {
        "date": view["date"],
        "counts": {**store.counts(), "proposals_pending": review.pending_count()},
        "customers": [_customer(c) for c in store.list_customers()],
        "projects": [_project(p) for p in store.list_projects()],
        "today": [_task(t, notes=0) for t in view["open"]],
        "inbox": [_task(t, notes=0) for t in store.list_tasks(status=["inbox"], limit=50)],
        "upcoming_meetings": [_meeting(m, summary=0) for m in hub.upcoming_meetings(days=7)],
        "open_ideas": ideas.count(),
        "settings": store.settings(),
    }


@_tool(READ)
async def list_tasks(
    area: Area | None = None,
    project: str | None = Field(None, description="Project name or id"),
    customer: str | None = Field(None, description="Customer name or id"),
    assignee: str | None = Field(None, description='Person name/email/id: tasks assigned to them; "me" = the user\'s own'),
    status: list[Status] | None = Field(None, description="Default: every open status"),
    today: bool | None = Field(None, description="true = only today's, false = only not today"),
    include_closed: bool = Field(False, description="Include done/cancelled when status isn't given"),
    limit: int = Field(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List tasks, in-progress first, then by priority and due date. Notes are truncated."""
    project_id = customer_id = None
    if project:
        project_id = store.resolve_project(project)["id"]  # type: ignore[index]
    if customer:
        customer_id = _customer_ref(customer)["id"]
    assignee_id, mine = None, False
    if assignee == "me":
        mine = True
    elif assignee:
        assignee_id = people.resolve(assignee)["id"]  # type: ignore[index]
    tasks = store.list_tasks(
        status=status, area=area, project_id=project_id, customer_id=customer_id, today=today,
        assignee_id=assignee_id, mine=mine,
        include_closed=include_closed, limit=limit,
    )
    return [_task(t) for t in tasks]


@_tool(READ)
async def search_tasks(
    query: str = Field(description="Key words: people, systems, customer, the action. Every word is optional."),
    include_closed: bool = Field(True, description="Also match done/cancelled tasks (to spot already-finished work)"),
    area: Area | None = None,
    limit: int = Field(10, ge=1, le=50),
) -> list[dict[str, Any]]:
    """Full-text search over task titles, notes and history, best match first. Use before
    proposing a task to find one that already tracks the same work. Each hit includes its most
    recent history entries."""
    hits = store.search(query, include_closed=include_closed, area=area, limit=limit)
    out = []
    for hit in hits:
        row = _task(hit, notes=300)
        row["score"] = hit["score"]
        row["recent_history"] = [
            f"{u['created_at'][:10]} {u['source']}: {u['body']}"
            for u in store.task_updates(hit["id"])[-3:]
            if u["body"]
        ]
        out.append(row)
    return out


@_tool(READ)
async def get_task(task_id: int) -> dict[str, Any]:
    """One task with full notes, its notebook (blocks; subtasks have kind 'subtask' and done),
    its whole history, and the meetings it came up in."""
    task = store.get_task(task_id, with_updates=True)
    out = _task(task, notes=None)
    out["blocks"] = [
        {k: b[k] for k in ("id", "kind", "title", "body", "done")} for b in notebook.blocks("task", task_id)
    ]
    out["history"] = [
        {"at": u["created_at"], "kind": u["kind"], "source": u["source"], "text": u["body"]}
        for u in task["updates"]
    ]
    out["meetings"] = hub.meetings_for_task(task_id)
    return out


@_tool(READ)
async def list_customers(include_archived: bool = False) -> list[dict[str, Any]]:
    """Customers with website, notes, last meeting and project/open/overdue counts."""
    return [_customer(c) for c in store.list_customers(include_archived=include_archived)]


@_tool(READ)
async def get_customer(customer: str = Field(description="Customer name or id")) -> dict[str, Any]:
    """A customer's hub: profile, topics (where each stands + its latest updates; active in the
    last month, plus quieter ones by name), upcoming meetings (with prep), recent recaps,
    projects, links, and the open work that needs attention."""
    view = hub.customer_hub(_customer_ref(customer)["id"])
    c = view["customer"]
    month = hub.topics_view(c["id"], days=30, updates=5)
    active_ids = {t["id"] for t in month["topics"]}
    return {
        "customer": _customer(c),
        # The single overview is retired; fold anything still useful into topics, don't rewrite it.
        "earlier_overview": c["overview"] or None,
        "counts": {k: c[k] for k in ("open_count", "in_progress_count", "waiting_count", "overdue_count")},
        "projects": [_project(p) for p in view["projects"]],
        "topics": [_topic(t) for t in month["topics"]],
        "quieter_topics": [{"topic_id": t["id"], "name": t["name"], "status": t["status"]}
                           for t in view["topics"] if t["id"] not in active_ids],
        "upcoming_meetings": [_meeting(m, summary=0) for m in view["upcoming"]],
        "recent_meetings": [_meeting(m, summary=500) for m in view["meetings"]],
        "needs_attention": [_task(t, notes=0) for t in view["next_up"]],
        "ideas": [_idea(i) for i in ideas.list(customer_id=c["id"], limit=20)],
        "links": [{"label": l["label"], "url": l["url"]} for l in view["links"] if l["kind"] == "link"],
    }


@_tool(READ)
async def list_meetings(
    customer: str = Field(description="Customer name or id"),
    limit: int = Field(20, ge=1, le=200),
) -> list[dict[str, Any]]:
    """A customer's meeting recaps, newest first (summaries truncated; get_meeting for all of one)."""
    return [_meeting(m, summary=300) for m in hub.list_meetings(_customer_ref(customer)["id"], limit=limit)]


@_tool(READ)
async def list_upcoming_meetings(
    days: int = Field(7, ge=1, le=60),
    customer: str | None = Field(None, description="Customer name or id; default all customers"),
) -> list[dict[str, Any]]:
    """Scheduled customer meetings from today through the next `days` days, with prep."""
    customer_id = _customer_ref(customer)["id"] if customer else None
    return [_meeting(m, summary=0) for m in hub.upcoming_meetings(customer_id=customer_id, days=days)]


@_tool(READ)
async def get_meeting(meeting_id: int) -> dict[str, Any]:
    """One meeting in full (prep, recap, decisions) with the tasks it created or updated."""
    return _meeting(hub.get_meeting(meeting_id))


@_tool(READ)
async def list_projects(include_archived: bool = False) -> list[dict[str, Any]]:
    """Projects with area, customer and open/overdue task counts."""
    return [_project(p) for p in store.list_projects(include_archived=include_archived)]


@_tool(READ)
async def list_proposals(
    status: Literal["pending", "applied", "partial", "rejected"] | None = Field(
        "pending", description="Default: still waiting for the user"
    ),
    limit: int = Field(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Proposals you (or earlier sessions) made, and whether the user applied them."""
    return [
        {k: cs[k] for k in ("id", "status", "source", "summary", "customer", "change_count", "created_at", "decided_at")}
        for cs in review.list(status=status, limit=limit)
    ]


@_tool(READ)
async def get_proposal(proposal_id: int) -> dict[str, Any]:
    """One proposal as a diff, with each change's status (pending / applied / rejected / failed)."""
    return _proposal(review.get(proposal_id))


# ----- proposing task changes -----

class ChangeItem(BaseModel):
    action: Literal["create", "update", "note", "complete", "add_link", "promote_idea", "add_subtask", "check_subtask", "follow"] = Field(
        description="create = new task; update = change fields; note = log progress (status optional); "
        "complete = mark done; add_link = bookmark a URL on the customer's hub; "
        "promote_idea = turn an idea into a task (title/notes carried over; give project if the idea has none); "
        "add_subtask = add a subtask block to task_id (title, optional body); "
        "check_subtask = mark subtask block_id done (done=false reopens); "
        "follow = add `person` as a follower of task_id (following=false removes them)"
    )
    ref: str | None = Field(
        None,
        description="Where this came from, stable across runs: gmail:<threadId>, gcal:<eventId> (or "
        "gcal:<eventId>#<item-slug> for one of a meeting's action items), jira:<KEY-123>, slack:<channel>/<ts>, "
        "teams:<messageId>. Always set it when the item came from a source you'll read again. Items whose ref "
        "you already proposed, the user rejected, or that became a closed task are skipped; with a ref, "
        "note/update/complete find their task by it (task_id optional).",
    )
    reconsider: bool | None = Field(None, description="Bring back something the user rejected (needs a reason: what's new)")
    reopen: bool | None = Field(None, description="Touch a closed task (needs a reason: why it's back)")
    task_id: int | None = Field(None, description="Required for update, note and complete (unless the ref finds it)")
    idea_id: int | None = Field(None, description="Required for promote_idea")
    block_id: int | None = Field(None, description="check_subtask: the subtask block (get_task blocks)")
    body: str | None = Field(None, description="add_subtask: the subtask's markdown details")
    done: bool | None = Field(None, description="check_subtask: true (default) = done, false = reopen")
    assignee: str | None = Field(None, description='create/update: person doing it (name, email or id); "" = the user')
    person: str | None = Field(None, description="follow: the person (name, email or id)")
    following: bool | None = Field(None, description="follow: true (default) adds them, false removes them")
    title: str | None = None
    notes: str | None = Field(None, description="create: the task's notes. update: replaces them")
    area: Area | None = None
    project: str | None = Field(None, description='Project name or id; "" removes')
    status: Status | None = None
    priority: Priority | None = None
    due_on: str | None = Field(None, description='YYYY-MM-DD; "" clears')
    today: bool | None = None
    waiting_on: str | None = None
    external_url: str | None = Field(None, description="Ticket link (e.g. the Jira issue) for the task")
    note: str | None = Field(None, description="Progress note for the history. Required for action 'note'.")
    reason: str | None = Field(None, description="Why: the line from the notes/conversation behind this change")
    customer: str | None = Field(None, description="add_link: customer name or id (default: the meeting's customer)")
    label: str | None = Field(None, description="add_link: e.g. 'Jira board'")
    url: str | None = Field(None, description="add_link: https://…")


@_tool(WRITE)
async def propose_changes(
    items: list[ChangeItem] = Field(description="Every change from this source"),
    summary: str = Field(description="One line for the review list, e.g. 'Acme weekly: 2 new, 1 done, SSO update'"),
    meeting_id: int | None = Field(None, description="The meeting (from log_meeting) these came from"),
    source: str | None = Field(None, description="Without a meeting, where these came from, e.g. 'email: Acme renewal'"),
    customer: str | None = Field(None, description="The customer these are about, if no meeting"),
) -> dict[str, Any]:
    """Propose task changes (and customer links) for the user to review as a diff and approve in
    the app. Nothing changes until they approve. Returns the proposal with its diff; show the
    diff to the user and point them to Review. Items already decided on (by ref: pending,
    rejected, already a task; or touching a closed task) come back in `skipped`, with why."""
    meeting = hub.get_meeting(meeting_id) if meeting_id else None
    label = source or (hub.meeting_source(meeting) if meeting else None)
    if not label:
        raise Invalid("Give meeting_id or source")
    customer_id = _customer_ref(customer)["id"] if customer else None
    with tracer.start_as_current_span(
        "proposal.create",
        attributes={"proposal.source": label, "proposal.count": len(items), "proposal.meeting_id": meeting_id or 0},
    ) as span:
        cs = review.propose(
            [i.model_dump(exclude_none=True) for i in items], source=label, summary=summary,
            meeting_id=meeting_id, customer_id=customer_id,
        )
        span.set_attribute("proposal.id", cs["id"] or 0)
        span.set_attribute("proposal.skipped", len(cs["skipped"]))
        span.set_attribute("proposal.auto_applied", cs["status"] not in ("pending", "nothing_new"))
    if cs["status"] == "nothing_new":
        return {"status": "nothing_new", "skipped": cs["skipped"],
                "next": "Nothing new: everything was already decided on. Nothing went to Review."}
    out = _proposal(cs)
    out["skipped"] = cs["skipped"]
    out["next"] = (
        "Waiting for the user to review in the app (Review)." if cs["status"] == "pending"
        else "Review is off in settings, so these were applied immediately."
    )
    return out


# ----- people (directory + 1:1 notes are direct writes; assignment is a proposal) -----

def _person(p: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "email", "title", "customer", "area", "notes", "assigned_open", "overdue", "following_open")
    return {k: p[k] for k in keys if k in p and p[k] not in (None, "")}


@_tool(READ)
async def list_people(area: Area | None = None) -> list[dict[str, Any]]:
    """Everyone in the directory, with their employer and open / overdue task counts."""
    return [_person(p) for p in people.list(area=area)]


@_tool(READ)
async def get_person(person: str = Field(description="Name, email or id")) -> dict[str, Any]:
    """A person's page, for a 1:1: what to follow up on (overdue, due this week, waiting on
    them), everything assigned to them, tasks you share, what they finished in the last 30
    days, customers/projects you work on together, meetings, and 1:1 notes."""
    view = people.view(people.resolve(person)["id"])  # type: ignore[index]
    return {
        "person": _person(view["person"]),
        "counts": view["counts"],
        "follow_up": [_task(t, notes=0) for t in view["follow_up"]],
        "assigned": [_task(t, notes=0) for t in view["assigned"]],
        "following": [_task(t, notes=0) for t in view["following"]],
        "done_recently": [_task(t, notes=0) for t in view["done_recently"]],
        "shared": [{k: v for k, v in s.items() if v is not None} for s in view["shared"]],
        "meetings": [_meeting(m, summary=200) for m in view["meetings"]],
        "notes": [{"id": b["id"], "title": b["title"], "body": b["body"]} for b in view["notes"]],
    }


@_tool(WRITE)
async def create_person(
    name: str,
    email: str | None = None,
    title: str = Field("", description="Their role, e.g. 'Head of IT'"),
    customer: str | None = Field(None, description="Their employer if client-side (customer name or id)"),
    area: Area = "work",
    notes: str = "",
) -> dict[str, Any]:
    """Add someone to the directory (check list_people first)."""
    customer_id = _customer_ref(customer)["id"] if customer else None
    return _person(people.create(name, email=email, title=title, customer_id=customer_id, area=area, notes=notes))


@_tool(WRITE)
async def update_person(
    person: str = Field(description="Name, email or id"),
    name: str | None = None,
    email: str | None = None,
    title: str | None = None,
    customer: str | None = Field(None, description='Customer name or id; "" = your side'),
    notes: str | None = None,
) -> dict[str, Any]:
    """Update someone's details."""
    target = people.resolve(person)
    fields: dict[str, Any] = {"name": name, "title": title, "notes": notes}
    if email is not None:
        fields["email"] = email
    if customer is not None:
        fields["customer_id"] = _customer_ref(customer)["id"] if customer else None
    return _person(people.update(target["id"], **fields))  # type: ignore[index]


@_tool(WRITE)
async def add_person_note(
    person: str = Field(description="Name, email or id"),
    title: str = Field(description="e.g. '1:1 2026-10-06' or 'Career goals'"),
    body: str = Field(description="Markdown"),
) -> dict[str, Any]:
    """Add a block to someone's 1:1 notes notebook."""
    target = people.resolve(person)
    block = notebook.add("person", target["id"], title=title, body=body, source="mcp")  # type: ignore[index]
    return {k: block[k] for k in ("id", "person_id", "title", "body")}


# ----- ideas (direct writes; promotion is a proposal) -----

class BlockInput(BaseModel):
    title: str = ""
    body: str = ""

@_tool(READ)
async def list_ideas(
    customer: str | None = Field(None, description="Customer name or id"),
    project: str | None = Field(None, description="Project name or id"),
    query: str | None = Field(None, description="Words that must all appear in the title, summary or a block"),
    status: Literal["open", "promoted", "dropped"] | None = Field("open", description="null = all"),
    area: Area | None = None,
    limit: int = Field(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Ideas (not-yet-tasks), most recently touched first."""
    customer_id = _customer_ref(customer)["id"] if customer else None
    project_id = store.resolve_project(project)["id"] if project else None  # type: ignore[index]
    return [_idea(i) for i in ideas.list(status=status, area=area, customer_id=customer_id,
                                          project_id=project_id, query=query, limit=limit)]


@_tool(READ)
async def get_idea(idea_id: int) -> dict[str, Any]:
    """One idea with its whole notebook (every block, in order)."""
    return _idea(ideas.get(idea_id, with_blocks=True), blocks=True)


@_tool(WRITE)
async def capture_idea(
    title: str = Field(description="Short and specific, e.g. 'Self-serve onboarding for Acme admins'"),
    summary: str = Field("", description="One or two sentences: what it is and why"),
    blocks: list[BlockInput] = Field(default_factory=list, description="Optional starting blocks, e.g. 'What they said', 'Open questions'"),
    customer: str | None = Field(None, description="Customer it's for (name or id), if any"),
    project: str | None = Field(None, description="Project it belongs to (name or id), if any"),
    area: Area | None = Field(None, description="Only when there's no customer or project"),
    source: str = Field("mcp", description="e.g. 'meeting: Acme weekly 2026-10-05'"),
) -> dict[str, Any]:
    """Save an idea: something worth keeping that isn't ready to be a task. Kept off boards."""
    customer_id = _customer_ref(customer)["id"] if customer else None
    project_id = store.resolve_project(project)["id"] if project else None  # type: ignore[index]
    return _idea(ideas.create(title, summary=summary, blocks=[b.model_dump() for b in blocks], area=area,
                              customer_id=customer_id, project_id=project_id, source=source), blocks=True)


@_tool(WRITE)
async def update_idea(
    idea_id: int,
    title: str | None = None,
    summary: str | None = Field(None, description="Replaces the one-line summary"),
    customer: str | None = Field(None, description='Customer name or id; "" removes'),
    project: str | None = Field(None, description='Project name or id; "" removes'),
    status: Literal["open", "dropped"] | None = Field(None, description="dropped = not pursuing (kept, not deleted)"),
) -> dict[str, Any]:
    """Rename an idea, change its summary, place it under a customer or project, or drop it.
    (Its content lives in blocks: add_idea_block / update_idea_block.) To make it a task,
    propose_changes with action promote_idea."""
    fields: dict[str, Any] = {"title": title, "summary": summary, "status": status}
    if customer is not None:
        fields["customer_id"] = _customer_ref(customer)["id"] if customer else None
    if project is not None:
        resolved = store.resolve_project(project)
        fields["project_id"] = resolved["id"] if resolved else None
    return _idea(ideas.update(idea_id, **fields))


@_tool(WRITE)
async def add_idea_block(
    idea_id: int,
    title: str = Field(description="Short heading for this part, e.g. 'Rollout options'"),
    body: str = Field(description="Markdown content of the block"),
    after_block_id: int | None = Field(None, description="Insert after this block (0 = top); default: at the end"),
    source: str = "mcp",
) -> dict[str, Any]:
    """Add a block (a small markdown document) to an idea's notebook."""
    block = ideas.add_block(idea_id, title=title, body=body, after_id=after_block_id, source=source)
    return {k: block[k] for k in ("id", "idea_id", "title", "body")}


@_tool(WRITE)
async def update_idea_block(
    block_id: int,
    title: str | None = None,
    body: str | None = Field(None, description="Replaces the block's content; prefer append for the user's blocks"),
    append: str | None = Field(None, description="Added to the end of the block as a new paragraph"),
) -> dict[str, Any]:
    """Rename, rewrite or extend one block of an idea."""
    if not notebook.get(block_id)["idea_id"]:
        raise Invalid("That block belongs to a task; propose task changes with propose_changes")
    block = notebook.update(block_id, title=title, body=body, append=append, source="mcp")
    return {k: block[k] for k in ("id", "idea_id", "title", "body")}


# ----- customer hub (direct writes) -----

class TopicInput(BaseModel):
    name: str = Field(description="Short noun phrase, e.g. 'SSO rollout', 'Renewal pricing'. Reuse existing names (get_customer).")
    update: str = Field("", description="What's new on this topic (from this meeting): 1-3 short sentences or bullets. Appended to its timeline.")
    where_things_stand: str | None = Field(None, description="Replace the topic's where-things-stand when the picture changed: 1-3 sentences, the current state, not history")
    status: Literal["active", "watching", "resolved"] | None = None
    summary: str | None = Field(None, description="Deprecated: use where_things_stand")


class CalendarEvent(BaseModel):
    calendar_id: str = Field(description="The calendar event's id (stable across syncs)")
    customer: str = Field(description="Customer name or id this meeting is with")
    title: str
    starts_at: str = Field(description="ISO 8601 with offset, e.g. 2026-10-06T14:00:00-04:00")
    ends_at: str | None = None
    attendees: str = Field("", description="Comma-separated names / emails")
    location: str | None = Field(None, description="Video link or room")


@_tool(WRITE)
async def sync_calendar(
    events: list[CalendarEvent] = Field(description="Every customer meeting in the window (skip internal ones)"),
    window_start: str = Field(description="YYYY-MM-DD, first day you read"),
    window_end: str = Field(description="YYYY-MM-DD, last day you read"),
) -> dict[str, Any]:
    """Mirror the user's upcoming customer meetings into their hubs. Events are matched by
    calendar_id; scheduled meetings in the window that are no longer on the calendar become
    cancelled. Then write prep for each with set_meeting_prep."""
    resolved = []
    for event in events:
        data = event.model_dump()
        data["customer_id"] = _customer_ref(data.pop("customer"))["id"]
        resolved.append(data)
    with tracer.start_as_current_span("calendar.sync", attributes={"calendar.events": len(events)}) as span:
        result = hub.sync_calendar(resolved, window_start=window_start, window_end=window_end)
        span.set_attributes({f"calendar.{k}": len(v) for k, v in result.items()})
    return {**result, "upcoming": [_meeting(m, summary=0) for m in hub.upcoming_meetings(days=14)]}


@_tool(WRITE)
async def set_meeting_prep(
    meeting_id: int,
    prep: str = Field(description="Markdown bullets: tasks to raise, items to chase, active topics, what changed"),
) -> dict[str, Any]:
    """Write (replace) the prep notes for an upcoming meeting."""
    return _meeting(hub.update_meeting(meeting_id, prep=prep), summary=0)


@_tool(WRITE)
async def log_meeting(
    customer: str = Field(description="Customer name or id"),
    title: str = Field(description="e.g. 'Weekly sync', 'Renewal call'"),
    held_on: str = Field(description="YYYY-MM-DD"),
    summary: str = Field(description="The recap in markdown: what was discussed, in short bullets or paragraphs"),
    attendees: str = Field("", description="Comma-separated names (and roles if useful)"),
    decisions: str = Field("", description="Decisions made, markdown bullets"),
    project: str | None = Field(None, description="The customer's project it was about, if one"),
    external_url: str | None = Field(None, description="Link to the recording, transcript or notes doc"),
    calendar_id: str | None = Field(None, description="The calendar event, if it was synced: that entry becomes the recap"),
    topics: list[TopicInput] = Field(default_factory=list, description="Topics discussed; matched to existing ones by name"),
) -> dict[str, Any]:
    """Save a meeting recap to the customer's hub and record the topics it touched. Returns the
    meeting id: pass it to propose_changes so the tasks from this meeting link back to it."""
    target = _customer_ref(customer)
    project_id = store.resolve_project(project)["id"] if project else None  # type: ignore[index]
    with store.tx():
        scheduled = hub.find_meeting_by_calendar_id(calendar_id) if calendar_id else None
        if scheduled:
            meeting = hub.update_meeting(
                scheduled["id"], status="held", title=title, held_on=held_on, attendees=attendees or None,
                summary=summary, decisions=decisions, project_id=project_id,
                **({"external_url": external_url} if external_url else {}),
            )
        else:
            meeting = hub.create_meeting(
                target["id"], title=title, held_on=held_on, attendees=attendees, summary=summary,
                decisions=decisions, project_id=project_id, external_url=external_url, source="mcp",
            )
        if topics:
            hub.log_topic_updates(target["id"], [_topic_item(t) for t in topics], happened_on=meeting["held_on"],
                                  meeting_id=meeting["id"], source="mcp")
    return _meeting(meeting)


def _topic_item(t: TopicInput) -> dict[str, Any]:
    return {"topic": t.name, "update": t.update or "", "status": t.status,
            "where_things_stand": t.where_things_stand if t.where_things_stand is not None else t.summary}


@_tool(WRITE)
async def update_meeting(
    meeting_id: int,
    title: str | None = None,
    held_on: str | None = None,
    summary: str | None = None,
    attendees: str | None = None,
    decisions: str | None = None,
    external_url: str | None = None,
) -> dict[str, Any]:
    """Correct or extend a meeting recap. Omitted fields stay as they are."""
    return _meeting(hub.update_meeting(
        meeting_id, title=title, held_on=held_on, summary=summary, attendees=attendees,
        decisions=decisions, **({"external_url": external_url} if external_url is not None else {}),
    ))


@_tool(WRITE)
async def log_topic_updates(
    customer: str = Field(description="Customer name or id"),
    topics: list[TopicInput] = Field(description="Topics with something new; matched to existing ones by name (case-insensitive)"),
    happened_on: str | None = Field(None, description="YYYY-MM-DD it happened (default today)"),
    meeting_id: int | None = Field(None, description="The meeting it came up in, if any"),
) -> list[dict[str, Any]]:
    """Add what's new on the customer's topics: each `update` is appended to that topic's
    timeline (never rewritten); `where_things_stand` replaces only that topic's summary. A new
    name creates a topic. log_meeting does this for a recap's topics."""
    target = _customer_ref(customer)
    touched = hub.log_topic_updates(target["id"], [_topic_item(t) for t in topics], happened_on=happened_on,
                                    meeting_id=meeting_id, source="mcp")
    return [_topic(t) for t in touched]


@_tool(WRITE)
async def create_customer(
    name: str,
    website: str | None = Field(None, description="Their domain or homepage"),
    notes: str = Field("", description="Who they are, contacts, how the user works with them"),
) -> dict[str, Any]:
    """Create a customer. Ask the user first unless they asked for it; check list_customers for
    an existing one under a slightly different name."""
    return _customer(store.create_customer(name, notes, website))


@_tool(WRITE)
async def update_customer(
    customer: str = Field(description="Customer name or id"),
    name: str | None = None,
    notes: str | None = Field(None, description="Who they are, contacts, how the user works with them"),
    website: str | None = None,
) -> dict[str, Any]:
    """Rename a customer or update its notes or website. (Where things stand lives in topics: log_topic_updates.)"""
    target = _customer_ref(customer)
    extra = {"website": website} if website is not None else {}
    return _customer(store.update_customer(target["id"], name=name, notes=notes, **extra))


@_tool(WRITE)
async def create_project(
    name: str,
    area: Area,
    customer: str | None = Field(None, description="Customer name or id; an unknown name creates the customer"),
    description: str = "",
) -> dict[str, Any]:
    """Create a project. Ask the user first unless they asked for it."""
    return _project(store.create_project(name, area, customer, description))


# ----- cadences -----


class AgendaTopic(BaseModel):
    title: str
    guidance: str = Field("", description="What belongs under this topic / how to prepare it")


class PrepStep(BaseModel):
    title: str
    instructions: str = Field("", description="What to run or gather, and how (markdown)")
    tool: str | None = Field(None, description="A desktop tool (launcher) on the customer's hub, by label or id")
    due_hours_before: int = Field(24, description="When it should be done, in hours before the meeting")
    outputs: str = Field("", description="Files the step produces, one path/glob per line (~ and tool-cwd relative)")
    id: int | None = Field(None, description="An existing step's id, to keep its history when editing")


def _cadence_brief(c: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "name", "customer", "customer_id", "project", "schedule", "schedule_text", "prep_days",
            "duration_min", "active", "purpose", "agenda", "steps_count", "next")
    return {k: c[k] for k in keep if k in c}


def _packet(occ: dict[str, Any]) -> dict[str, Any]:
    """The prep packet, trimmed for the model."""
    cad = occ["cadence"]
    return {
        "occurrence_id": occ["id"],
        "cadence": {"id": cad["id"], "name": cad["name"], "customer": cad["customer"], "purpose": cad["purpose"],
                    "schedule": cad["schedule_text"]},
        "starts_at": occ["starts_at"],
        "status": occ["status"],
        "meeting_id": occ["meeting_id"],
        "attendees": (occ.get("meeting") or {}).get("attendees", ""),
        "prep_task_id": occ["prep_task_id"],
        "prep": occ["prep"],
        "notes": occ["notes"],
        "steps": [
            {"step_id": s["id"], "title": s["title"], "instructions": s["instructions"],
             "due_hours_before": s["due_hours_before"], "outputs": s["outputs"],
             "tool": {"label": s["tool"], "command": s["tool_command"], "cwd": s["tool_cwd"]} if s["tool"] else None,
             "task": s["task"], "files": [f["name"] for f in s["files"]]}
            for s in occ["steps"]
        ],
        "topics": [{"topic_id": t["id"], "title": t["title"], "guidance": t["guidance"], "points": t["points"]}
                   for t in occ["topics"]],
        "files": [{"file_id": f["id"], "name": f["name"], "content_type": f["content_type"], "bytes": f["bytes"],
                   "step_id": f["step_id"], "note": f["note"], "source": f["source"]} for f in occ["files"]],
        "last_meeting": {
            "occurrence_id": occ["previous"]["id"], "held_on": occ["previous"]["held_on"], "notes": occ["previous"]["notes"],
            "topics": [{"title": t["title"], "points": t["points"]} for t in occ["previous"]["topics"]],
        } if occ.get("previous") else None,
    }


@_tool(READ)
async def list_cadences(customer: str | None = Field(None, description="Customer name or id; all when omitted")) -> list[dict[str, Any]]:
    """The recurring meetings (cadences), with their schedule and next meeting."""
    customer_id = _customer_ref(customer)["id"] if customer else None
    return [_cadence_brief(c) for c in cadences.list(customer_id=customer_id)]


@_tool(READ)
async def get_cadence(cadence: str = Field(description="Cadence name or id")) -> dict[str, Any]:
    """A cadence's template (purpose, agenda, prep steps with tools) and its recent/next meetings."""
    c = cadences.get(cadences.resolve(cadence)["id"])
    return {**_cadence_brief(c), "steps": c["steps"], "upcoming": c["upcoming"],
            "recent": [{k: o[k] for k in ("id", "held_on", "status", "prep", "files")} for o in c["occurrences"]]}


@_tool(WRITE)
async def save_cadence(
    customer: str = Field(description="Customer name or id"),
    name: str = Field(description="e.g. 'Weekly Ops'"),
    schedule: dict[str, Any] | None = Field(None, description='{"cron": "0 9 * * 4"} (Thu 9am), {"nth": 2, "weekday": 1, "time": "10:00"} (2nd Tue; weekday 0=Mon), or {"calendar": "<text in the synced meeting title>"}'),
    purpose: str | None = Field(None, description="What the meeting is for and how to prep it (markdown)"),
    agenda: list[AgendaTopic] | None = None,
    steps: list[PrepStep] | None = Field(None, description="The full list of prep steps, in order (replaces the current list)"),
    prep_days: int | None = Field(None, description="Days ahead to create each meeting's prep (default 3)"),
    duration_min: int | None = None,
    project: str | None = Field(None, description="The customer's project for the prep tasks"),
    cadence_id: int | None = Field(None, description="To change an existing cadence"),
) -> dict[str, Any]:
    """Create or update a cadence. Confirm with the user before replacing steps they wrote."""
    cust = _customer_ref(customer)
    fields: dict[str, Any] = {"purpose": purpose, "prep_days": prep_days, "duration_min": duration_min}
    if agenda is not None:
        fields["agenda"] = [a.model_dump() for a in agenda]
    if project:
        match = next((p for p in store.list_projects() if p["customer_id"] == cust["id"] and p["name"].lower() == project.lower()), None)
        if not match:
            raise Invalid(f"{cust['name']} has no project {project!r}")
        fields["project_id"] = match["id"]
    fields = {k: v for k, v in fields.items() if v is not None}
    if cadence_id:
        c = cadences.update(cadence_id, name=name, schedule=schedule, **fields)
    else:
        if not schedule:
            raise Invalid("A new cadence needs a schedule")
        c = cadences.create(cust["id"], name, schedule, **fields)
    if steps is not None:
        tools = {l["label"].lower(): l["id"] for l in hub.list_links(cust["id"]) if l["kind"] == "launcher"}
        resolved = []
        for step in steps:
            data = step.model_dump()
            tool = data.pop("tool")
            if tool:
                data["link_id"] = int(tool) if str(tool).isdigit() else tools.get(str(tool).lower())
                if not data["link_id"]:
                    raise Invalid(f"No desktop tool {tool!r} on {cust['name']}'s hub ({', '.join(tools) or 'none yet'})")
            resolved.append(data)
        cadences.set_steps(c["id"], resolved)
    cadences.ensure()
    return await get_cadence(str(c["id"]))


@_tool(WRITE)
async def get_meeting_prep(
    cadence: str | None = Field(None, description="Cadence name or id: its next meeting (made now if needed)"),
    occurrence_id: int | None = Field(None, description="A specific meeting instead"),
) -> dict[str, Any]:
    """The prep packet for a cadence meeting: steps (tools, outputs, task status), agenda topics
    with guidance and last meeting's points, notes and files. Creates the next meeting's prep if
    it doesn't exist yet."""
    if occurrence_id:
        return _packet(cadences.occurrence(occurrence_id))
    if not cadence:
        raise Invalid("Pass a cadence or an occurrence_id")
    return _packet(cadences.prepare(cadences.resolve(cadence)["id"]))


@_tool(WRITE)
async def set_talking_points(
    occurrence_id: int,
    topic: str = Field(description="Agenda topic title or topic_id; a new title adds a topic"),
    points: str = Field(description="Markdown bullets"),
    append: bool = Field(False, description="Add to what's there instead of replacing it"),
) -> dict[str, Any]:
    """Write the talking points for one agenda topic of a cadence meeting."""
    return cadences.set_points(occurrence_id, topic, points, append=append, source="mcp")


@_tool(WRITE)
async def update_meeting_notes(
    occurrence_id: int,
    notes: str | None = Field(None, description="Replace the meeting's prep notes (markdown)"),
    append: str | None = Field(None, description="Or add to them"),
) -> dict[str, Any]:
    """Free-form prep notes for a cadence meeting (context, numbers, a summary of the reports)."""
    return _packet(cadences.update_occurrence(occurrence_id, notes=notes, append=append))


@_tool(WRITE)
async def set_meeting_prep_status(
    occurrence_id: int, status: Literal["upcoming", "ready", "held", "skipped"]
) -> dict[str, Any]:
    """Mark a cadence meeting's prep ready (or the meeting held / skipped)."""
    return _packet(cadences.update_occurrence(occurrence_id, status=status))


@_tool(WRITE)
async def complete_prep_step(
    occurrence_id: int,
    step: str = Field(description="Step title (or part of it) or step_id"),
    done: bool = True,
    note: str | None = Field(None, description="Optional progress note on the step's task (what you found / produced)"),
) -> dict[str, Any]:
    """Check off a prep step: its task is marked done directly (cadence prep tasks only)."""
    task = cadences.complete_step(occurrence_id, step, done=done, note=note, source="mcp")
    return {"task_id": task["id"], "title": task["title"], "status": task["status"]}


@_tool(WRITE)
async def attach_file(
    occurrence_id: int,
    name: str = Field(description="File name with extension, e.g. ops-report-2026-10-08.md"),
    text: str | None = Field(None, description="Text content (markdown, CSV, JSON…)"),
    content_base64: str | None = Field(None, description="Or base64 bytes for a small binary (prefer `todo-agent upload` for big files)"),
    step: str | None = Field(None, description="The prep step it belongs to (title or step_id)"),
    note: str = "",
) -> dict[str, Any]:
    """Attach a file to a cadence meeting (a report, breakdown, export)."""
    if (text is None) == (content_base64 is None):
        raise Invalid("Pass exactly one of text or content_base64")
    data = text.encode() if text is not None else base64.b64decode(content_base64 or "", validate=True)
    step_id = cadences.step_task(occurrence_id, step)["step"]["id"] if step else None
    f = attachments.add(occurrence_id, name, data, None, step_id=step_id, note=note, source="mcp")
    return {k: f[k] for k in ("id", "name", "content_type", "bytes", "step_id", "url")}


@_tool(READ)
async def read_file(file_id: int) -> dict[str, Any]:
    """Read an attached text file (CSV, markdown, JSON, logs), up to 200 KB."""
    return attachments.read_text(file_id)


# ----- client sync and the source ledger (direct writes; see ledger.py) -----

SyncSource = Literal["gmail", "calendar", "jira", "slack", "teams"]


@_tool(READ)
async def check_refs(
    refs: list[str] = Field(description="Refs of the items you're about to look at, e.g. ['gmail:18c2f…', 'jira:ACME-12']"),
) -> list[dict[str, Any]]:
    """What the app already knows about each source item: unseen (new to it), pending (waiting
    in Review), rejected (the user turned it down; leave it alone), tracked (it's task #N, with
    its status: send notes/updates there, don't recreate it), deleted. Call before reading
    items in depth, so you only spend effort on what's new."""
    return ledger.check(refs)


@_tool(READ)
async def get_client_sync(customer: str = Field(description="Customer name or id")) -> dict[str, Any]:
    """Everything a sync run for one client needs: client-wide rules, its **feeds** (each one
    query against one source: a JQL, a Gmail search, Slack channels, calendar title words; with
    its own filters, rules and cursor), plus the client's projects, people, topics, cadences,
    open tasks with their refs, and the user's agents."""
    c = _customer_ref(customer)
    profile = ledger.profile(c["id"])
    open_tasks = store.list_tasks(customer_id=c["id"], limit=200)
    return {
        "customer": {"id": c["id"], "name": c["name"], "website": c.get("website")},
        **profile,
        "projects": [_project(p) for p in store.list_projects() if p["customer_id"] == c["id"]],
        "people": [_person(p) for p in people.list() if p["customer_id"] == c["id"]],
        "topics": [{"name": t["name"], "status": t["status"]} for t in hub.list_topics(c["id"])],
        "cadences": [{"id": x["id"], "name": x["name"]} for x in cadences.list(customer_id=c["id"])],
        "open_tasks": [{**_task(t, notes=0), "refs": ledger.refs_for(t["id"]) or None} for t in open_tasks],
        "agents": [{"name": a["name"], "title": a["title"], "projects": a["projects"]} for a in dispatch.list_agents()],
        "routine_prompt": f"/todo-sync {c['name']}",
    }


class SourceFilters(BaseModel):
    domains: list[str] | None = Field(None, description="gmail / calendar: the client's email domains")
    addresses: list[str] | None = Field(None, description="gmail: specific addresses")
    labels: list[str] | None = Field(None, description="gmail: labels")
    query: str | None = Field(None, description="gmail: a search query")
    title_patterns: list[str] | None = Field(None, description="calendar: words in the client's meeting titles")
    site: str | None = Field(None, description="jira: e.g. acme.atlassian.net")
    projects: list[str] | None = Field(None, description="jira: project keys")
    jql: str | None = Field(None, description="jira: the JQL for this feed")
    channels: list[str] | None = Field(None, description="slack / teams: channel names or ids")
    users: list[str] | None = Field(None, description="slack: the client's people")
    chats: list[str] | None = Field(None, description="teams: chats")


@_tool(WRITE)
async def set_client_sync(
    customer: str = Field(description="Customer name or id"),
    rules: str | None = Field(None, description="Markdown: client-wide guidance for sync runs (replaces the old rules)"),
    enabled: bool | None = None,
    default_project: str | None = Field(None, description="Project new work goes in when nothing else fits"),
) -> dict[str, Any]:
    """Client-wide sync settings. What to read lives in feeds (save_sync_feed)."""
    c = _customer_ref(customer)
    project_id: int | str | None = ""
    if default_project is not None:
        project_id = store.resolve_project(default_project)["id"] if default_project else None  # type: ignore[index]
    return ledger.save_profile(c["id"], enabled=enabled, rules=rules, default_project_id=project_id)


@_tool(WRITE)
async def save_sync_feed(
    customer: str = Field(description="Customer name or id"),
    feed: str | None = Field(None, description="To change an existing feed: its name or id. Omit to add one."),
    source: SyncSource | None = Field(None, description="For a new feed"),
    name: str | None = Field(None, description="Short and specific, e.g. 'ACME open issues', 'Escalations JQL', '#acme-shared'"),
    filters: SourceFilters | None = Field(None, description="Replaces the feed's filters (only the fields for its source)"),
    rules: str | None = Field(None, description="Markdown: guidance just for this feed, e.g. 'only P1s become tasks'"),
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Add or change one sync feed: one query against one source (a JQL, a Gmail search, a Slack
    channel set, calendar title words). A client can have several feeds per source; each keeps
    its own cursor. Show the user the filters before saving a new feed."""
    c = _customer_ref(customer)
    return ledger.save_feed(c["id"], feed=feed, source=source, name=name,
                            filters=filters.model_dump(exclude_none=True) if filters else None, rules=rules, enabled=enabled)


@_tool(WRITE)
async def remove_sync_feed(
    customer: str = Field(description="Customer name or id"), feed: str = Field(description="Feed name or id"),
) -> dict[str, Any]:
    """Delete a sync feed (what it already brought in stays)."""
    c = _customer_ref(customer)
    ledger.delete_feed(c["id"], feed)
    return {"ok": True, "feeds": [f["name"] for f in ledger.feeds(c["id"])]}


@_tool(WRITE)
async def set_sync_state(
    customer: str = Field(description="Customer name or id"),
    feed: str = Field(description="The feed this run covered: its name or id (or the source, if the client has one feed of it)"),
    cursor: str | None = Field(None, description="Where to start next time: an ISO time, or the source's own marker"),
    summary: str = Field("", description="One line, e.g. '3 threads: 1 new task, 2 notes, 4 skipped as decided'"),
) -> dict[str, Any]:
    """Record where this feed's sync stopped, after its proposals went through."""
    return ledger.set_state(_customer_ref(customer)["id"], feed, cursor=cursor, summary=summary)


@_tool(WRITE)
async def attach_task_file(
    task_id: int,
    name: str = Field(description="File name with extension"),
    text: str | None = Field(None, description="Text content"),
    content_base64: str | None = Field(None, description="Or base64 bytes, up to 15 MB"),
    note: str = "",
) -> dict[str, Any]:
    """Attach a file to a task (an email attachment, a ticket export). Doesn't change the task."""
    if (text is None) == (content_base64 is None):
        raise Invalid("Pass exactly one of text or content_base64")
    data = text.encode() if text is not None else base64.b64decode(content_base64 or "", validate=True)
    if len(data) > 15 * 1024 * 1024:
        raise Invalid("Files over 15 MB can't go through MCP")
    f = attachments.add(None, name, data, None, task_id=task_id, note=note, source="mcp")
    return {k: f[k] for k in ("id", "name", "content_type", "bytes", "url")}


def _sync_guide(customer: str = "") -> str:
    text = (TEMPLATES / "sync.md").read_text()
    return text.replace("{{customer}}", customer or "<client>")


@_tool(READ)
async def get_sync_guide(customer: str = Field("", description="The client this run is for")) -> str:
    """How to run a scheduled sync for a client (the todo-sync skill follows this). Always the
    current version, so the procedure keeps up with the app."""
    return _sync_guide(customer)


@mcp.prompt(title="Sync a client")
def sync(customer: str) -> str:
    """Sync one client from Gmail, Calendar, Jira and Slack/Teams into the todo app (or 'all')."""
    return _sync_guide(customer)


# ----- agents: setting them up from Claude Code (direct writes; setup needs the full token) -----

TEMPLATES = Path(__file__).parent / "agent_templates"


def _run_brief(r: dict[str, Any], output: int = 3000) -> dict[str, Any]:
    keys = ("id", "kind", "status", "agent", "label", "person", "task_id", "task", "branch", "pr_url", "outcome",
            "tokens", "cached_tokens", "error", "requested_at", "started_at", "finished_at")
    out = {k: r[k] for k in keys if r.get(k) not in (None, "")}
    out["machine"] = out.pop("agent", None)
    out["output_tail"] = (r.get("output") or "")[-output:]
    return out


@mcp.prompt(title="Set up a todo agent")
def setup_agent(name: str = "Maintainer", role: str = "") -> str:
    """Set up an agent: for the repo you're in (works in worktrees, opens PRs) or for general
    work (no repo). Writes its Claude Code agent file and worker skill, adds it to the todo app,
    then runs a smoke test."""
    slug = "-".join(name.lower().split()) or "maintainer"
    text = (TEMPLATES / "setup.md").read_text()
    return (text.replace("{{name}}", name).replace("{{slug}}", slug)
            .replace("{{first_name}}", name.split()[0].lower() if name.split() else slug)
            .replace("{{role_line}}", f" ({role})" if role else ""))


@_tool(READ)
async def get_agent_templates() -> dict[str, Any]:
    """The files a todo agent needs, and allowed_tools presets. A repo agent's file goes in the
    repo (.claude/agents/<name>.md); a general agent's (tasks with no repo) in ~/.claude/agents
    on its machine. Fill in the {{placeholders}}; write the todo-worker skill next to it
    (.claude/skills/todo-worker/SKILL.md) unchanged. The setup_agent prompt walks through it."""
    return {
        "repo_agent": {"path": ".claude/agents/<name>.md (in the repo)", "content": (TEMPLATES / "agent.md").read_text()},
        "general_agent": {"path": "~/.claude/agents/<name>.md (on the agent's machine)",
                          "content": (TEMPLATES / "general.md").read_text()},
        "worker_skill": {"path": ".claude/skills/todo-worker/SKILL.md (in the repo, or under ~ for a general agent)",
                         "content": (TEMPLATES / "todo-worker" / "SKILL.md").read_text()},
        "allowed_tools_presets": json.loads((TEMPLATES / "presets.json").read_text()),
        "procedure": (TEMPLATES / "setup.md").read_text(),
    }


@_tool(WRITE)
async def set_project_repo(
    project: str = Field(description="Project name or id; a new name creates the project"),
    repo_path: str = Field(description="The git checkout on the user's machines, ~ for home, e.g. ~/Work/todo-app"),
    default_branch: str = "main",
    area: Area = Field("work", description="For a new project"),
    customer: str | None = Field(None, description="For a new project: its customer (name or id)"),
) -> dict[str, Any]:
    """Link a project to its repo, so agents assigned its tasks know where the code is."""
    return _project(dispatch.set_project_repo(project, repo_path=repo_path, default_branch=default_branch,
                                              area=area, customer=customer))


@_tool(READ)
async def list_machines() -> list[dict[str, Any]]:
    """The user's machines running todo-agent: online now, platform, version (task runs need 1.2+)."""
    return [{**m, "runs_agents": supports_tasks(m["version"])} for m in agents.list()]


@_tool(READ)
async def list_agents() -> list[dict[str, Any]]:
    """The user's agents: which Claude Code agent each runs as, on which machine, for which
    projects, and its last run."""
    return [{**a, "last_run": _run_brief(a["last_run"], 300) if a["last_run"] else None} for a in dispatch.list_agents()]


@_tool(WRITE)
async def save_agent(
    executor: Literal["claude", "codex-herdr", "codex-app"] | None = Field(None, description="Shared Codex harness: Herdr or already connected Codex app / Remote; requires todo-agent 1.4"),
    name: str = Field(description="The agent's name in the app, e.g. 'Maintainer'"),
    claude_agent: str | None = Field(None, description="Its agent file: .claude/agents/<claude_agent>.md in the repo"),
    machine: str | None = Field(None, description='Machine it runs on (list_machines); "" = the only one online'),
    allowed_tools: str | None = Field(None, description="claude --allowedTools (see get_agent_templates presets)"),
    projects: list[str] | None = Field(None, description="Projects (names or ids) it works on; [] = any with a repo"),
    model: str | None = Field(None, description='Override the agent file\'s model; "" clears'),
    max_turns: int | None = None,
    auto_dispatch: bool | None = Field(None, description="Start as soon as a task is assigned (default true)"),
    title: str | None = Field(None, description="Its role, shown on its page, e.g. 'Repo maintainer'"),
) -> dict[str, Any]:
    """Create or update an agent (a person of kind agent, plus how it runs). Only the fields
    given change. Assigning a task to it then starts it on its machine."""
    agent = dispatch.save_agent(name, executor=executor, claude_agent=claude_agent, machine=machine, allowed_tools=allowed_tools,
                                projects=projects, model=model, max_turns=max_turns, auto_dispatch=auto_dispatch,
                                title=title)
    return {**agent, "last_run": _run_brief(agent["last_run"], 300) if agent["last_run"] else None}


@_tool(READ)
async def check_agent_setup(
    agent: str = Field(description="Agent name or id"),
    project: str | None = Field(None, description="Check for this project (default: its projects)"),
) -> dict[str, Any]:
    """Is everything in place for this agent to work? Each check says how to fix it."""
    return dispatch.check_setup(agent, project)


@_tool(WRITE)
async def test_agent(
    agent: str = Field(description="Agent name or id"),
    project: str | None = Field(None, description="Project to test in (default: its first with a repo)"),
) -> dict[str, Any]:
    """Queue a smoke run on the agent's machine: worktree, claude --agent, get_assignment,
    report_progress. Poll get_run until it finishes. The machine may ask the user to approve
    the agent first."""
    return _run_brief(dispatch.test_agent(agent, project))


@_tool(READ)
async def get_run(run_id: int) -> dict[str, Any]:
    """An agent (or desktop tool) run: status, outcome, branch, PR, tokens used, and its output tail."""
    return _run_brief(agents.get_run(run_id))


# ----- HTTP transport -----

def build_asgi_app() -> "McpEndpoint":
    """The /mcp endpoint. Call before startup; run ``mcp.session_manager.run()`` in the lifespan."""
    mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # Reached by LAN IP, so the localhost-only Host check would reject every real
        # client. API_TOKEN locks the endpoint down instead.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return McpEndpoint(StreamableHTTPASGIApp(mcp.session_manager), cfg.api_token)


class McpEndpoint:
    """ASGI wrapper adding the bearer-token check in front of the MCP transport."""

    def __init__(self, app: StreamableHTTPASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.token and scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode("latin-1")
            if not hmac.compare_digest(supplied, f"Bearer {self.token}"):
                response = JSONResponse({"detail": "Missing or wrong API token"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
