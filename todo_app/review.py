"""Proposed changes: Claude's task edits wait here until the user approves them.

A changeset is one batch from one source (usually a meeting): creates, field updates,
progress notes, completions, and customer links. Each change stores its payload in Store
terms plus a snapshot of the task when it was proposed, so the review shows a git-style
diff (`- before` / `+ after`) and flags tasks that changed since. Approved changes apply
one by one (a failure doesn't block the rest) and link back to the meeting.

With the review setting off, proposals apply immediately, still recorded as changesets.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from opentelemetry import trace

from .hub import Hub
from .ideas import Ideas
from .ledger import Ledger
from .notebook import Notebook
from .store import PRIORITY_LABELS, STATUS_LABELS, Invalid, NotFound, Store, now_iso

# What the ledger filters, as JSON log lines (Loki: {service_name="todo"} | json | logger="todo.ledger").
log = logging.getLogger("todo.ledger")

ACTIONS = ("create", "update", "note", "complete", "add_link", "promote_idea", "add_subtask", "check_subtask", "follow")

# Field order and labels for diffs.
FIELDS = [
    ("title", "title"), ("status", "status"), ("project_id", "project"), ("area", "area"),
    ("priority", "priority"), ("due_on", "due"), ("today", "today"), ("waiting_on", "waiting on"),
    ("external_url", "link"), ("assignee_id", "assigned to"), ("notes", "notes"),
]


class Review:
    def __init__(self, store: Store, hub: Hub, ideas: Ideas | None = None, people: Any = None) -> None:
        self.store = store
        self.hub = hub
        self.ideas = ideas or Ideas(store)
        if people is None:
            from .people import People

            people = People(store, hub, self.ideas.notebook)
        self.people = people
        self.notebook = self.ideas.notebook
        self.ledger = Ledger(store)

    # ----- proposing -----

    def propose(
        self,
        items: list[dict[str, Any]],
        *,
        source: str,
        summary: str = "",
        meeting_id: int | None = None,
        customer_id: int | None = None,
        created_by: str = "mcp",
    ) -> dict[str, Any]:
        """Validate and record a batch. Raises Invalid naming the first bad item; nothing is stored then.

        Items you already decided on (see ledger.py: a ref waiting in Review or rejected, a create
        for something tracked, anything touching a closed task) are dropped and listed in the
        result's ``skipped``. If nothing is left, nothing is stored: status "nothing_new"."""
        if not items:
            raise Invalid("No changes to propose")
        if meeting_id:
            meeting = self.hub.get_meeting(meeting_id)
            customer_id = customer_id or meeting["customer_id"]
        prepared = []
        skipped: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            label = item.get("title") or item.get("task_id") or item.get("label") or ""
            try:
                kept, code, why, flags = self.ledger.screen(item, customer_id)
                if kept is None:
                    skipped.append({"item": index, "action": item.get("action"), "title": item.get("title"),
                                    "task_id": item.get("task_id"), "ref": item.get("ref"), "code": code, "why": why})
                    continue
                change = self._prepare(kept, customer_id)
            except (Invalid, NotFound, ValueError) as exc:
                raise Invalid(f"Item {index} ({item.get('action')} {label}): {exc}") from exc
            change["ref"] = kept.get("ref")
            if flags:
                change["payload"]["flags"] = flags
            prepared.append(change)
        self._log_screening(source, customer_id, prepared, skipped)
        if not prepared:
            return {"id": None, "status": "nothing_new", "source": source, "summary": summary, "changes": [],
                    "skipped": skipped}
        ts = now_iso()
        area = self._area_of(prepared, customer_id)
        with self.store.tx() as c:
            cur = c.execute(
                "INSERT INTO changesets (source, summary, meeting_id, customer_id, area, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, summary or "", meeting_id, customer_id, area, created_by, ts),
            )
            changeset_id = cur.lastrowid
            for seq, change in enumerate(prepared):
                c.execute(
                    "INSERT INTO changes (changeset_id, seq, action, task_id, payload, before, reason, ref)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (changeset_id, seq, change["action"], change["task_id"], json.dumps(change["payload"]),
                     json.dumps(change["before"]) if change["before"] else None, change["reason"], change.get("ref")),
                )
        if created_by != "agent" and not self.store.settings()["review_claude_changes"]:
            return {**self.decide(changeset_id, approve="all"), "skipped": skipped}
        return {**self.get(changeset_id), "skipped": skipped}

    def _log_screening(self, source: str, customer_id: int | None, kept: list[dict[str, Any]],
                       skipped: list[dict[str, Any]]) -> None:
        """One log line per item the ledger filtered or let back in, one summary line per
        proposal, and the counts on the current span (proposal.create), so you can check the
        sync is behaving: {service_name="todo"} | json | logger="todo.ledger"."""
        customer = None
        if customer_id:
            try:
                customer = self.store.get_customer(customer_id)["name"]
            except NotFound:
                pass
        base = {"proposal_source": source, "customer": customer}
        for s in skipped:
            log.info("ledger skipped", extra={**base, "event": "skipped", "code": s["code"], "action": s["action"],
                                              "ref": s["ref"], "title": s["title"], "task_id": s["task_id"],
                                              "why": s["why"]})
        for change in kept:
            for flag in change["payload"].get("flags") or []:
                log.info("ledger override", extra={**base, "event": flag, "action": change["action"],
                                                   "ref": change.get("ref"), "task_id": change["task_id"],
                                                   "reason": change["reason"]})
        codes = Counter(s["code"] for s in skipped)
        log.info("ledger screened", extra={**base, "event": "screened", "kept": len(kept), "skipped": len(skipped),
                                           "with_ref": sum(1 for c in kept if c.get("ref")), **{f"skipped_{k}": v for k, v in codes.items()}})
        span = trace.get_current_span()
        span.set_attribute("ledger.kept", len(kept))
        span.set_attribute("ledger.skipped", len(skipped))
        for code, n in codes.items():
            span.set_attribute(f"ledger.skipped.{code}", n)
        for s in skipped:
            span.add_event("ledger.skipped", {"code": s["code"] or "", "ref": s["ref"] or "", "action": s["action"] or ""})

    def _area_of(self, prepared: list[dict[str, Any]], customer_id: int | None) -> str | None:
        """work / personal when every change lands on one side, else None (shown in both focuses)."""
        if customer_id:
            return "work"
        areas = set()
        for change in prepared:
            if change["action"] == "add_link":
                areas.add("work")  # customer links are client work
                continue
            if change["action"] == "promote_idea":
                project_id = (change["payload"].get("fields") or {}).get("project_id")
                areas.add(self.store.get_project(project_id)["area"] if project_id
                          else self.ideas.get(change["payload"]["idea_id"])["area"])
                continue
            fields = change["payload"].get("fields") or {}
            if fields.get("area"):
                areas.add(fields["area"])
            elif fields.get("project_id"):
                areas.add(self.store.get_project(fields["project_id"])["area"])
            elif change["before"]:
                areas.add(change["before"]["area"])
            else:
                areas.add(self.store.settings()["default_area"])
        return areas.pop() if len(areas) == 1 else None

    def _prepare(self, item: dict[str, Any], customer_id: int | None) -> dict[str, Any]:
        action = item.get("action")
        if action not in ACTIONS:
            raise Invalid(f"action must be one of {', '.join(ACTIONS)}")
        reason = (item.get("reason") or "").strip()
        note = (item.get("note") or "").strip() or None
        if action == "add_link":
            target = item.get("customer") or customer_id
            customer = self.store.resolve_customer(target, create=False) if target else None
            if not customer:
                raise Invalid("add_link needs a customer (or a meeting/customer on the proposal)")
            if not (item.get("url") or "").strip() or not (item.get("label") or "").strip():
                raise Invalid("add_link needs a label and a url")
            payload = {"customer_id": customer["id"], "label": item["label"].strip(), "url": item["url"].strip()}
            return {"action": action, "task_id": None, "payload": payload, "before": None, "reason": reason}

        if action == "follow":
            task = self.store.get_task(int(item["task_id"])) if item.get("task_id") is not None else None
            if not task:
                raise Invalid("task_id is required")
            person = self.people.resolve(item.get("person"))
            if not person:
                raise Invalid("person is required")
            following = item.get("following", True) is not False
            return {"action": action, "task_id": task["id"], "reason": reason, "before": _snapshot(task),
                    "payload": {"person_id": person["id"], "following": following, "note": note}}
        if action == "add_subtask":
            task = self.store.get_task(int(item["task_id"])) if item.get("task_id") is not None else None
            if not task:
                raise Invalid("task_id is required")
            if not (item.get("title") or "").strip():
                raise Invalid("A subtask needs a title")
            return {"action": action, "task_id": task["id"], "reason": reason, "before": _snapshot(task),
                    "payload": {"title": item["title"].strip(), "body": item.get("body") or ""}}
        if action == "check_subtask":
            if item.get("block_id") is None:
                raise Invalid("block_id is required")
            block = self.notebook.get(int(item["block_id"]))
            if not block["task_id"] or block["kind"] != "subtask":
                raise Invalid("That block isn't a subtask")
            task = self.store.get_task(block["task_id"])
            return {"action": action, "task_id": task["id"], "reason": reason, "before": _snapshot(task),
                    "payload": {"block_id": block["id"], "done": item.get("done", True) is not False, "note": note}}
        fields = self._fields(item)
        if action == "promote_idea":
            if item.get("idea_id") is None:
                raise Invalid("idea_id is required")
            idea = self.ideas.get(int(item["idea_id"]))
            if idea["status"] != "open":
                raise Invalid(f"That idea is already {idea['status']}")
            preview = {"title": idea["title"], **fields}
            if "project_id" not in preview and idea["project_id"]:
                preview["project_id"] = idea["project_id"]
            if "project_id" not in preview:
                preview.setdefault("area", idea["area"])
            self.store._validate(preview, None)
            return {"action": action, "task_id": None, "reason": reason, "before": None,
                    "payload": {"idea_id": idea["id"], "fields": fields, "note": note}}
        task_id = item.get("task_id")
        if action == "create":
            if not fields.get("title"):
                raise Invalid("title is required")
            self.store._validate(fields, None)
            return {"action": action, "task_id": None, "payload": {"fields": fields, "note": note},
                    "before": None, "reason": reason}
        if task_id is None:
            raise Invalid("task_id is required")
        task = self.store.get_task(int(task_id))
        if action == "note":
            if not note:
                raise Invalid("note is required")
            fields = {"status": fields["status"]} if "status" in fields else {}
        elif action == "complete":
            fields = {"status": "done"}
        elif not fields and not note:
            raise Invalid("nothing to change")
        self.store._validate(fields, task)
        return {"action": action, "task_id": task["id"], "payload": {"fields": fields, "note": note},
                "before": _snapshot(task), "reason": reason}

    def _fields(self, item: dict[str, Any]) -> dict[str, Any]:
        """MCP item → Store fields. None = not proposed; "" clears text, dates and project."""
        fields: dict[str, Any] = {}
        for key in ("title", "notes", "status", "area", "due_on", "today", "waiting_on", "external_url"):
            if item.get(key) is not None:
                fields[key] = item[key]
        if item.get("priority") is not None:
            priority = item["priority"]
            fields["priority"] = {v: k for k, v in PRIORITY_LABELS.items()}.get(priority, priority)
        if item.get("project") is not None:
            project = self.store.resolve_project(item["project"])
            fields["project_id"] = project["id"] if project else None
        if item.get("assignee") is not None:
            person = self.people.resolve(item["assignee"])  # "" = back to you
            fields["assignee_id"] = person["id"] if person else None
        return fields

    # ----- reading -----

    def list(self, status: str | None = "pending", limit: int = 50, area: str | None = None) -> list[dict[str, Any]]:
        conditions, params = [], []
        if status:
            conditions.append("cs.status = ?")
            params.append(status)
        if area:
            conditions.append("(cs.area IS NULL OR cs.area = ?)")
            params.append(area)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.store._rows(
            f"""
            SELECT cs.*, c.name AS customer, m.title AS meeting_title, m.held_on AS meeting_on,
                   (SELECT COUNT(*) FROM changes ch WHERE ch.changeset_id = cs.id) AS change_count
            FROM changesets cs
            LEFT JOIN customers c ON c.id = cs.customer_id
            LEFT JOIN meetings m ON m.id = cs.meeting_id
            {where} ORDER BY cs.id DESC LIMIT ?
            """,
            [*params, limit],
        )
        return [dict(r) for r in rows]

    def pending_count(self) -> int:
        return self.store._row("SELECT COUNT(*) FROM changesets WHERE status = 'pending'")[0]

    def get(self, changeset_id: int) -> dict[str, Any]:
        rows = self.list(status=None, limit=100000)
        head = next((r for r in rows if r["id"] == changeset_id), None)
        if not head:
            raise NotFound(f"No proposal with id {changeset_id}")
        changes = self.store._rows("SELECT * FROM changes WHERE changeset_id = ? ORDER BY seq", (changeset_id,))
        head["changes"] = [self._render(dict(c)) for c in changes]
        return head

    def _render(self, change: dict[str, Any]) -> dict[str, Any]:
        """Add the diff lines, the task's current title, and whether it changed since proposed."""
        payload = json.loads(change["payload"])
        before = json.loads(change["before"]) if change["before"] else None
        change["payload"] = payload
        change["before"] = before
        lines: list[dict[str, str]] = []
        current = None
        if change["task_id"]:
            try:
                current = _snapshot(self.store.get_task(change["task_id"]))
            except NotFound:
                current = None
        change["task_title"] = (current or before or {}).get("title") or payload.get("fields", {}).get("title")
        change["stale"] = []

        if change["action"] == "follow":
            try:
                name = self.people.get(payload["person_id"])["name"]
            except NotFound:
                name = "(deleted person)"
            lines.append({"op": "+" if payload["following"] else "-", "field": "follower", "text": name})
        elif change["action"] == "add_subtask":
            lines.append({"op": "+", "field": "subtask", "text": payload["title"]})
            if payload["body"].strip():
                lines.append({"op": "+", "field": "details", "text": payload["body"].strip()})
        elif change["action"] == "check_subtask":
            try:
                block = self.notebook.get(payload["block_id"])
            except NotFound:
                block = None
            label = Notebook.label(block) if block else "(deleted subtask)"
            was, now = ("☐", "☑") if payload["done"] else ("☑", "☐")
            lines.append({"op": "-", "field": "subtask", "text": f"{was} {label}"})
            lines.append({"op": "+", "field": "subtask", "text": f"{now} {label}"})
            if block and block["done"] == payload["done"] and change["status"] == "pending":
                change["stale"].append("subtask already " + ("done" if block["done"] else "open"))
        elif change["action"] == "promote_idea":
            try:
                idea = self.ideas.get(payload["idea_id"])
            except NotFound:
                idea = None
            change["task_title"] = payload["fields"].get("title") or (idea["title"] if idea else None)
            lines.append({"op": " ", "field": "idea", "text": f"#{payload['idea_id']} {idea['title'] if idea else '(deleted)'}"})
            lines.append({"op": "+", "field": "task", "text": change["task_title"] or ""})
            project_id = payload["fields"].get("project_id") or (idea or {}).get("project_id")
            if project_id:
                lines.append({"op": "+", "field": "project", "text": self._show("project_id", project_id)})
            for key, label in FIELDS:
                if key not in ("title", "project_id") and payload["fields"].get(key) not in (None, "", False):
                    lines.append({"op": "+", "field": label, "text": self._show(key, payload["fields"][key])})
            for block in self.notebook.blocks("idea", payload["idea_id"]) if idea else []:
                lines.append({"op": "+", "field": "subtask", "text": Notebook.label(block)})
            if idea and idea["status"] != "open" and change["status"] == "pending":
                change["stale"].append(f"idea already {idea['status']}")
        elif change["action"] == "add_link":
            customer = self.store.get_customer(payload["customer_id"])
            lines.append({"op": " ", "field": "customer", "text": customer["name"]})
            lines.append({"op": "+", "field": "link", "text": f"{payload['label']} → {payload['url']}"})
        elif change["action"] == "create":
            for key, label in FIELDS:
                if key in payload["fields"] and payload["fields"][key] not in (None, "", False):
                    lines.append({"op": "+", "field": label, "text": self._show(key, payload["fields"][key])})
            if "status" not in payload["fields"]:
                lines.append({"op": "+", "field": "status", "text": STATUS_LABELS["todo"]})
        else:
            for key, label in FIELDS:
                if key not in payload["fields"]:
                    continue
                old = before.get(key) if before else None
                new = payload["fields"][key]
                if key == "today":
                    new = bool(new)
                if _norm(old) == _norm(new):
                    lines.append({"op": " ", "field": label, "text": f"{self._show(key, old)} (unchanged)"})
                    continue
                lines.append({"op": "-", "field": label, "text": self._show(key, old)})
                lines.append({"op": "+", "field": label, "text": self._show(key, new)})
                if current and before and _norm(current.get(key)) != _norm(before.get(key)):
                    change["stale"].append(label)
        note = payload.get("note")
        if note:
            lines.append({"op": "+", "field": "note", "text": note})
        if current is None and change["task_id"] and change["status"] == "pending":
            change["stale"].append("task was deleted")
        change["lines"] = lines
        return change

    def _show(self, key: str, value: Any) -> str:
        if value in (None, ""):
            return "you" if key == "assignee_id" else "none"
        if key == "status":
            return STATUS_LABELS.get(value, value)
        if key == "priority":
            return PRIORITY_LABELS.get(value, str(value))
        if key == "project_id":
            try:
                project = self.store.get_project(value)
            except NotFound:
                return f"#{value}"
            return f"{project['name']}" + (f" ({project['customer']})" if project.get("customer") else "")
        if key == "today":
            return "yes" if value else "no"
        if key == "assignee_id":
            row = self.store._row("SELECT name FROM people WHERE id = ?", (value,))
            return row["name"] if row else f"#{value}"
        return str(value)

    # ----- deciding -----

    def decide(
        self, changeset_id: int, *, approve: list[int] | str, edits: dict[int, dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Apply the approved changes (ids, or "all"); reject the rest of the pending ones."""
        cs = self.get(changeset_id)
        if cs["status"] != "pending":
            raise Invalid("This proposal was already decided")
        meeting = None
        if cs["meeting_id"]:
            try:
                meeting = self.hub.get_meeting(cs["meeting_id"])
            except NotFound:
                meeting = None
        approved = {c["id"] for c in cs["changes"]} if approve == "all" else set(approve)
        for change in cs["changes"]:
            if change["status"] != "pending":
                continue
            if change["id"] not in approved:
                self._set(change["id"], "rejected")
                continue
            try:
                with self.store.tx():
                    result = self._apply(change, cs["source"], (edits or {}).get(change["id"]))
                    if change.get("ref") and change["action"] != "add_link":
                        self.ledger.record(result, change["ref"])  # the ref now points at this task
                    if meeting and change["action"] != "add_link":
                        self.hub.link_task(meeting["id"], result, change["action"])
                self._set(change["id"], "applied", result_id=result)
            except (Invalid, NotFound, ValueError) as exc:
                self._set(change["id"], "failed", error=str(exc))
        statuses = [c["status"] for c in self.get(changeset_id)["changes"]]
        status = (
            "applied" if all(s == "applied" for s in statuses)
            else "rejected" if not any(s == "applied" for s in statuses)
            else "partial"
        )
        with self.store.tx() as c:
            c.execute("UPDATE changesets SET status = ?, decided_at = ? WHERE id = ?", (status, now_iso(), changeset_id))
        return self.get(changeset_id)

    def _apply(self, change: dict[str, Any], source: str, edit: dict[str, Any] | None) -> int:
        payload = change["payload"]
        if change["action"] == "add_link":
            link = self.hub.create_link(payload["customer_id"], "link", label=payload["label"], url=payload["url"])
            return link["id"]
        if change["action"] == "follow":
            current = [p["id"] for p in self.store.get_task(change["task_id"])["followers"]]
            pid = payload["person_id"]
            wanted = current + [pid] if payload["following"] else [p for p in current if p != pid]
            self.store.set_followers(change["task_id"], wanted, source=source)
            if payload.get("note"):
                self.store.add_update(change["task_id"], payload["note"], source=source)
            return change["task_id"]
        if change["action"] == "add_subtask":
            self.notebook.add("task", change["task_id"], title=payload["title"], body=payload["body"],
                              kind="subtask", source=source)
            return change["task_id"]
        if change["action"] == "check_subtask":
            self.notebook.update(payload["block_id"], done=payload["done"], source=source)
            if payload.get("note"):
                self.store.add_update(change["task_id"], payload["note"], source=source)
            return change["task_id"]
        if change["action"] == "promote_idea":
            fields = dict(payload.get("fields") or {})
            if edit:
                fields.update({k: v for k, v in edit.items() if k in ("title", "due_on", "project_id", "priority")})
                if edit.get("today"):
                    fields.update(today=True, assignee_id=None)
            task = self.ideas.promote(payload["idea_id"], fields, source=source)
            if payload.get("note"):
                self.store.add_update(task["id"], payload["note"], source=source)
            return task["id"]
        fields = dict(payload.get("fields") or {})
        if edit:  # the user tweaked the proposal in the review (e.g. a better title)
            fields.update({k: v for k, v in edit.items() if k in ("title", "due_on", "project_id", "priority")})
            if change["action"] == "create" and edit.get("today"):
                # "Take it on today": it goes on your list, so it's yours whoever Claude suggested.
                fields.update(today=True, assignee_id=None)
        note = payload.get("note")
        if change["action"] == "create":
            return self.store.create_task(fields, source=source, created_note=note or "")["id"]
        if change["action"] == "note":
            return self.store.add_update(change["task_id"], note, source=source, status=fields.get("status"))["id"]
        return self.store.update_task(change["task_id"], fields, source=source, note=note)["id"]

    def _set(self, change_id: int, status: str, result_id: int | None = None, error: str | None = None) -> None:
        with self.store.tx() as c:
            c.execute(
                "UPDATE changes SET status = ?, result_id = ?, error = ? WHERE id = ?",
                (status, result_id, error, change_id),
            )


def _snapshot(task: dict[str, Any]) -> dict[str, Any]:
    keys = ("title", "status", "project_id", "area", "priority", "due_on", "waiting_on", "external_url", "notes",
            "assignee_id")
    out = {k: task.get(k) for k in keys}
    out["today"] = bool(task.get("today"))
    return out


def _norm(value: Any) -> Any:
    return None if value in ("", None) else value


def diff_text(changeset: dict[str, Any]) -> str:
    """The changeset as plain text, for Claude to show the user."""
    out = [f"Proposal #{changeset['id']} · {changeset['source']} · {changeset['status']}"]
    for change in changeset["changes"]:
        head = change["action"]
        if change["task_id"]:
            head += f" #{change['task_id']} {change['task_title'] or ''}"
        elif change["action"] in ("create", "promote_idea"):
            head += f" {change['task_title'] or ''}"
        out.append(f"\n{head.strip()}" + (f"   [{change['status']}]" if change["status"] != "pending" else ""))
        if change["reason"]:
            out.append(f"  # {change['reason']}")
        for line in change["lines"]:
            out.append(f"{line['op']} {line['field']}: {line['text']}")
    return "\n".join(out)
