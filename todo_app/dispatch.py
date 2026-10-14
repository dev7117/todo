"""Agent assignees: hand a task to Claude Code running on one of your machines.

An agent is a person (people.kind = 'agent') with a profile (agent_profiles) naming the Claude
Code agent it runs as: `.claude/agents/<claude_agent>.md` in the project's repo, which holds its
role and rules and is reviewed in PRs like code. The project says where the repo lives on your
machines (projects.repo_path) and which branch work starts from.

Assigning a task to an agent queues a *task run* (launcher_runs.kind = 'task') for a machine.
todo-agent claims it, makes a git worktree of the repo for the task, and runs
`claude -p --agent <claude_agent>` there with an MCP config pointing at /mcp/agent and a token
issued at claim time that only works for that run, on that task (and its children), while the
run is live. Over it the agent reads its assignment, logs progress, moves the task between
in progress and waiting, keeps its own subtasks, asks you questions, and finally asks for
review: done is a proposal you approve in Review next to its PR.

A repo is optional. A task with no project, or whose project has no repo, is general work
(research, drafting, planning): the agent runs in a scratch folder of its own for that task
(kept, so it can resume), with its definition in your user-level ~/.claude/agents/<name>.md,
and finishes with a summary instead of a PR.

A *check run* (kind 'check') is the setup smoke test: worktree, claude --agent, get_assignment,
report_progress("ready"), with no task involved.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any

import base64

from .agents import FINAL, Agents
from .attachments import Attachments
from .notebook import Notebook
from .people import People
from .review import Review
from .store import CLOSED_STATUSES, Invalid, NotFound, Store, now_iso

AGENT_MIN_VERSION = (1, 2)  # todo-agent that knows task runs
LIVE = ("claimed", "running")
ACTIVE = ("queued", *LIVE)
CLAUDE_AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
WORKER_PATH = "/mcp/agent"
MAX_INLINE_FILE = 15 * 1024 * 1024

# What every agent is told: you see the task, never its machine.
VISIBLE = (
    "The user only sees what's on the task in the todo app, never your working folder or this terminal. "
    "Deliver there: write-ups, drafts and findings as notebook blocks (add_note), files with attach_file "
    "or by saving them in ./outputs/ (attached to the task when the run ends), progress with report_progress. "
    "Never point the user to a local path."
)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _version(text: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in (text or "0").split("."))
    except ValueError:
        return (0,)


def supports_tasks(version: str | None) -> bool:
    """Whether a todo-agent of this version can run agents."""
    return _version(version) >= AGENT_MIN_VERSION


def _slug(title: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", title.lower()))[:40].strip("-") or "task"


class Dispatch:
    def __init__(self, store: Store, agents: Agents, people: People, review: Review, notebook: Notebook,
                 attachments: Attachments) -> None:
        self.store = store
        self.attachments = attachments
        self.agents = agents
        self.people = people
        self.review = review
        self.notebook = notebook
        store.on_assigned.append(self._assigned)

    # ----- agents (people with a profile) -----

    def profile(self, person_id: int) -> dict[str, Any] | None:
        row = self.store._row("SELECT * FROM agent_profiles WHERE person_id = ?", (person_id,))
        if not row:
            return None
        out = dict(row)
        out["projects"] = json.loads(out["projects"] or "[]")
        out["auto_dispatch"] = bool(out["auto_dispatch"])
        return out

    def get_agent(self, ref: int | str) -> dict[str, Any]:
        person = self.people.resolve(ref)
        if not person or person["kind"] != "agent":
            raise NotFound(f"{ref!r} isn't an agent. list_agents shows them; save_agent makes one.")
        return self._agent(person)

    def _agent(self, person: dict[str, Any]) -> dict[str, Any]:
        profile = self.profile(person["id"]) or {}
        runs = self.agents.list_runs(person_id=person["id"], limit=1)
        return {
            "id": person["id"], "name": person["name"], "title": person["title"], "archived": person["archived"],
            "claude_agent": profile.get("claude_agent"), "machine": profile.get("machine"),
            "model": profile.get("model"), "max_turns": profile.get("max_turns"),
            "allowed_tools": profile.get("allowed_tools", ""), "projects": profile.get("projects", []),
            "executor": profile.get("executor", "claude"),
            "auto_dispatch": profile.get("auto_dispatch", True), "configured": bool(profile),
            "last_run": runs[0] if runs else None,
        }

    def list_agents(self) -> list[dict[str, Any]]:
        rows = self.store._rows("SELECT id FROM people WHERE kind = 'agent' AND archived = 0 ORDER BY lower(name)")
        return [self._agent(self.people.get(r["id"])) for r in rows]

    def save_agent(
        self,
        name: str,
        *,
        claude_agent: str | None = None,
        executor: str | None = None,
        machine: str | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        allowed_tools: str | None = None,
        projects: list[int | str] | None = None,
        auto_dispatch: bool | None = None,
        title: str | None = None,
        person_id: int | None = None,
    ) -> dict[str, Any]:
        """Create or update an agent: the person and its profile. Only the fields given change;
        "" clears machine / model."""
        name = (name or "").strip()
        if person_id is not None:
            person = self.people.get(person_id)
        else:
            if not name:
                raise Invalid("An agent needs a name")
            row = self.store._row("SELECT id FROM people WHERE lower(name) = lower(?)", (name,))
            person = self.people.get(row["id"]) if row else None
        if person and person["kind"] != "agent":
            raise Invalid(f"{person['name']} is a person, not an agent. Pick another name.")
        current = self.profile(person["id"]) if person else None
        claude_agent = (claude_agent or "").strip() or (current or {}).get("claude_agent") or ""
        executor = executor or (current or {}).get("executor", "claude")
        if executor not in ("claude", "codex-herdr", "codex-app"):
            raise Invalid("executor must be claude, codex-herdr or codex-app")
        if executor in ("codex-herdr", "codex-app") and not claude_agent:
            claude_agent = "codex"  # compatibility field; Codex reads AGENTS.md, not Claude agent files
        if not CLAUDE_AGENT.fullmatch(claude_agent):
            raise Invalid("claude_agent is the agent file's name: .claude/agents/<claude_agent>.md (letters, digits, - and _)")
        if executor in ("codex-herdr", "codex-app") and (allowed_tools or max_turns):
            raise Invalid("Claude allowed_tools/max_turns are not supported by Codex")
        project_ids = None
        if projects is not None:
            project_ids = [self.store.resolve_project(p)["id"] for p in projects if p not in (None, "")]  # type: ignore[index]
        ts = now_iso()
        with self.store.tx() as c:
            if person:
                person = self.people.update(person["id"], name=name or None, title=title)
            else:
                person = self.people.create(name, kind="agent", title=title or "Agent", area="work")
            values = {
                "claude_agent": claude_agent,
                "executor": executor,
                "machine": ((machine or "").strip() or None) if machine is not None else (current or {}).get("machine"),
                "model": ((model or "").strip() or None) if model is not None else (current or {}).get("model"),
                "max_turns": None if executor in ("codex-herdr", "codex-app") else max_turns if max_turns is not None else (current or {}).get("max_turns"),
                "allowed_tools": "" if executor in ("codex-herdr", "codex-app") else allowed_tools.strip() if allowed_tools is not None else (current or {}).get("allowed_tools", ""),
                "projects": json.dumps(project_ids if project_ids is not None else (current or {}).get("projects", [])),
                "auto_dispatch": int(auto_dispatch if auto_dispatch is not None else (current or {}).get("auto_dispatch", True)),
            }
            c.execute(
                f"INSERT INTO agent_profiles (person_id, {', '.join(values)}, created_at, updated_at)"
                f" VALUES (?, {', '.join('?' * len(values))}, ?, ?) ON CONFLICT (person_id) DO UPDATE SET"
                f" {', '.join(f'{k} = excluded.{k}' for k in values)}, updated_at = excluded.updated_at",
                (person["id"], *values.values(), ts, ts),
            )
        return self.get_agent(person["id"])

    # ----- dispatching -----

    def _assigned(self, task_id: int, assignee_id: int | None, source: str) -> None:
        """Store hook: a task's assignee changed. Start the agent if it's one that starts on assign."""
        if not assignee_id:
            return
        profile = self.profile(assignee_id)
        if not profile or not profile["auto_dispatch"] or self._active(task_id):
            return
        try:
            self.enqueue(task_id, source=source)
        except (Invalid, NotFound) as exc:
            # The assignment stands; the task says why nothing started.
            name = self.people.get(assignee_id)["name"]
            self.store._log(task_id, "note", f"{name} couldn't start: {exc}", f"agent: {name}", event="agent")

    def _active(self, task_id: int) -> dict[str, Any] | None:
        row = self.store._row(
            f"SELECT id FROM launcher_runs WHERE task_id = ? AND status IN {ACTIVE} ORDER BY id DESC LIMIT 1", (task_id,)
        )
        return self.agents.get_run(row["id"]) if row else None

    def enqueue(self, task_id: int, *, message: str | None = None, source: str = "app") -> dict[str, Any]:
        """Queue a run of the task's agent. ``message``: your reply, when the agent asked you
        something or you're sending it back; it resumes its last Claude session."""
        task = self.store.get_task(task_id)
        if task["status"] in CLOSED_STATUSES:
            raise Invalid("The task is closed")
        if not task["assignee_id"]:
            raise Invalid("Assign the task to an agent first")
        person = self.people.get(task["assignee_id"])
        profile = self.profile(person["id"])
        if person["kind"] != "agent" or not profile:
            raise Invalid(f"{person['name']} isn't an agent")
        if self._active(task_id):
            raise Invalid(f"{person['name']} is already on it")
        project = self._project_for(task, profile)  # None: general work, no repo
        machine = self._machine(profile)
        previous = self.store._row(
            "SELECT session_id, branch FROM launcher_runs WHERE task_id = ? AND person_id = ? AND kind = 'task' AND executor = ?"
            " ORDER BY id DESC LIMIT 1", (task_id, person["id"], profile["executor"]),
        )
        ts = now_iso()
        with self.store.tx() as c:
            # Recheck under BEGIN IMMEDIATE: concurrent assignment/start requests serialize.
            current_task = self.store.get_task(task_id)
            if current_task["assignee_id"] != person["id"] or current_task["status"] in CLOSED_STATUSES:
                raise Invalid("The task assignment changed or the task closed")
            if self._active(task_id):
                raise Invalid(f"{person['name']} is already on it")
            uncertain = c.execute("SELECT id FROM launcher_runs WHERE task_id = ? AND "
                                  "json_extract(launch, '$.status') IN ('needs_inspection', 'cancel_pending')",
                                  (task_id,)).fetchone()
            if uncertain:
                raise Invalid(f"Inspect Herdr launch for run {uncertain['id']} before starting again")
            cur = c.execute(
                "INSERT INTO launcher_runs (kind, customer_id, agent, label, cwd, mode, requested_at, task_id,"
                " person_id, project_id, message, session_id, branch, executor) VALUES ('task', ?, ?, ?, ?, 'background', ?, ?, ?, ?, ?, ?, ?, ?)",
                (task["customer_id"], machine, f"{person['name']}: {task['title']}"[:200],
                 project["repo_path"] if project else None, ts, task_id, person["id"],
                 project["id"] if project else None, (message or "").strip() or None,
                 previous["session_id"] if previous else None,
                 ((previous["branch"] if previous else None) or f"agent/{task_id}-{_slug(task['title'])}") if project else None, profile["executor"]),
            )
            what = "Sent back to" if message else "Queued for"
            self.store._log(task_id, "change", f"{what} {person['name']} on {machine}", source, ts, event="agent")
            if message and message.strip():
                self.store._add_note(task_id, message.strip(), source)
            self.store._reindex(task_id)
        return self.agents.get_run(cur.lastrowid)

    def _project_for(self, task: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
        """The project whose repo the agent works in, or None for general work (no repo)."""
        project = self.store.get_project(task["project_id"]) if task["project_id"] else None
        if project and profile["projects"] and project["id"] not in profile["projects"]:
            raise Invalid(f"This agent isn't set up for {project['name']}")
        return project if project and project["repo_path"] else None

    def _machine(self, profile: dict[str, Any]) -> str:
        """The profile's machine (the run waits for it if it's offline), else the only one online,
        else the only one known."""
        if profile.get("machine"):
            return profile["machine"]
        machines = self.agents.list()
        online = [m["name"] for m in machines if m["online"]]
        if len(online) == 1:
            return online[0]
        if not machines:
            raise Invalid("No machine is connected. Install todo-agent (Settings → Machines).")
        if len(machines) == 1:
            return machines[0]["name"]
        raise Invalid(f"Pick a machine for this agent ({', '.join(m['name'] for m in machines)})")

    def stop(self, run_id: int, *, source: str = "app") -> dict[str, Any]:
        run = self.agents.cancel(run_id)
        self._finish(run, source=source)
        return run

    # ----- the machine side -----

    def claim(self, machine: str, *, version: str = "", accept_tasks: bool = True) -> dict[str, Any] | None:
        """A run for this machine. Task and check runs come with what todo-agent needs to start
        Claude: the repo and branch, the agent profile, and a fresh run token (only its hash is
        kept; it dies with the run)."""
        knows_tasks = supports_tasks(version)
        kinds = ("tool", "task", "check") if knows_tasks and accept_tasks else ("tool",)
        # Old machines must never consume an interactive Codex job as a Claude job.
        excluded = () if _version(version) >= (1, 4) else ("codex-herdr", "codex-app")
        run = self.agents.claim(machine, kinds=kinds, excluded_executors=excluded)
        if not run or run["kind"] == "tool":
            return run
        token = secrets.token_urlsafe(32)
        with self.store.tx() as c:
            c.execute("UPDATE launcher_runs SET token_hash = ? WHERE id = ?", (token_hash(token), run["id"]))
        full = self.agents.get_run(run["id"])
        if full["status"] != "claimed":
            return None
        profile = self.profile(full["person_id"]) if full["person_id"] else None
        project = self.store.get_project(full["project_id"]) if full["project_id"] else None
        if not profile or profile["executor"] != full["executor"] or (project and not project["repo_path"]):
            self.agents.report(run["id"], machine, status="failed", error="The agent or its project's repo is gone")
            return None
        task = self.store.get_task(full["task_id"]) if full["task_id"] else None
        if task and (task["assignee_id"] != full["person_id"] or task["status"] in CLOSED_STATUSES):
            self.report(run["id"], machine, status="failed", error="Task assignment changed or task closed before claim")
            return None
        if task:
            prompt = (f"You are {full['person']}, working todo task {task['id']}: “{task['title']}”. "
                      f"Use the todo-worker skill: start with get_assignment. {VISIBLE}")
            if profile["executor"] in ("codex-herdr", "codex-app"):
                prompt = (f"Work todo task {task['id']}: {task['title']}. Start with todo_worker.get_assignment. "
                          "Read AGENTS.md and work in this isolated worktree. Report progress through todo_worker. "
                          "Ask if blocked; request_review when ready. Never mark the task done. "
                          "Do not push, publish a PR, merge or deploy without user authorization. "
                          "The user can join this work in the selected harness.")
                instructions = self.people.get(full["person_id"]).get("notes") or ""
                if instructions:
                    prompt += f"\n\nYour agent role/context (follow repository and task rules):\n{instructions}"
            if full["message"]:
                prompt += " The user replied on the task; get_assignment has it."
        else:
            prompt = ("Setup check for the todo app. Call get_assignment, follow it, then stop. "
                      "Don't change any files.")
        return {
            **run,
            "token": token,
            "mcp_path": WORKER_PATH,
            "prompt": prompt,
            "task_id": full["task_id"],
            # None = general work: a scratch folder, no git, the agent file from ~/.claude/agents.
            "project": {"id": project["id"], "name": project["name"], "repo_path": project["repo_path"],
                        "default_branch": project["default_branch"]} if project else None,
            "branch": full["branch"],
            "session_id": full["session_id"],
            "agent": {"executor": profile["executor"], "name": full["person"], "instructions": self.people.get(full["person_id"]).get("notes") or "", "claude_agent": profile["claude_agent"], "model": profile["model"],
                      "max_turns": profile["max_turns"], "allowed_tools": profile["allowed_tools"]},
        }

    def report(self, run_id: int, machine: str, **fields: Any) -> dict[str, Any]:
        before = self.agents.get_run(run_id)
        run = self.agents.report(run_id, machine, **fields)
        if run["kind"] == "task" and run["task_id"]:
            if before["status"] in ("queued", "claimed") and run["status"] == "running":
                self._started(run)
            if before["status"] not in FINAL and run["status"] in FINAL:
                self._finish(run, source=f"agent: {run['person']}")
        return run

    def _started(self, run: dict[str, Any]) -> None:
        task = self.store.get_task(run["task_id"])
        if task["status"] in CLOSED_STATUSES:
            return
        fields = {"status": "in_progress"} if task["status"] != "in_progress" else {}
        if task["waiting_on"]:
            fields["waiting_on"] = None
        self.store.update_task(task["id"], fields, source=self.source(run))
        with self.store.tx():
            self.store._log(task["id"], "change", f"{run['person']} started on {run['agent']}", self.source(run),
                            event="agent")

    def _finish(self, run: dict[str, Any], *, source: str) -> None:
        """A task run ended. If the agent asked for review or asked a question, it already left
        the task waiting on you. Otherwise say what happened and put it back in your hands."""
        if run["kind"] != "task" or not run["task_id"] or run.get("outcome") in ("review", "question"):
            return
        try:
            task = self.store.get_task(run["task_id"])
        except NotFound:
            return
        if task["status"] in CLOSED_STATUSES:
            return
        tail = "\n".join((run["output"] or "").strip().splitlines()[-12:])
        name = run["person"] or "The agent"
        if run["status"] == "succeeded":
            note, waiting = f"{name} stopped without asking for review.", "you"
        elif run["status"] == "cancelled":
            note, waiting = f"Stopped {name}.", "you"
        elif run["status"] == "declined":
            note, waiting = f"{name} wasn't allowed to run on {run['agent']}.", "you"
        else:
            note, waiting = f"{name}'s run {run['status']}: {run.get('error') or 'see the run log'}", "you"
        if tail and run["status"] != "declined":
            note += f"\n\n```\n{tail}\n```"
        self.store.update_task(task["id"], {"status": "waiting", "waiting_on": waiting}, source=source, note=note)

    # ----- run tokens (the worker MCP endpoint) -----

    def run_for_token(self, token: str) -> dict[str, Any] | None:
        """The live run this token was issued for, else None."""
        if not token:
            return None
        row = self.store._row(
            f"SELECT id FROM launcher_runs WHERE token_hash = ? AND status IN {LIVE}", (token_hash(token),)
        )
        return self.agents.get_run(row["id"]) if row else None

    @staticmethod
    def source(run: dict[str, Any]) -> str:
        return f"agent: {run.get('person') or 'agent'}"

    def _scoped(self, run: dict[str, Any], task_id: int | None) -> dict[str, Any]:
        """A task this run may touch: its own, or one of its children."""
        if run["kind"] != "task":
            raise Invalid("This is a setup check; there's no task to change")
        target = task_id or run["task_id"]
        task = self.store.get_task(target)
        if task["id"] != run["task_id"] and task["parent_id"] != run["task_id"]:
            raise Invalid("You can only change the task you were given (and its children)")
        return task

    # ----- what the agent can do (called by mcp_worker) -----

    def assignment(self, run: dict[str, Any]) -> dict[str, Any]:
        project = self.store.get_project(run["project_id"]) if run["project_id"] else None
        repo = {"name": project["name"], "repo_path": project["repo_path"], "default_branch": project["default_branch"],
                "branch": run["branch"]} if project else None
        if run["kind"] == "check":
            return {
                "setup_check": True,
                "you_are": run["person"],
                "project": repo,
                "do": "This is only a setup check. Call report_progress with the note \"ready\", "
                      "mentioning the agent file and skills you loaded, then stop. Change nothing.",
            }
        task = self.store.get_task(run["task_id"], with_updates=True)
        updates = task["updates"]
        mine = self.source(run)
        replies = [u for u in updates if u["kind"] == "note" and not u["source"].startswith("agent:")]
        # What the user said since the agent's last note (its status changes don't count).
        last_mine = max((u["id"] for u in updates if u["source"] == mine and u["kind"] == "note"), default=0)
        return {
            "you_are": run["person"],
            "run_id": run["id"],
            "task": {
                "id": task["id"], "title": task["title"], "status": task["status"], "notes": task["notes"],
                "priority": task["priority"], "due_on": task["due_on"], "project": task["project"],
                "customer": task["customer"], "external_url": task["external_url"], "parent": task["parent"],
                "children": [{"id": ch["id"], "title": ch["title"], "status": ch["status"], "assignee": ch.get("assignee")}
                             for ch in task.get("children") or []],
            },
            "notebook": [{"id": b["id"], "kind": b["kind"], "title": b["title"], "body": b["body"], "done": b["done"]}
                         for b in self.notebook.blocks("task", task["id"])],
            "history": [{"at": u["created_at"], "kind": u["kind"], "source": u["source"], "text": u["body"]}
                        for u in updates[-30:]],
            "new_from_user": [u["body"] for u in replies if u["id"] > last_mine][-5:],
            "reply": run["message"],
            "files": [{"id": f["id"], "name": f["name"], "bytes": f["bytes"], "source": f["source"]}
                      for f in self.attachments.for_task(task["id"])],
            "repo": repo,
            "deliver": "The user can join your selected Codex harness; report progress and request_review on the task." if run.get("executor") in ("codex-herdr", "codex-app") else VISIBLE,
            "how": ("Work in the current isolated folder (worktree for a repository, scratch folder otherwise). "
                    "Deliver findings/files on the task with add_note/attach_file or ./outputs/. "
                    "report_progress at milestones; ask if blocked. "
                    "request_review when ready. Never mark done, merge or deploy. Push or publish a PR only "
                    "with user authorization.") if run.get("executor") in ("codex-herdr", "codex-app") else ("Work in the current directory (a git worktree on the branch above). report_progress at "
                    "milestones; ask() if you're blocked on a decision; when the work is ready, push the "
                    "branch, open a PR, and request_review with the PR URL. Never merge or deploy.") if repo else
                   ("General work, no repo: the current directory is a scratch folder for this task (kept between "
                    "runs), for your own use. The deliverable goes on the task: the write-up as notebook blocks "
                    "(add_note), files via attach_file or ./outputs/. report_progress at milestones; ask() if you're "
                    "blocked; request_review (no pr_url) when it's done, naming what you added to the task."),
        }

    def progress(self, run: dict[str, Any], note: str, task_id: int | None = None) -> dict[str, Any]:
        note = (note or "").strip()
        if not note:
            raise Invalid("note is required")
        if run["kind"] == "check":
            with self.store.tx() as c:
                c.execute("UPDATE launcher_runs SET outcome = 'ready', output = output || ? WHERE id = ?",
                          (f"\n[report_progress] {note}\n", run["id"]))
            return {"ok": True, "setup_check": "passed"}
        task = self._scoped(run, task_id)
        self.store.add_update(task["id"], note, source=self.source(run))
        return {"ok": True, "task_id": task["id"]}

    def set_status(self, run: dict[str, Any], status: str, waiting_on: str | None = None,
                   note: str | None = None, task_id: int | None = None) -> dict[str, Any]:
        if status not in ("in_progress", "waiting"):
            raise Invalid("Agents move tasks between in_progress and waiting; done goes through request_review")
        task = self._scoped(run, task_id)
        fields: dict[str, Any] = {"status": status, "waiting_on": (waiting_on or None) if status == "waiting" else None}
        updated = self.store.update_task(task["id"], fields, source=self.source(run), note=note)
        return {"task_id": updated["id"], "status": updated["status"], "waiting_on": updated["waiting_on"]}

    def add_subtask(self, run: dict[str, Any], title: str, body: str = "", task_id: int | None = None) -> dict[str, Any]:
        task = self._scoped(run, task_id)
        if not (title or "").strip():
            raise Invalid("A subtask needs a title")
        block = self.notebook.add("task", task["id"], title=title, body=body or "", kind="subtask", source=self.source(run))
        return {"block_id": block["id"], "title": block["title"]}

    def check_subtask(self, run: dict[str, Any], block_id: int, done: bool = True) -> dict[str, Any]:
        block = self.notebook.get(block_id)
        if block["kind"] != "subtask" or not block["task_id"]:
            raise Invalid("That block isn't a subtask")
        self._scoped(run, block["task_id"])
        block = self.notebook.update(block_id, done=done, source=self.source(run))
        return {"block_id": block["id"], "done": bool(block["done"])}

    def add_note(self, run: dict[str, Any], title: str, body: str, task_id: int | None = None) -> dict[str, Any]:
        """A notebook block on the task: where a write-up, draft or findings go."""
        task = self._scoped(run, task_id)
        if not (body or "").strip():
            raise Invalid("body is required (markdown)")
        block = self.notebook.add("task", task["id"], title=title or "", body=body, source=self.source(run))
        return {"block_id": block["id"], "title": block["title"]}

    def update_note(self, run: dict[str, Any], block_id: int, body: str, *, append: bool = False,
                    title: str | None = None) -> dict[str, Any]:
        """Revise a block this agent wrote (never the user's)."""
        block = self.notebook.get(block_id)
        if not block["task_id"]:
            raise Invalid("That block isn't on a task")
        self._scoped(run, block["task_id"])
        if block["source"] != self.source(run):
            raise Invalid("You can only revise blocks you wrote; add a new one instead")
        text = (block["body"].rstrip() + "\n\n" + body) if append else body
        fields: dict[str, Any] = {"body": text}
        if title is not None:
            fields["title"] = title
        block = self.notebook.update(block_id, source=self.source(run), **fields)
        return {"block_id": block["id"], "title": block["title"]}

    def attach_file(self, run: dict[str, Any], name: str, *, text: str | None = None,
                    content_base64: str | None = None, note: str = "", task_id: int | None = None) -> dict[str, Any]:
        task = self._scoped(run, task_id)
        if (text is None) == (content_base64 is None):
            raise Invalid("Pass exactly one of text or content_base64")
        data = text.encode() if text is not None else base64.b64decode(content_base64 or "", validate=True)
        if len(data) > MAX_INLINE_FILE:
            raise Invalid("Too big to send inline; save it in ./outputs/ and it's attached when the run ends")
        f = self.attachments.add(None, name, data, None, task_id=task["id"], note=note, source=self.source(run))
        return {k: f[k] for k in ("id", "name", "content_type", "bytes", "url")}

    def attach_output(self, run_id: int, machine: str, name: str, data: bytes, content_type: str | None) -> dict[str, Any]:
        """todo-agent uploading a file from the run's outputs/ folder to its task."""
        run = self.agents.get_run(run_id)
        if run["agent"] != machine:
            raise Invalid("That run belongs to another machine")
        if run["kind"] != "task" or not run["task_id"]:
            raise Invalid("Only task runs attach files")
        f = self.attachments.add(None, name, data, content_type, task_id=run["task_id"], source=self.source(run))
        return {k: f[k] for k in ("id", "name", "content_type", "bytes", "url")}

    def ask(self, run: dict[str, Any], question: str) -> dict[str, Any]:
        """Blocked on the user: the task waits on them with the question in its history. Their
        reply (Send to agent) resumes this session."""
        question = (question or "").strip()
        if not question:
            raise Invalid("question is required")
        task = self._scoped(run, None)
        self.store.update_task(task["id"], {"status": "waiting", "waiting_on": "you"}, source=self.source(run),
                               note=f"Question: {question}")
        with self.store.tx() as c:
            c.execute("UPDATE launcher_runs SET outcome = 'question' WHERE id = ?", (run["id"],))
        return {"ok": True, "next": "Stop now. The user's answer starts a new run that resumes this session."}

    def request_review(self, run: dict[str, Any], summary: str, pr_url: str | None = None) -> dict[str, Any]:
        """The work is ready: record the PR, leave the task waiting on your review, and propose
        completing it (approved in Review)."""
        summary = (summary or "").strip()
        if not summary:
            raise Invalid("summary is required: what you did and how you checked it")
        pr_url = (pr_url or "").strip() or None
        if pr_url and not re.match(r"https?://", pr_url):
            raise Invalid("pr_url must be an http(s) URL")
        task = self._scoped(run, None)
        source = self.source(run)
        fields: dict[str, Any] = {"status": "waiting", "waiting_on": "your review"}
        if pr_url and not task["external_url"]:
            fields["external_url"] = pr_url
        note = summary + (f"\n\nPR: {pr_url}" if pr_url else "")
        self.store.update_task(task["id"], fields, source=source, note=note)
        with self.store.tx() as c:
            c.execute("UPDATE launcher_runs SET outcome = 'review', pr_url = COALESCE(?, pr_url) WHERE id = ?",
                      (pr_url, run["id"]))
        pending = self.store._row(
            "SELECT cs.id FROM changes ch JOIN changesets cs ON cs.id = ch.changeset_id WHERE ch.task_id = ?"
            " AND ch.action = 'complete' AND ch.status = 'pending' AND cs.status = 'pending'", (task["id"],),
        )
        if pending:  # back after your reply: the earlier "done" proposal still stands
            return {"ok": True, "proposal_id": pending["id"], "status": "pending",
                    "next": "Done. The user reviews the PR and approves completing the task. Stop now."}
        cs = self.review.propose(
            [{"action": "complete", "task_id": task["id"], "note": f"Approved {run['person']}'s work"
              + (f" ({pr_url})" if pr_url else ""), "reason": summary[:300]}],
            source=source, summary=f"{run['person']}: {task['title']}"[:200], created_by="agent",
        )
        return {"ok": True, "proposal_id": cs["id"], "status": cs["status"],
                "next": "Done. The user reviews the PR and approves completing the task. Stop now."}

    # ----- setup -----

    def set_project_repo(
        self, project: int | str, *, repo_path: str, default_branch: str | None = None,
        area: str = "work", customer: str | None = None, create: bool = True,
    ) -> dict[str, Any]:
        try:
            found = self.store.resolve_project(project)
        except NotFound:
            if not create or isinstance(project, int) or str(project).isdigit():
                raise
            found = None
        if not found:
            found = self.store.create_project(str(project), area, customer, repo_path=repo_path,
                                              default_branch=default_branch)
            return {**found, "created": True}
        return self.store.update_project(found["id"], repo_path=repo_path, default_branch=default_branch)

    def check_setup(self, agent: int | str, project: int | str | None = None) -> dict[str, Any]:
        """What's in place for this agent to work, and how to fix what isn't."""
        info = self.get_agent(agent)
        profile = self.profile(info["id"]) or {}
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str, fix: str = "") -> None:
            checks.append({"check": name, "ok": ok, "detail": detail, **({"fix": fix} if not ok and fix else {})})

        name = profile.get("claude_agent")
        codex = profile.get("executor") in ("codex-herdr", "codex-app")
        check("profile", bool(profile), f"Codex harness: {profile['executor']}" if codex else f"runs as the Claude agent “{name}”" if profile
              else "no profile", "save_agent with claude_agent")
        projects = [self.store.get_project(p) for p in profile.get("projects", [])]
        if project is not None:
            projects = [self.store.resolve_project(project)]  # type: ignore[list-item]
        repos = [p for p in projects if p and p["repo_path"]]
        # Informational: a repo is optional. Tasks without one are general work in a scratch folder.
        check("where it works", True,
              "; ".join(f"{p['name']} → {p['repo_path']} ({p['default_branch']}), agent file .claude/agents/{name}.md there"
                        for p in repos)
              + ("; " if repos else "")
              + f"tasks without a repo: a scratch folder, agent file ~/.claude/agents/{name}.md on the machine")
        if codex:
            checks[-1] = {"check": "where it works", "ok": True,
                          "detail": "All projects by default; optional project limits on the agent. Repositories use isolated worktrees; "
                                    "general tasks use scratch folders. Harness setup is machine-wide, not per client/project."}
        machines = {m["name"]: m for m in self.agents.list()}
        try:
            target = self._machine(profile) if profile else None
        except Invalid as exc:
            target, why = None, str(exc)
        else:
            why = ""
        m = machines.get(target or "")
        check("machine", bool(m and m["online"]), f"{target}: {'online' if m and m['online'] else 'offline'}"
              if target else why, "Start todo-agent there, or save_agent with machine")
        minimum = (1, 4) if codex else AGENT_MIN_VERSION
        check("todo-agent version", bool(m and _version(m["version"]) >= minimum),
              f"{m['version'] or 'unknown'} (needs {'.'.join(map(str, minimum))}+)" if m else "no machine",
              "Re-run the installer: curl -fsSL <todo>/agent/install.sh | sh")
        last_check = self.store._row(
            "SELECT id, status, outcome, error, finished_at FROM launcher_runs WHERE person_id = ? AND kind = 'check'"
            " ORDER BY id DESC LIMIT 1", (info["id"],),
        )
        if codex:
            check("local routing", False, "Harness readiness is machine-local: Herdr session or existing connected Codex app daemon",
                  "Use isolated integration tests, then an authorized real task on the selected machine")
            return {"agent": info["name"], "executor": profile["executor"], "ok": False, "checks": checks}
        if last_check:
            passed = last_check["outcome"] == "ready" and last_check["status"] == "succeeded"
            check("smoke run", passed, f"run {last_check['id']}: {last_check['status']}"
                  + (f" ({last_check['error']})" if last_check["error"] else ""),
                  "get_run for the log. The agent file must exist (pushed to origin for a repo, else in "
                  "~/.claude/agents on the machine) and claude must be logged in")
        else:
            check("smoke run", False, "not run yet", "test_agent runs one")
        return {"agent": info["name"], "executor": profile.get("executor", "claude"), "ok": all(c["ok"] for c in checks), "checks": checks}

    def test_agent(self, agent: int | str, project: int | str | None = None) -> dict[str, Any]:
        """Queue a check run: claude --agent in a worktree of the project's repo (the given one,
        else the agent's first with a repo), or in a scratch folder when there's none; then
        get_assignment, report_progress, done."""
        info = self.get_agent(agent)
        profile = self.profile(info["id"])
        if not profile:
            raise Invalid("Save the agent's profile first (save_agent)")
        if profile["executor"] in ("codex-herdr", "codex-app"):
            raise Invalid("Codex harness setup is machine-wide; use an authorized task to verify the selected route")
        if project is not None:
            proj = self.store.resolve_project(project)
        else:
            proj = next((p for p in (self.store.get_project(i) for i in profile["projects"]) if p["repo_path"]), None)
        if proj and not proj["repo_path"]:
            proj = None
        machine = self._machine(profile)
        with self.store.tx() as c:
            cur = c.execute(
                "INSERT INTO launcher_runs (kind, customer_id, agent, label, cwd, mode, requested_at, person_id,"
                " project_id, branch) VALUES ('check', ?, ?, ?, ?, 'background', ?, ?, ?, ?)",
                (proj["customer_id"] if proj else None, machine, f"{info['name']}: setup check",
                 proj["repo_path"] if proj else None, now_iso(), info["id"], proj["id"] if proj else None,
                 f"agent/check-{_slug(info['name'])}" if proj else None),
            )
        return self.agents.get_run(cur.lastrowid)
