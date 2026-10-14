"""Desktop agents and launcher runs.

A todo-agent (todo_app/agent_dist/todo-agent.py) runs on each of the user's machines (the Mac,
the Omarchy desktop). It long-polls `POST /api/agent/poll` with the API token; when the user
presses Run on a hub tool, a run is queued for a machine and handed to that machine's agent.
The agent asks for local approval if the command is new or changed (it fingerprints the
command itself, never trusting the server's word), runs it in a terminal (interactive) or in
the background (headless), and reports status and output back here.
"""
from __future__ import annotations

import json

from datetime import datetime, timedelta, timezone
from typing import Any

from .hub import Hub
from .store import Invalid, NotFound, Store, now_iso

ONLINE_SECONDS = 75  # an agent polls continuously; silence this long means it's gone
CLAIM_EXPIRY_SECONDS = 120  # queued runs nobody picked up
MAX_OUTPUT = 200_000
FINAL = ("succeeded", "failed", "declined", "expired", "cancelled")
AGENT_STATUSES = ("running", "succeeded", "failed", "declined")


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


class Agents:
    def __init__(self, store: Store, hub: Hub) -> None:
        self.store = store
        self.hub = hub

    # ----- machines -----

    def heartbeat(self, name: str, platform: str, version: str = "") -> None:
        name = (name or "").strip()
        if not name:
            raise Invalid("Agent name is required")
        ts = now_iso()
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO agents (name, platform, version, last_seen, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET platform = excluded.platform, version = excluded.version,"
                " last_seen = excluded.last_seen",
                (name, platform or "", version or "", ts, ts),
            )

    def list(self) -> list[dict[str, Any]]:
        cutoff = _ago(ONLINE_SECONDS)
        rows = self.store._rows("SELECT * FROM agents ORDER BY last_seen DESC")
        return [{**dict(r), "online": r["last_seen"] >= cutoff} for r in rows]

    def delete(self, name: str) -> None:
        with self.store.tx() as c:
            c.execute("DELETE FROM agents WHERE name = ?", (name,))

    # ----- runs -----

    def request_run(
        self, link_id: int, agent: str | None = None, *, occurrence_id: int | None = None, step_id: int | None = None
    ) -> dict[str, Any]:
        """Queue a tool run. Machine: the one asked for, else the tool's preferred machine, else
        the only one online. Run for a cadence meeting's prep step, the agent uploads the step's
        output files to that meeting afterwards."""
        launcher = self.hub.launcher(link_id)
        link = self.hub.get_link(link_id)
        machines = self.list()
        online = [m["name"] for m in machines if m["online"]]
        target = agent or link.get("agent")
        if not target:
            if len(online) == 1:
                target = online[0]
            elif not online:
                raise Invalid("No machine is connected. Install todo-agent (Settings → Machines).")
            else:
                raise Invalid(f"Pick a machine: {', '.join(online)}")
        if target not in {m["name"] for m in machines}:
            raise Invalid(f"Unknown machine {target!r}")
        if target not in online:
            raise Invalid(f"{target} isn't connected right now")
        with self.store.tx() as c:
            cur = c.execute(
                "INSERT INTO launcher_runs (link_id, customer_id, agent, label, command, cwd, mode, requested_at,"
                " occurrence_id, step_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (link_id, launcher["customer_id"], target, launcher["label"], launcher["command"],
                 launcher["cwd"], launcher["mode"], now_iso(), occurrence_id, step_id),
            )
        return self.get_run(cur.lastrowid)

    def claim(self, agent: str, *, kinds: tuple[str, ...] = ("tool",), excluded_executors: tuple[str, ...] = ()) -> dict[str, Any] | None:
        """Hand the oldest queued run for this machine to its agent. ``kinds``: what the agent
        takes right now (agents before 1.2 only know tool runs; a busy one skips task runs)."""
        self._expire()
        with self.store.tx() as c:
            row = c.execute(
                f"SELECT id FROM launcher_runs WHERE agent = ? AND status = 'queued'"
                f" AND kind IN ({', '.join('?' * len(kinds))})"
                + (f" AND executor NOT IN ({', '.join('?' * len(excluded_executors))})" if excluded_executors else "")
                + " ORDER BY id LIMIT 1", (agent, *kinds, *excluded_executors),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE launcher_runs SET status = 'claimed', claimed_at = ? WHERE id = ?", (now_iso(), row["id"])
            )
        run = self.get_run(row["id"])
        out = {
            "id": run["id"], "kind": run["kind"], "label": run["label"], "command": run["command"], "cwd": run["cwd"],
            "mode": run["mode"], "customer": run["customer"], "customer_id": run["customer_id"],
        }
        if run.get("occurrence_id"):
            # Files the step produces (globs, ~ and cwd-relative); the agent uploads the ones
            # written during the run to the meeting.
            step = self.store._row("SELECT outputs FROM cadence_steps WHERE id = ?", (run["step_id"],)) if run.get("step_id") else None
            out.update(occurrence_id=run["occurrence_id"], step_id=run.get("step_id"),
                       outputs=[o.strip() for o in (step["outputs"] if step else "").splitlines() if o.strip()])
        return out

    def report(self, run_id: int, agent: str, **fields: Any) -> dict[str, Any]:
        # Cancellation and reports serialize; a stale running report cannot revive a stopped run.
        with self.store.tx():
            return self._report(run_id, agent, **fields)

    def _report(
        self,
        run_id: int,
        agent: str,
        *,
        status: str | None = None,
        exit_code: int | None = None,
        output: str | None = None,
        append: bool = True,
        error: str | None = None,
        session_id: str | None = None,
        branch: str | None = None,
        tokens: int | None = None,
        cached_tokens: int | None = None,
        launch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A machine reporting on its run. Task runs also send Claude's session (for resume),
        the branch it works on, and the tokens it used (running totals for the run)."""
        run = self.get_run(run_id)
        if run["agent"] != agent:
            raise Invalid("That run belongs to another machine")
        if launch is not None:
            allowed = {"session", "workspace", "tab", "pane_id", "agent_name", "cwd", "project_root", "terminal_id", "route", "thread_id", "turn_id", "codex_project", "remote_server", "app_installation", "status", "error"}
            if set(launch) - allowed:
                raise Invalid("Unknown launch receipt fields")
            with self.store.tx() as c:
                c.execute("UPDATE launcher_runs SET launch = ? WHERE id = ?", (json.dumps(launch), run_id))
        if session_id or branch or tokens is not None or cached_tokens is not None:
            with self.store.tx() as c:
                c.execute(
                    "UPDATE launcher_runs SET session_id = COALESCE(?, session_id), branch = COALESCE(?, branch),"
                    " tokens = COALESCE(?, tokens), cached_tokens = COALESCE(?, cached_tokens) WHERE id = ?",
                    (session_id, branch, tokens, cached_tokens, run_id),
                )
        if run["status"] in FINAL:
            return self.get_run(run_id)
        sets: dict[str, Any] = {}
        if status:
            if status not in AGENT_STATUSES:
                raise Invalid(f"status must be one of {', '.join(AGENT_STATUSES)}")
            sets["status"] = status
            if status == "running":
                sets["started_at"] = now_iso()
            else:
                sets["finished_at"] = now_iso()
        if exit_code is not None:
            sets["exit_code"] = exit_code
        if error is not None:
            sets["error"] = error[:2000]
        if output:
            text = (run["output"] + output) if append else output
            sets["output"] = text[-MAX_OUTPUT:]
        if sets:
            with self.store.tx() as c:
                c.execute(
                    f"UPDATE launcher_runs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                    (*sets.values(), run_id),
                )
        return self.get_run(run_id)

    def cancel(self, run_id: int) -> dict[str, Any]:
        with self.store.tx():
            return self._cancel(run_id)

    def _cancel(self, run_id: int) -> dict[str, Any]:
        """Withdraw a run nobody has picked up yet, or stop an agent's task run: the machine
        sees 'cancelled' on its next report and stops Claude."""
        run = self.get_run(run_id)
        stoppable = ("queued", "claimed", "running") if run["kind"] != "tool" else ("queued",)
        if run["status"] not in stoppable:
            raise Invalid("Only a run still waiting for its machine can be cancelled")
        with self.store.tx() as c:
            c.execute("UPDATE launcher_runs SET status = 'cancelled', finished_at = ? WHERE id = ?", (now_iso(), run_id))
        return self.get_run(run_id)

    _RUN_SELECT = (
        "SELECT r.*, c.name AS customer, pe.name AS person, t.title AS task FROM launcher_runs r"
        " LEFT JOIN customers c ON c.id = r.customer_id LEFT JOIN people pe ON pe.id = r.person_id"
        " LEFT JOIN tasks t ON t.id = r.task_id"
    )

    def get_run(self, run_id: int) -> dict[str, Any]:
        row = self.store._row(self._RUN_SELECT + " WHERE r.id = ?", (run_id,))
        if not row:
            raise NotFound(f"No run with id {run_id}")
        out = dict(row)
        out.pop("token_hash", None)
        out["launch"] = json.loads(out["launch"])
        return out

    def list_runs(
        self, *, link_id: int | None = None, customer_id: int | None = None, task_id: int | None = None,
        person_id: int | None = None, limit: int = 20, output: bool = False,
    ) -> list[dict[str, Any]]:
        self._expire()
        where, params = [], []
        if task_id is not None:
            where.append("r.task_id = ?")
            params.append(task_id)
        if person_id is not None:
            where.append("r.person_id = ?")
            params.append(person_id)
        if link_id is not None:
            where.append("r.link_id = ?")
            params.append(link_id)
        if customer_id is not None:
            where.append("r.customer_id = ?")
            params.append(customer_id)
        sql = self._RUN_SELECT + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY r.id DESC LIMIT ?"
        rows = [dict(r) for r in self.store._rows(sql, [*params, limit])]
        for r in rows:
            r.pop("token_hash", None)
            r["launch"] = json.loads(r["launch"])
        if not output:
            for r in rows:
                r["output"] = r["output"][-2000:]
        return rows

    def _expire(self) -> None:
        # Task runs wait in the queue until their machine comes online; tool runs are for now.
        with self.store.tx() as c:
            c.execute(
                "UPDATE launcher_runs SET status = 'expired', finished_at = ?,"
                " error = 'The machine never picked it up' WHERE status = 'queued' AND kind = 'tool'"
                " AND requested_at < ?",
                (now_iso(), _ago(CLAIM_EXPIRY_SECONDS)),
            )
            # Claimed but never started: the approval prompt went unanswered or the agent died.
            c.execute(
                "UPDATE launcher_runs SET status = 'expired', finished_at = ?,"
                " error = 'No answer on the machine' WHERE status = 'claimed' AND claimed_at < ?",
                (now_iso(), _ago(15 * 60)),
            )
