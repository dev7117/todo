"""The worker MCP endpoint, /mcp/agent: what an agent working a task can do.

todo-agent starts `claude -p --agent <name>` with an MCP config pointing here and a bearer
token issued when it claimed the run. The token only works while that run is live, and every
tool acts on that run's task (or one of its children). It's a separate server from /mcp, so
an agent never even sees the user's full toolset; the API token isn't accepted here.
"""
from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, Literal, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from opentelemetry import trace
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from . import telemetry
from .deps import dispatch
from .store import Invalid, NotFound

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])
tracer = trace.get_tracer("todo.mcp.agent")

INSTRUCTIONS = """\
You're working one task from the user's todo app, on their machine. These
tools are your line to the user and only work for this task. Start with get_assignment; use the
todo-worker skill for the loop. Finish with request_review (PR + summary) or ask (a question
for the user), then stop. Done is the user's call: they approve it in the app.

get_assignment tells you whether this is headless work or a visible Codex/Herdr session.
In headless work the user only sees the task. In Herdr they can join your terminal. Put the
deliverable there: write-ups and drafts as notebook blocks (add_note), files with attach_file or
saved in ./outputs/ (attached when the run ends). Never point them to a local path.
"""

worker = MCPServer(name="todo-agent", title="Todo (agent)", instructions=INSTRUCTIONS)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def _run(ctx: Context) -> dict[str, Any]:
    request = ctx.request_context.request
    header = request.headers.get("authorization", "") if request is not None else ""
    run = dispatch.run_for_token(header.removeprefix("Bearer ").strip())
    if not run:
        raise Invalid("This run is over (or the token is wrong). Stop.")
    return run


def _tool(annotations: ToolAnnotations) -> Callable[[F], F]:
    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            with tracer.start_as_current_span("mcp.agent_tool_call", attributes={"mcp.tool.name": fn.__name__}) as span:
                try:
                    return await fn(*args, **kwargs)
                except (NotFound, Invalid, ValueError) as exc:
                    span.set_attribute("mcp.tool.outcome", "error")
                    raise ToolError(str(exc)) from exc
                except Exception as exc:
                    telemetry.record_error(span, exc)
                    raise

        worker.tool(annotations=annotations)(wrapper)
        return fn

    return decorate


@_tool(READ)
async def get_assignment(ctx: Context) -> dict[str, Any]:
    """Your task: title, notes (the spec), notebook and subtasks, history, what the user said
    since your last run, and the repo / branch you're on. Call this first."""
    return dispatch.assignment(_run(ctx))


@_tool(WRITE)
async def report_progress(
    ctx: Context,
    note: str = Field(description="What's done, what's next, anything surprising. A few plain sentences."),
    task_id: int | None = Field(None, description="One of your task's children; default your task"),
) -> dict[str, Any]:
    """Log progress in the task's history (the user reads it in the app)."""
    return dispatch.progress(_run(ctx), note, task_id)


@_tool(WRITE)
async def set_status(
    ctx: Context,
    status: Literal["in_progress", "waiting"],
    waiting_on: str | None = Field(None, description='When waiting: on what, e.g. "CI" or "upstream fix"'),
    note: str | None = None,
    task_id: int | None = None,
) -> dict[str, Any]:
    """Move the task between in progress and waiting. Done goes through request_review."""
    return dispatch.set_status(_run(ctx), status, waiting_on, note, task_id)


@_tool(WRITE)
async def add_subtask(
    ctx: Context, title: str, body: str = Field("", description="Markdown details"), task_id: int | None = None,
) -> dict[str, Any]:
    """Add a subtask (checkbox) to your task: your plan, one step each."""
    return dispatch.add_subtask(_run(ctx), title, body, task_id)


@_tool(WRITE)
async def check_subtask(ctx: Context, block_id: int, done: bool = True) -> dict[str, Any]:
    """Mark a subtask done (done=false reopens it)."""
    return dispatch.check_subtask(_run(ctx), block_id, done)


@_tool(WRITE)
async def add_note(
    ctx: Context,
    title: str = Field(description="Short, e.g. 'Findings', 'Draft reply', 'Plan'"),
    body: str = Field(description="Markdown: the write-up itself"),
    task_id: int | None = None,
) -> dict[str, Any]:
    """Add a notebook block to the task: where write-ups, drafts and findings go (the user reads
    them on the task page). Prefer this to long progress notes."""
    return dispatch.add_note(_run(ctx), title, body, task_id)


@_tool(WRITE)
async def update_note(
    ctx: Context,
    block_id: int,
    body: str = Field(description="Markdown"),
    append: bool = Field(False, description="Add to the end instead of replacing"),
    title: str | None = None,
) -> dict[str, Any]:
    """Revise a notebook block you wrote (not the user's)."""
    return dispatch.update_note(_run(ctx), block_id, body, append=append, title=title)


@_tool(WRITE)
async def attach_file(
    ctx: Context,
    name: str = Field(description="File name with extension, e.g. report.pdf"),
    text: str | None = Field(None, description="Text content (markdown, CSV, JSON…)"),
    content_base64: str | None = Field(None, description="Or base64 bytes (PDF, images…), up to 15 MB"),
    note: str = Field("", description="One line on what it is"),
    task_id: int | None = None,
) -> dict[str, Any]:
    """Attach a file to the task. For a file you made on disk, saving it in ./outputs/ also
    works: everything there is attached when the run ends."""
    return dispatch.attach_file(_run(ctx), name, text=text, content_base64=content_base64, note=note, task_id=task_id)


@_tool(WRITE)
async def ask(
    ctx: Context,
    question: str = Field(description="A concrete question, with the options you see and your recommendation"),
) -> dict[str, Any]:
    """Blocked on a decision only the user can make: the task waits on them with your question.
    Stop after calling this; their answer resumes this session."""
    return dispatch.ask(_run(ctx), question)


@_tool(WRITE)
async def request_review(
    ctx: Context,
    summary: str = Field(description="What you did and how you verified it"),
    pr_url: str | None = Field(None, description="The pull request URL"),
) -> dict[str, Any]:
    """The work is ready: records the PR, leaves the task waiting on the user's review, and
    proposes marking it done (they approve in the app). Stop after calling this."""
    return dispatch.request_review(_run(ctx), summary, pr_url)


def build_worker_app() -> "WorkerEndpoint":
    worker.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return WorkerEndpoint(StreamableHTTPASGIApp(worker.session_manager))


class WorkerEndpoint:
    """Rejects anything without a live run token before it reaches the MCP transport."""

    def __init__(self, app: StreamableHTTPASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode("latin-1").removeprefix("Bearer ").strip()
            if not dispatch.run_for_token(supplied):
                await JSONResponse({"detail": "Not a live agent run token"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)
