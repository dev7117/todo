"""REST API under /api for the web UI, the bar widget and sync scripts."""
from __future__ import annotations

import asyncio
import hmac
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from opentelemetry import trace
from pydantic import BaseModel, Field

from . import quickadd
from . import timeline
from .attachments import INLINE_TYPES
from .deps import agents, attachments, cadences, cfg, dispatch, hub, ideas, ledger, notebook, people, review, store, uploads
from .store import AREAS, CLOSED_STATUSES, STATUSES, STATUS_LABELS

router = APIRouter(prefix="/api")
tracer = trace.get_tracer("todo.api")

Area = Literal["work", "personal"]
Status = Literal["inbox", "todo", "in_progress", "waiting", "done", "cancelled"]


def require_token(authorization: str = Header(default="")) -> None:
    """Machine-facing endpoints: bearer API_TOKEN when one is configured."""
    if cfg.api_token and not hmac.compare_digest(authorization, f"Bearer {cfg.api_token}"):
        raise HTTPException(status_code=401, detail="Missing or wrong API token")


# ----- meta / settings -----

class SettingsIn(BaseModel):
    default_area: Area | None = None
    review_claude_changes: bool | None = None


@router.get("/meta")
def meta() -> dict[str, Any]:
    return {
        "areas": list(AREAS),
        "statuses": [{"id": s, "label": STATUS_LABELS[s], "closed": s in CLOSED_STATUSES} for s in STATUSES],
        "settings": store.settings(),
        "token_required": bool(cfg.api_token),
    }


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return store.settings()


@router.put("/settings")
def put_settings(body: SettingsIn) -> dict[str, Any]:
    return store.update_settings(default_area=body.default_area, review_claude_changes=body.review_claude_changes)


# ----- customers -----

class CustomerIn(BaseModel):
    name: str
    notes: str = ""
    website: str | None = None


class CustomerPatch(BaseModel):
    name: str | None = None
    notes: str | None = None
    website: str | None = None
    overview: str | None = None
    archived: bool | None = None


@router.get("/customers")
def list_customers(include_archived: bool = False) -> list[dict[str, Any]]:
    return store.list_customers(include_archived=include_archived)


@router.post("/customers", status_code=201)
def create_customer(body: CustomerIn) -> dict[str, Any]:
    return store.create_customer(body.name, body.notes, body.website)


@router.patch("/customers/{customer_id}")
def update_customer(customer_id: int, body: CustomerPatch) -> dict[str, Any]:
    return store.update_customer(customer_id, **body.model_dump(exclude_unset=True), overview_source="app")


@router.get("/customers/{customer_id}/hub")
def customer_hub(customer_id: int) -> dict[str, Any]:
    return hub.customer_hub(customer_id)


@router.post("/uploads")
async def upload_image(request: Request) -> dict[str, Any]:
    """Raw image body (Content-Type: image/png etc.), e.g. a pasted screenshot. Returns its URL."""
    return uploads.save(await request.body(), request.headers.get("content-type", ""))


@router.get("/uploads/{name}", include_in_schema=False)
def uploaded_image(name: str) -> Response:
    # Named by content hash, so it never changes.
    return FileResponse(uploads.path(name), headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/customers/{customer_id}/logo", include_in_schema=False)
def customer_logo(customer_id: int) -> Response:
    path = hub.logo_file(customer_id)
    if not path:
        raise HTTPException(status_code=404, detail="No logo")
    # The file name changes with its content, so the URL (?v=<logo>) can be cached for good.
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.put("/customers/{customer_id}/logo")
async def upload_logo(customer_id: int, request: Request) -> dict[str, Any]:
    """Raw image body (Content-Type: image/png etc.)."""
    return hub.set_logo(customer_id, await request.body(), request.headers.get("content-type", ""))


@router.post("/customers/{customer_id}/logo/fetch")
def fetch_logo(customer_id: int) -> dict[str, Any]:
    with tracer.start_as_current_span("customer.logo_fetch", attributes={"customer.id": customer_id}):
        return hub.fetch_logo(customer_id)


@router.delete("/customers/{customer_id}/logo")
def delete_logo(customer_id: int) -> dict[str, Any]:
    return hub.clear_logo(customer_id)


# ----- meetings -----

class MeetingIn(BaseModel):
    title: str
    held_on: str
    attendees: str = ""
    summary: str = ""
    decisions: str = ""
    project_id: int | None = None
    external_url: str | None = None


class MeetingPatch(BaseModel):
    prep: str | None = None
    status: Literal["scheduled", "held", "cancelled"] | None = None
    title: str | None = None
    held_on: str | None = None
    attendees: str | None = None
    summary: str | None = None
    decisions: str | None = None
    project_id: int | None = None
    external_url: str | None = None


@router.get("/customers/{customer_id}/meetings")
def list_meetings(customer_id: int, limit: int = 200) -> list[dict[str, Any]]:
    store.get_customer(customer_id)
    return hub.list_meetings(customer_id, limit=limit)


@router.post("/customers/{customer_id}/meetings", status_code=201)
def create_meeting(customer_id: int, body: MeetingIn) -> dict[str, Any]:
    return hub.create_meeting(customer_id, **body.model_dump(), source="app")


@router.get("/meetings/upcoming")
def upcoming_meetings(days: int = Query(default=7, ge=1, le=60), area: Area | None = None) -> list[dict[str, Any]]:
    # Customer meetings are work; the personal focus has none.
    return [] if area == "personal" else hub.upcoming_meetings(days=days)


@router.get("/meetings")
def meetings_between(start: str, end: str, area: Area | None = None, customer_id: int | None = None) -> list[dict[str, Any]]:
    """Meetings (scheduled and held) in a date range, for the week calendar."""
    return [] if area == "personal" else hub.meetings_between(start, end, customer_id=customer_id)


@router.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: int) -> dict[str, Any]:
    return hub.get_meeting(meeting_id)


@router.patch("/meetings/{meeting_id}")
def update_meeting(meeting_id: int, body: MeetingPatch) -> dict[str, Any]:
    return hub.update_meeting(meeting_id, **body.model_dump(exclude_unset=True))


@router.delete("/meetings/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int) -> None:
    hub.delete_meeting(meeting_id)


# ----- topics: where things stand + a timeline of updates -----

class TopicIn(BaseModel):
    name: str
    update: str = ""
    where_things_stand: str | None = None
    status: Literal["active", "watching", "resolved"] | None = None


class TopicPatch(BaseModel):
    name: str | None = None
    stand: str | None = None
    status: Literal["active", "watching", "resolved"] | None = None


class TopicUpdateIn(BaseModel):
    body: str
    happened_on: str | None = None


class MergeIn(BaseModel):
    into_id: int


@router.get("/customers/{customer_id}/topics")
def customer_topics(customer_id: int, days: int = Query(default=7, ge=0, le=365)) -> dict[str, Any]:
    """Topics updated in the last ``days`` (1 = today; 0 = every topic), busiest first."""
    return hub.topics_view(customer_id, days=days or None)


@router.post("/customers/{customer_id}/topics", status_code=201)
def add_topic(customer_id: int, body: TopicIn) -> dict[str, Any]:
    return hub.log_topic_updates(customer_id, [{"topic": body.name, "update": body.update,
                                                "where_things_stand": body.where_things_stand, "status": body.status}])[0]


@router.get("/topics/{topic_id}")
def get_topic(topic_id: int) -> dict[str, Any]:
    return hub.get_topic(topic_id)


@router.patch("/topics/{topic_id}")
def update_topic(topic_id: int, body: TopicPatch) -> dict[str, Any]:
    return hub.update_topic(topic_id, **body.model_dump(exclude_unset=True))


@router.post("/topics/{topic_id}/updates", status_code=201)
def add_topic_update(topic_id: int, body: TopicUpdateIn) -> dict[str, Any]:
    topic = hub.update_topic(topic_id)
    if not body.body.strip():
        raise HTTPException(status_code=400, detail="Write what happened")
    return hub.log_topic_updates(topic["customer_id"], [{"topic_id": topic_id, "update": body.body}], happened_on=body.happened_on)[0]


@router.delete("/topic-updates/{update_id}", status_code=204)
def delete_topic_update(update_id: int) -> None:
    hub.delete_topic_update(update_id)


@router.post("/topics/{topic_id}/merge")
def merge_topic(topic_id: int, body: MergeIn) -> dict[str, Any]:
    return hub.merge_topics(topic_id, body.into_id)


@router.delete("/topics/{topic_id}", status_code=204)
def delete_topic(topic_id: int) -> None:
    hub.delete_topic(topic_id)


# ----- links and desktop launchers -----

class LinkIn(BaseModel):
    kind: Literal["link", "launcher"]
    label: str
    url: str | None = None
    command: str | None = None
    cwd: str | None = None
    mode: Literal["terminal", "background"] = "terminal"
    agent: str | None = Field(default=None, description="Machine to run on; null = pick at run time")


class LinkPatch(BaseModel):
    label: str | None = None
    url: str | None = None
    command: str | None = None
    cwd: str | None = None
    mode: Literal["terminal", "background"] | None = None
    agent: str | None = None


@router.post("/customers/{customer_id}/links", status_code=201)
def create_link(customer_id: int, body: LinkIn) -> dict[str, Any]:
    fields = body.model_dump()
    return hub.create_link(customer_id, fields.pop("kind"), **fields)


@router.patch("/links/{link_id}")
def update_link(link_id: int, body: LinkPatch) -> dict[str, Any]:
    return hub.update_link(link_id, **body.model_dump(exclude_unset=True))


@router.delete("/links/{link_id}", status_code=204)
def delete_link(link_id: int) -> None:
    hub.delete_link(link_id)


class LinkOrderIn(BaseModel):
    ids: list[int]


@router.put("/customers/{customer_id}/links/order", status_code=204)
def order_links(customer_id: int, body: LinkOrderIn) -> None:
    hub.reorder_links(body.ids)


# ----- machines (todo-agent) and runs -----

class RunIn(BaseModel):
    agent: str | None = Field(default=None, description="Machine name; default: the tool's machine or the only one online")


@router.get("/agents")
def list_agents() -> list[dict[str, Any]]:
    return agents.list()


@router.delete("/agents/{name}", status_code=204)
def delete_agent(name: str) -> None:
    agents.delete(name)


@router.post("/links/{link_id}/run", status_code=201)
def run_launcher(link_id: int, body: RunIn) -> dict[str, Any]:
    with tracer.start_as_current_span("launcher.run", attributes={"link.id": link_id}) as span:
        run = agents.request_run(link_id, body.agent)
        span.set_attributes({"run.id": run["id"], "run.agent": run["agent"], "run.mode": run["mode"]})
    return run


@router.get("/links/{link_id}/runs")
def launcher_runs(link_id: int, limit: int = 10) -> list[dict[str, Any]]:
    return agents.list_runs(link_id=link_id, limit=limit)


@router.get("/customers/{customer_id}/runs")
def customer_runs(customer_id: int, limit: int = 20) -> list[dict[str, Any]]:
    return agents.list_runs(customer_id=customer_id, limit=limit)


@router.get("/runs/{run_id}")
def get_run(run_id: int) -> dict[str, Any]:
    return agents.get_run(run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int) -> dict[str, Any]:
    """Withdraw a queued run, or stop an agent mid-task."""
    return dispatch.stop(run_id)


# ----- agents working tasks (todo_app/dispatch.py) -----

class DispatchIn(BaseModel):
    message: str | None = Field(default=None, description="Your reply / instructions; resumes its last session")


class AgentIn(BaseModel):
    executor: Literal["claude", "codex-herdr", "codex-app"] | None = None
    claude_agent: str | None = None
    machine: str | None = None
    model: str | None = None
    max_turns: int | None = None
    allowed_tools: str | None = None
    projects: list[int] | None = None
    auto_dispatch: bool | None = None


@router.post("/tasks/{task_id}/dispatch", status_code=201)
def dispatch_task(task_id: int, body: DispatchIn) -> dict[str, Any]:
    """Start (or send back) the task's agent."""
    with tracer.start_as_current_span("agent.dispatch", attributes={"task.id": task_id}) as span:
        run = dispatch.enqueue(task_id, message=body.message, source="app")
        span.set_attributes({"run.id": run["id"], "run.agent": run["agent"]})
    return run


@router.get("/tasks/{task_id}/runs")
def task_runs(task_id: int, limit: int = 10) -> list[dict[str, Any]]:
    return agents.list_runs(task_id=task_id, limit=limit)


@router.get("/agent-profiles")
def agent_profiles() -> list[dict[str, Any]]:
    return dispatch.list_agents()


@router.get("/people/{person_id}/agent")
def get_agent_profile(person_id: int) -> dict[str, Any]:
    return dispatch.get_agent(person_id)


@router.put("/people/{person_id}/agent")
def put_agent_profile(person_id: int, body: AgentIn) -> dict[str, Any]:
    person = people.get(person_id)
    return dispatch.save_agent(person["name"], person_id=person_id, **body.model_dump(exclude_unset=True))


@router.get("/people/{person_id}/agent/check")
def check_agent(person_id: int) -> dict[str, Any]:
    return dispatch.check_setup(person_id)


@router.post("/people/{person_id}/agent/test", status_code=201)
def test_agent(person_id: int) -> dict[str, Any]:
    return dispatch.test_agent(person_id)


# The agent's side. Token-protected: only your machines can take runs or report on them.

class PollIn(BaseModel):
    name: str
    platform: str
    version: str = ""
    accept_tasks: bool = Field(default=True, description="False while the machine is at its agent-run limit")


class ReportIn(BaseModel):
    launch: dict[str, Any] | None = None
    name: str
    status: Literal["running", "succeeded", "failed", "declined"] | None = None
    exit_code: int | None = None
    output: str | None = None
    append: bool = True
    error: str | None = None
    session_id: str | None = None
    branch: str | None = None
    tokens: int | None = None
    cached_tokens: int | None = None


@router.post("/agent/poll", dependencies=[Depends(require_token)])
async def agent_poll(body: PollIn) -> dict[str, Any]:
    """Long poll: returns a run as soon as one is queued for this machine, else null after ~25s."""
    for tick in range(25):
        if tick % 10 == 0:
            agents.heartbeat(body.name, body.platform, body.version)
        run = dispatch.claim(body.name, version=body.version, accept_tasks=body.accept_tasks)
        if run:
            return {"run": run}
        await asyncio.sleep(1)
    return {"run": None}


@router.post("/agent/ping", dependencies=[Depends(require_token)])
def agent_ping(body: PollIn) -> dict[str, Any]:
    agents.heartbeat(body.name, body.platform, body.version)
    return {"ok": True}


@router.post("/agent/runs/{run_id}", dependencies=[Depends(require_token)])
def agent_report(run_id: int, body: ReportIn) -> dict[str, Any]:
    run = dispatch.report(
        run_id, body.name, status=body.status, exit_code=body.exit_code, output=body.output,
        append=body.append, error=body.error, session_id=body.session_id, branch=body.branch,
        tokens=body.tokens, cached_tokens=body.cached_tokens, launch=body.launch,
    )
    return {"status": run["status"], "outcome": run.get("outcome")}  # 'cancelled' tells the machine to stop


# ----- client sync (todo_app/ledger.py): what a scheduled sync reads for this customer -----

class SyncIn(BaseModel):
    enabled: bool | None = None
    rules: str | None = None
    default_project_id: int | None = None


class FeedIn(BaseModel):
    source: Literal["gmail", "calendar", "jira", "slack", "teams"] | None = None
    name: str | None = None
    filters: dict[str, Any] | None = None
    rules: str | None = None
    enabled: bool | None = None


@router.get("/customers/{customer_id}/sync")
def get_customer_sync(customer_id: int) -> dict[str, Any]:
    return ledger.profile(customer_id)


@router.put("/customers/{customer_id}/sync")
def put_customer_sync(customer_id: int, body: SyncIn) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    return ledger.save_profile(
        customer_id, enabled=fields.get("enabled"), rules=fields.get("rules"),
        default_project_id=fields["default_project_id"] if "default_project_id" in fields else "",
    )


@router.post("/customers/{customer_id}/sync/feeds", status_code=201)
def add_sync_feed(customer_id: int, body: FeedIn) -> dict[str, Any]:
    return ledger.save_feed(customer_id, **body.model_dump(exclude_unset=True))


@router.patch("/customers/{customer_id}/sync/feeds/{feed_id}")
def update_sync_feed(customer_id: int, feed_id: int, body: FeedIn) -> dict[str, Any]:
    return ledger.save_feed(customer_id, feed=feed_id, **body.model_dump(exclude_unset=True, exclude={"source"}))


@router.delete("/customers/{customer_id}/sync/feeds/{feed_id}", status_code=204)
def delete_sync_feed(customer_id: int, feed_id: int) -> None:
    ledger.delete_feed(customer_id, feed_id)


@router.delete("/customers/{customer_id}", status_code=204)
def delete_customer(customer_id: int) -> None:
    store.delete_customer(customer_id)


# ----- projects -----

class ProjectIn(BaseModel):
    name: str
    area: Area
    customer: str | int | None = Field(default=None, description="Customer id or name; a new name creates it")
    description: str = ""
    repo_path: str | None = Field(default=None, description="The git checkout on your machines, e.g. ~/Work/todo-app")
    default_branch: str | None = None


class ProjectPatch(BaseModel):
    name: str | None = None
    area: Area | None = None
    customer: str | int | None = Field(default=None, description='Customer id or name; null or "" removes it')
    description: str | None = None
    archived: bool | None = None
    repo_path: str | None = None
    default_branch: str | None = None


@router.get("/projects")
def list_projects(include_archived: bool = False) -> list[dict[str, Any]]:
    return store.list_projects(include_archived=include_archived)


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn) -> dict[str, Any]:
    return store.create_project(body.name, body.area, body.customer, body.description,
                                repo_path=body.repo_path, default_branch=body.default_branch)


@router.patch("/projects/{project_id}")
def update_project(project_id: int, body: ProjectPatch) -> dict[str, Any]:
    return store.update_project(project_id, **body.model_dump(exclude_unset=True))


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int) -> None:
    store.delete_project(project_id)


# ----- tasks -----

class TaskIn(BaseModel):
    title: str
    notes: str = ""
    status: Status = "todo"
    area: Area | None = None
    project_id: int | None = None
    priority: int = Field(default=0, ge=0, le=3)
    due_on: str | None = None
    today: bool = False
    waiting_on: str | None = None
    external_url: str | None = None
    assignee_id: int | None = None


class TaskPatch(BaseModel):
    title: str | None = None
    notes: str | None = None
    status: Status | None = None
    area: Area | None = None
    project_id: int | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    due_on: str | None = None
    today: bool | None = None
    waiting_on: str | None = None
    external_url: str | None = None
    assignee_id: int | None = Field(default=None, description="Person doing it; null = you")
    note: str | None = Field(default=None, description="Progress note logged with the change")


class QuickIn(BaseModel):
    text: str
    notes: str = ""
    source: str = "app"
    area: Area | None = Field(default=None, description="The caller's focus; used unless the text has @area/#project")
    project_id: int | None = Field(default=None, description="The caller's project filter, same rule")


class UpdateIn(BaseModel):
    body: str
    status: Status | None = None


class OrderIn(BaseModel):
    ids: list[int]


@router.get("/tasks")
def list_tasks(
    status: list[Status] | None = Query(default=None),
    area: Area | None = None,
    project_id: int | None = None,
    no_project: bool = False,
    customer_id: int | None = None,
    no_customer: bool = False,
    assignee_id: int | None = None,
    mine: bool = False,
    delegated: bool = False,
    following: int | None = None,
    today: bool | None = None,
    source: str | None = None,
    include_closed: bool = False,
    closed_since: str | None = None,
    top_level: bool = False,
    limit: int = Query(default=500, le=2000),
) -> list[dict[str, Any]]:
    return store.list_tasks(
        status=list(status) if status else None,
        area=area,
        project_id=project_id,
        no_project=no_project,
        customer_id=customer_id,
        no_customer=no_customer,
        assignee_id=assignee_id,
        mine=mine,
        delegated=delegated,
        following=following,
        today=today,
        source=source,
        include_closed=include_closed,
        closed_since=closed_since,
        top_level=top_level,
        limit=limit,
    )


@router.get("/today")
def today(area: Area | None = None, project_id: int | None = None, customer_id: int | None = None) -> dict[str, Any]:
    return store.today_view(area, project_id, customer_id)


class MoveIn(BaseModel):
    order: list[int] = Field(description="The column's task ids, top to bottom, including the moved task")
    status: str | None = None


@router.post("/tasks/{task_id}/move")
def move_task(task_id: int, body: MoveIn) -> dict[str, Any]:
    return store.move_task(task_id, body.order, status=body.status)


class GroupIn(BaseModel):
    task_id: int = Field(description="The task being dropped")
    onto_id: int = Field(description="The task (or group) it was dropped on")
    title: str | None = Field(default=None, description="Name for a new group; suggested when omitted")


@router.post("/tasks/group")
def group_tasks(body: GroupIn) -> dict[str, Any]:
    """Like making an iOS folder: returns the group (parent task)."""
    return store.group_tasks(body.task_id, body.onto_id, title=body.title)


@router.post("/tasks/{task_id}/ungroup")
def ungroup_task(task_id: int) -> dict[str, Any]:
    return store.ungroup(task_id)


@router.put("/today/order")
def order_today(body: OrderIn) -> dict[str, Any]:
    store.reorder_today(body.ids)
    return {"ok": True}


@router.get("/counts")
def counts(area: Area | None = None) -> dict[str, int]:
    return {**store.counts(area), "ideas": ideas.count(area)}


@router.get("/bar")
def bar(area: Area | None = None, project_id: int | None = None, customer_id: int | None = None) -> dict[str, Any]:
    """The bar widget's view of its focus. Also lists what it can narrow to in that area."""
    state = store.bar_state(area, project_id, customer_id)
    # Today's customer meetings (work), for the panel.
    state["meetings"] = [] if area == "personal" else [
        {k: m[k] for k in ("id", "customer_id", "customer", "title", "starts_at", "prep")}
        for m in hub.upcoming_meetings(days=0, customer_id=customer_id)
        if project_id is None or m["project_id"] in (None, project_id)
    ]
    projects = [p for p in store.list_projects() if not area or p["area"] == area]
    state["filters"] = {
        "customers": [
            {"id": c["id"], "name": c["name"], "open": c["open_count"]}
            for c in store.list_customers() if area != "personal"
        ],
        "projects": [
            {"id": p["id"], "name": p["name"], "customer_id": p["customer_id"], "area": p["area"], "open": p["open_count"]}
            for p in projects
        ],
    }
    return state


@router.get("/search")
def search(q: str, include_closed: bool = True, area: Area | None = None, limit: int = 30) -> list[dict[str, Any]]:
    return store.search(q, include_closed=include_closed, area=area, limit=limit)


def _task_full(task_id: int) -> dict[str, Any]:
    """A task as its page shows it: history, notebook, and the meetings it came up in."""
    return {
        **store.get_task(task_id, with_updates=True),
        "meetings": hub.meetings_for_task(task_id),
        "blocks": notebook.blocks("task", task_id),
        "files": attachments.for_task(task_id),
        "refs": ledger.refs_for(task_id),
    }


@router.get("/tasks/{task_id}/timeline")
def task_timeline(task_id: int) -> list[dict[str, Any]]:
    return timeline.build(store, hub, task_id)


@router.get("/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    return _task_full(task_id)


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn) -> dict[str, Any]:
    return store.create_task(body.model_dump(), source="app")


@router.post("/tasks/quick", status_code=201)
def quick_add(body: QuickIn) -> dict[str, Any]:
    source = body.source if body.source in ("app", "intake") else "app"
    return quickadd.quick_add(
        store, body.text, source=source, notes=body.notes, area=body.area, project_id=body.project_id,
        ideas=ideas, people=people,
    )


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, body: TaskPatch) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    note = fields.pop("note", None)
    store.update_task(task_id, fields, source="app", note=note)
    return _task_full(task_id)


@router.post("/tasks/{task_id}/updates", status_code=201)
def add_update(task_id: int, body: UpdateIn) -> dict[str, Any]:
    store.add_update(task_id, body.body, source="app", status=body.status)
    return _task_full(task_id)


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int) -> None:
    store.delete_task(task_id)


# ----- ingest (sync scripts) -----

class IngestTask(BaseModel):
    external_id: str = Field(description="The task's id in the source system (ticket number, etc.)")
    title: str | None = None
    notes: str | None = None
    status: Status | None = None
    area: Area | None = None
    project: str | int | None = Field(default=None, description="Project name or id")
    priority: int | None = Field(default=None, ge=0, le=3)
    due_on: str | None = None
    external_url: str | None = None
    note: str | None = Field(default=None, description="Logged in the task's history")


class IngestIn(BaseModel):
    source: str = Field(description="Short name of the source, e.g. 'jira-acme' or 'zendesk'")
    tasks: list[IngestTask]
    close_missing: bool = Field(
        default=False,
        description="Mark this source's open tasks that aren't in the batch as done "
        "(use when the batch is the complete list of open items)",
    )


@router.post("/ingest", dependencies=[Depends(require_token)])
def ingest(body: IngestIn) -> dict[str, Any]:
    """Idempotent upsert keyed by (source, external_id). Safe to run on a schedule."""
    source = body.source.strip()
    if not source or source in ("app", "intake", "mcp"):
        raise HTTPException(status_code=400, detail="source must name the external system")
    created: list[int] = []
    updated: list[int] = []
    closed: list[int] = []
    with tracer.start_as_current_span(
        "ingest.batch", attributes={"ingest.source": source, "ingest.count": len(body.tasks)}
    ) as span, store.tx():
        for item in body.tasks:
            fields = item.model_dump(exclude_unset=True, exclude={"external_id", "project", "note"})
            if "project" in item.model_fields_set:
                project = store.resolve_project(item.project)
                fields["project_id"] = project["id"] if project else None
            task, was_created = store.upsert_external(source, item.external_id, fields, note=item.note)
            (created if was_created else updated).append(task["id"])
        if body.close_missing:
            seen = {item.external_id for item in body.tasks}
            for task in store.list_tasks(source=source):
                if task["external_id"] and task["external_id"] not in seen:
                    store.update_task(
                        task["id"], {"status": "done"}, source=source, note="No longer open in the source"
                    )
                    closed.append(task["id"])
        span.set_attributes(
            {"ingest.created": len(created), "ingest.updated": len(updated), "ingest.closed": len(closed)}
        )
    return {"created": created, "updated": updated, "closed": closed}


# ----- proposals (Claude's changes awaiting review) -----

class DecideIn(BaseModel):
    approve: list[int] | Literal["all", "none"] = Field(description="Change ids to apply; the rest are rejected")
    edits: dict[int, dict[str, Any]] = Field(default_factory=dict, description="Per change: title/due_on/project_id/priority")


@router.get("/proposals")
def list_proposals(
    status: Literal["pending", "applied", "partial", "rejected"] | None = None,
    limit: int = 50,
    area: Area | None = None,
) -> list[dict[str, Any]]:
    return review.list(status=status, limit=limit, area=area)


@router.get("/proposals/{changeset_id}")
def get_proposal(changeset_id: int) -> dict[str, Any]:
    return review.get(changeset_id)


@router.post("/proposals/{changeset_id}/decide")
def decide_proposal(changeset_id: int, body: DecideIn) -> dict[str, Any]:
    approve = [] if body.approve == "none" else body.approve
    with tracer.start_as_current_span("proposal.decide", attributes={"proposal.id": changeset_id}) as span:
        result = review.decide(changeset_id, approve=approve, edits=body.edits)
        span.set_attribute("proposal.status", result["status"])
    return result


# ----- ideas (not-yet-tasks; never on boards / today / the bar) -----

class BlockIn(BaseModel):
    title: str = ""
    body: str = ""
    kind: Literal["note", "subtask"] = "note"
    after_id: int | None = Field(default=None, description="Insert after this block (0 = top); default: at the end")


class BlockPatch(BaseModel):
    title: str | None = None
    body: str | None = None
    collapsed: bool | None = None
    kind: Literal["note", "subtask"] | None = None
    done: bool | None = None


class IdeaIn(BaseModel):
    title: str
    summary: str = ""
    blocks: list[BlockIn] = Field(default_factory=list)
    area: Area | None = None
    customer_id: int | None = None
    project_id: int | None = None


class IdeaPatch(BaseModel):
    title: str | None = None
    summary: str | None = None
    area: Area | None = None
    customer_id: int | None = None
    project_id: int | None = None
    status: Literal["open", "dropped"] | None = None


class PromoteIn(BaseModel):
    note_block_ids: list[int] = Field(default_factory=list, description="Idea blocks to bring over as notes, not subtasks")
    title: str | None = None
    project_id: int | None = None
    area: Area | None = None
    due_on: str | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    today: bool | None = None


@router.get("/ideas")
def list_ideas(
    status: Literal["open", "promoted", "dropped"] | None = "open",
    area: Area | None = None,
    customer_id: int | None = None,
    project_id: int | None = None,
    q: str | None = None,
    limit: int = Query(default=500, le=2000),
) -> list[dict[str, Any]]:
    return ideas.list(status=status, area=area, customer_id=customer_id, project_id=project_id, query=q, limit=limit)


@router.post("/ideas", status_code=201)
def create_idea(body: IdeaIn) -> dict[str, Any]:
    return ideas.create(body.title, summary=body.summary, blocks=[b.model_dump() for b in body.blocks],
                        area=body.area, customer_id=body.customer_id, project_id=body.project_id, source="app")


@router.get("/ideas/{idea_id}")
def get_idea(idea_id: int) -> dict[str, Any]:
    return ideas.get(idea_id, with_blocks=True)


# ----- notebooks (blocks on ideas and tasks; subtasks on tasks) -----

@router.post("/ideas/{idea_id}/blocks", status_code=201)
def add_idea_block(idea_id: int, body: BlockIn) -> dict[str, Any]:
    return notebook.add("idea", idea_id, title=body.title, body=body.body, kind=body.kind, after_id=body.after_id)


@router.post("/people/{person_id}/blocks", status_code=201)
def add_person_block(person_id: int, body: BlockIn) -> dict[str, Any]:
    return notebook.add("person", person_id, title=body.title, body=body.body, after_id=body.after_id)


@router.put("/people/{person_id}/blocks/order")
def order_person_blocks(person_id: int, body: OrderIn) -> list[dict[str, Any]]:
    return notebook.reorder("person", person_id, body.ids)


@router.post("/tasks/{task_id}/blocks", status_code=201)
def add_task_block(task_id: int, body: BlockIn) -> dict[str, Any]:
    return notebook.add("task", task_id, title=body.title, body=body.body, kind=body.kind, after_id=body.after_id)


@router.put("/ideas/{idea_id}/blocks/order")
def order_idea_blocks(idea_id: int, body: OrderIn) -> list[dict[str, Any]]:
    return notebook.reorder("idea", idea_id, body.ids)


@router.put("/tasks/{task_id}/blocks/order")
def order_task_blocks(task_id: int, body: OrderIn) -> list[dict[str, Any]]:
    return notebook.reorder("task", task_id, body.ids)


@router.patch("/blocks/{block_id}")
def update_block(block_id: int, body: BlockPatch) -> dict[str, Any]:
    return notebook.update(block_id, **body.model_dump(exclude_unset=True))


@router.delete("/blocks/{block_id}", status_code=204)
def delete_block(block_id: int) -> None:
    notebook.delete(block_id)


@router.patch("/ideas/{idea_id}")
def update_idea(idea_id: int, body: IdeaPatch) -> dict[str, Any]:
    ideas.update(idea_id, **body.model_dump(exclude_unset=True))
    return ideas.get(idea_id, with_blocks=True)


@router.delete("/ideas/{idea_id}", status_code=204)
def delete_idea(idea_id: int) -> None:
    ideas.delete(idea_id)


@router.post("/ideas/{idea_id}/promote", status_code=201)
def promote_idea(idea_id: int, body: PromoteIn) -> dict[str, Any]:
    """Turn the idea into a task (in the app, the user's own action: no review needed)."""
    with tracer.start_as_current_span("idea.promote", attributes={"idea.id": idea_id}):
        fields = body.model_dump(exclude_none=True, exclude={"note_block_ids"})
        task = ideas.promote(idea_id, fields, source="app", note_block_ids=body.note_block_ids)
        return {**task, "kind": "task"}


# ----- people -----

class PersonIn(BaseModel):
    name: str
    email: str | None = None
    title: str = ""
    customer_id: int | None = Field(default=None, description="Their employer; null = your side")
    area: Area = "work"
    notes: str = ""
    kind: Literal["human", "agent"] = "human"


class PersonPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    title: str | None = None
    customer_id: int | None = None
    area: Area | None = None
    notes: str | None = None
    archived: bool | None = None


class FollowersIn(BaseModel):
    person_ids: list[int]


@router.get("/people")
def list_people(area: Area | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
    return people.list(area=area, include_archived=include_archived)


@router.post("/people", status_code=201)
def create_person(body: PersonIn) -> dict[str, Any]:
    return people.create(**body.model_dump())


@router.get("/people/{person_id}")
def person_view(person_id: int, area: Area | None = None) -> dict[str, Any]:
    """The person page: follow-ups, their tasks, shared work, recent wins, meetings, 1:1 notes."""
    return people.view(person_id, area=area)


@router.patch("/people/{person_id}")
def update_person(person_id: int, body: PersonPatch) -> dict[str, Any]:
    return people.update(person_id, **body.model_dump(exclude_unset=True))


@router.delete("/people/{person_id}", status_code=204)
def delete_person(person_id: int) -> None:
    people.delete(person_id)


@router.put("/tasks/{task_id}/followers")
def set_task_followers(task_id: int, body: FollowersIn) -> dict[str, Any]:
    """Who follows the task (it stays the assignee's)."""
    store.set_followers(task_id, body.person_ids, source="app")
    return _task_full(task_id)


# ----- cadences: recurring meetings and their prep -----


class CadenceIn(BaseModel):
    customer_id: int
    name: str
    schedule: dict[str, Any] = Field(description='{"cron": "0 9 * * 4"} | {"nth": 2, "weekday": 1, "time": "10:00"} | {"calendar": "title text"}')
    purpose: str | None = None
    agenda: list[Any] | None = None
    prep_days: int | None = None
    duration_min: int | None = None
    project_id: int | None = None


class CadencePatch(BaseModel):
    name: str | None = None
    schedule: dict[str, Any] | None = None
    purpose: str | None = None
    agenda: list[Any] | None = None
    prep_days: int | None = None
    duration_min: int | None = None
    project_id: int | None = None
    active: bool | None = None


class StepsIn(BaseModel):
    steps: list[dict[str, Any]]


class PrepareIn(BaseModel):
    starts_at: str | None = None


class OccurrencePatch(BaseModel):
    notes: str | None = None
    status: Literal["upcoming", "ready", "held", "skipped"] | None = None


class PointsIn(BaseModel):
    points: str
    title: str | None = None


@router.get("/cadences")
def list_cadences(customer_id: int | None = None) -> list[dict[str, Any]]:
    return cadences.list(customer_id=customer_id)


@router.post("/cadences")
def create_cadence(body: CadenceIn) -> dict[str, Any]:
    fields = body.model_dump(exclude={"customer_id", "name", "schedule"}, exclude_none=True)
    return cadences.create(body.customer_id, body.name, body.schedule, **fields)


@router.get("/cadences/{cadence_id}")
def get_cadence(cadence_id: int) -> dict[str, Any]:
    return cadences.get(cadence_id)


@router.patch("/cadences/{cadence_id}")
def update_cadence(cadence_id: int, body: CadencePatch) -> dict[str, Any]:
    cadence = cadences.update(cadence_id, **body.model_dump(exclude_unset=True))
    cadences.ensure()  # a shorter schedule or longer prep window may bring a meeting in now
    return cadences.get(cadence_id)


@router.delete("/cadences/{cadence_id}", status_code=204)
def delete_cadence(cadence_id: int) -> None:
    cadences.delete(cadence_id)


@router.put("/cadences/{cadence_id}/steps")
def set_cadence_steps(cadence_id: int, body: StepsIn) -> list[dict[str, Any]]:
    return cadences.set_steps(cadence_id, body.steps)


@router.post("/cadences/{cadence_id}/prepare")
def prepare_cadence(cadence_id: int, body: PrepareIn) -> dict[str, Any]:
    """Make the prep for a meeting now (the next one, or ``starts_at``), ahead of the window."""
    return cadences.prepare(cadence_id, body.starts_at)


@router.get("/occurrences/{occurrence_id}")
def get_occurrence(occurrence_id: int) -> dict[str, Any]:
    return cadences.occurrence(occurrence_id)


@router.patch("/occurrences/{occurrence_id}")
def update_occurrence(occurrence_id: int, body: OccurrencePatch) -> dict[str, Any]:
    return cadences.update_occurrence(occurrence_id, **body.model_dump(exclude_unset=True))


@router.put("/occurrences/{occurrence_id}/topics/{topic_id}")
def set_topic_points(occurrence_id: int, topic_id: int, body: PointsIn) -> dict[str, Any]:
    return cadences.set_points(occurrence_id, topic_id, body.points)


@router.post("/occurrences/{occurrence_id}/topics")
def add_occurrence_topic(occurrence_id: int, body: PointsIn) -> dict[str, Any]:
    if not (body.title or "").strip():
        raise HTTPException(status_code=400, detail="A topic needs a title")
    return cadences.set_points(occurrence_id, body.title, body.points)


@router.delete("/occurrence-topics/{topic_id}", status_code=204)
def delete_occurrence_topic(topic_id: int) -> None:
    cadences.remove_topic(topic_id)


@router.post("/occurrences/{occurrence_id}/steps/{step_id}/run")
def run_step(occurrence_id: int, step_id: int, body: RunIn) -> dict[str, Any]:
    """Run a prep step's desktop tool; its output files get attached to this meeting."""
    step = cadences.step_task(occurrence_id, step_id)["step"]
    if not step["link_id"]:
        raise HTTPException(status_code=400, detail="That step has no desktop tool")
    return agents.request_run(step["link_id"], body.agent, occurrence_id=occurrence_id, step_id=step_id)


@router.post("/occurrences/{occurrence_id}/files")
async def upload_file(
    occurrence_id: int, request: Request, name: str, step_id: int | None = None, note: str = "", source: str = "app"
) -> dict[str, Any]:
    """Raw file body; ``name`` is its file name. From the app, todo-agent or scripts."""
    data = await request.body()
    return attachments.add(occurrence_id, name, data, request.headers.get("content-type"),
                           step_id=step_id, note=note, source=source)


@router.post("/tasks/{task_id}/files")
async def upload_task_file(task_id: int, request: Request, name: str, note: str = "") -> dict[str, Any]:
    """Raw file body; ``name`` is its file name."""
    return attachments.add(None, name, await request.body(), request.headers.get("content-type"),
                           task_id=task_id, note=note, source="app")


@router.post("/agent/runs/{run_id}/files", dependencies=[Depends(require_token)])
async def agent_run_file(run_id: int, request: Request, machine: str, name: str) -> dict[str, Any]:
    """todo-agent attaching what an agent produced (its outputs/ folder) to the run's task."""
    return dispatch.attach_output(run_id, machine, name, await request.body(), request.headers.get("content-type"))


@router.get("/files/{attachment_id}/{name}", include_in_schema=False)
def download_file(attachment_id: int, name: str) -> Response:
    path, row = attachments.path(attachment_id)
    inline = row["content_type"] in INLINE_TYPES
    return FileResponse(
        path,
        media_type=row["content_type"] if inline else "application/octet-stream",
        filename=row["name"],
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
    )


@router.delete("/files/{attachment_id}", status_code=204)
def delete_file(attachment_id: int) -> None:
    attachments.delete(attachment_id)
