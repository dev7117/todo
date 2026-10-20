"""People: who you assign tasks to and work with, and the 1:1 view of each. Some are agents
(kind 'agent'): Claude Code running on your machines, set up in dispatch.py.

A person can be assigned tasks (tasks.assignee_id; NULL means you) and follow tasks (task_people:
things to discuss or keep them in the loop on; the task stays yours, and @mentions add them). They may work across several customers and projects; their page
gathers it all: what needs a follow-up, what they're on, what you're doing together, what
they finished lately, meetings they were in, and a notebook of 1:1 notes.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .hub import Hub
from .notebook import Notebook
from .store import AREAS, CLOSED_STATUSES, OPEN_STATUSES, Invalid, NotFound, Store, now_iso

# An agent is a person too: assign it tasks and it works them (todo_app/dispatch.py).
KINDS = ("human", "agent")


class People:
    def __init__(self, store: Store, hub: Hub, notebook: Notebook) -> None:
        self.store = store
        self.hub = hub
        self.notebook = notebook

    def list(self, *, area: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
        today = self.store.today().isoformat()
        rows = self.store._rows(
            f"""
            SELECT pe.*, c.name AS customer,
              (SELECT COUNT(*) FROM tasks t WHERE t.assignee_id = pe.id AND t.status IN {OPEN_STATUSES}
                 AND {self.store._VISIBLE_TASK}) AS assigned_open,
              (SELECT COUNT(*) FROM tasks t WHERE t.assignee_id = pe.id AND t.status IN {OPEN_STATUSES}
                 AND {self.store._VISIBLE_TASK} AND t.due_on < ?) AS overdue,
              (SELECT COUNT(*) FROM task_people tp JOIN tasks t ON t.id = tp.task_id
                 WHERE tp.person_id = pe.id AND t.status IN {OPEN_STATUSES}
                 AND {self.store._VISIBLE_TASK}) AS following_open,
              (SELECT MAX(t.updated_at) FROM tasks t WHERE t.assignee_id = pe.id
                 OR EXISTS (SELECT 1 FROM task_people tp WHERE tp.task_id = t.id AND tp.person_id = pe.id)) AS last_activity
            FROM people pe LEFT JOIN customers c ON c.id = pe.customer_id
            WHERE (? OR pe.archived = 0) AND (? IS NULL OR pe.area = ?)
            ORDER BY pe.archived, lower(pe.name)
            """,
            (today, include_archived, area, area),
        )
        return [{**dict(r), "archived": bool(r["archived"])} for r in rows]

    def get(self, person_id: int) -> dict[str, Any]:
        row = self.store._row(
            "SELECT pe.*, c.name AS customer FROM people pe LEFT JOIN customers c ON c.id = pe.customer_id"
            " WHERE pe.id = ?",
            (person_id,),
        )
        if not row:
            raise NotFound(f"No person with id {person_id}")
        return {**dict(row), "archived": bool(row["archived"])}

    def find(self, ref: str) -> dict[str, Any] | None:
        """By email, full name, or a first name / name prefix when it's unambiguous
        (case-insensitive; '-', '_' and '.' count as spaces, for +first-last in quick add)."""
        key = " ".join((ref or "").replace("-", " ").replace("_", " ").replace(".", " ").split()).lower()
        if not key:
            return None
        rows = self.store._rows("SELECT id, name, email FROM people WHERE archived = 0")
        for r in rows:
            if (r["email"] or "").lower() == (ref or "").strip().lower() or r["name"].lower() == key:
                return self.get(r["id"])
        matches = [r for r in rows if r["name"].lower().startswith(key)]
        return self.get(matches[0]["id"]) if len(matches) == 1 else None

    def resolve(self, ref: int | str | None) -> dict[str, Any] | None:
        if ref in (None, ""):
            return None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            return self.get(int(ref))
        found = self.find(str(ref))
        if not found:
            raise NotFound(f"No person matching {ref!r} (or more than one). Use their full name or id.")
        return found

    def _fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if fields.get("name") is not None:
            if not fields["name"].strip():
                raise Invalid("A person needs a name")
            out["name"] = fields["name"].strip()
        if "email" in fields:
            out["email"] = (fields["email"] or "").strip() or None
        for key in ("title", "notes"):
            if fields.get(key) is not None:
                out[key] = fields[key].strip() if key == "title" else fields[key]
        if "customer_id" in fields:
            if fields["customer_id"] is not None:
                self.store.get_customer(fields["customer_id"])
            out["customer_id"] = fields["customer_id"]
        if fields.get("area") is not None:
            if fields["area"] not in AREAS:
                raise Invalid(f"area must be one of {', '.join(AREAS)}")
            out["area"] = fields["area"]
        if fields.get("archived") is not None:
            out["archived"] = int(bool(fields["archived"]))
        if fields.get("kind") is not None:
            if fields["kind"] not in KINDS:
                raise Invalid(f"kind must be one of {', '.join(KINDS)}")
            out["kind"] = fields["kind"]
        return out

    def create(self, name: str, **fields: Any) -> dict[str, Any]:
        cols = self._fields({"name": name, **fields})
        if "name" not in cols:
            raise Invalid("A person needs a name")
        self._unique_email(cols.get("email"), None)
        ts = now_iso()
        cols.update(created_at=ts, updated_at=ts)
        with self.store.tx() as c:
            cur = c.execute(
                f"INSERT INTO people ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values())
            )
        return self.get(cur.lastrowid)

    def update(self, person_id: int, **fields: Any) -> dict[str, Any]:
        self.get(person_id)
        cols = self._fields(fields)
        if "email" in cols:
            self._unique_email(cols["email"], person_id)
        if cols:
            cols["updated_at"] = now_iso()
            with self.store.tx() as c:
                c.execute(f"UPDATE people SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), person_id))
        return self.get(person_id)

    def delete(self, person_id: int) -> None:
        """Remove a person; their tasks come back to you and their 1:1 notes go with them."""
        self.get(person_id)
        with self.store.tx() as c:
            c.execute("DELETE FROM people WHERE id = ?", (person_id,))

    def _unique_email(self, email: str | None, person_id: int | None) -> None:
        if not email:
            return
        row = self.store._row("SELECT id, name FROM people WHERE lower(email) = lower(?)", (email,))
        if row and row["id"] != person_id:
            raise Invalid(f"{row['name']} already has the email {email}")

    # ----- the person page / 1:1 view -----

    def view(self, person_id: int, *, area: str | None = None) -> dict[str, Any]:
        person = self.get(person_id)
        today = self.store.today()
        soon = (today + timedelta(days=7)).isoformat()
        since = (today - timedelta(days=30)).isoformat()
        assigned = self.store.list_tasks(assignee_id=person_id, area=area, limit=1000)
        following = self.store.list_tasks(following=person_id, area=area, limit=1000)
        done = [
            t
            for t in self.store.list_tasks(status=list(CLOSED_STATUSES), assignee_id=person_id, area=area, limit=200)
            + self.store.list_tasks(status=list(CLOSED_STATUSES), following=person_id, area=area, limit=200)
            if t["status"] == "done" and (t["completed_at"] or "") >= since
        ]
        done = sorted({t["id"]: t for t in done}.values(), key=lambda t: t["completed_at"] or "", reverse=True)

        # What to raise in the 1:1: overdue, due within a week, or waiting on them.
        def urgency(t: dict[str, Any]) -> tuple:
            return (0 if t["overdue"] else 1 if t["due_on"] and t["due_on"] <= soon else 2 if t["status"] == "waiting" else 3,
                    t["due_on"] or "9999", t["id"])

        follow_up = sorted(
            [t for t in assigned + following
             if t["overdue"] or (t["due_on"] and t["due_on"] <= soon) or (t["status"] == "waiting" and t["assignee_id"] == person_id)],
            key=urgency,
        )
        follow_up = list({t["id"]: t for t in follow_up}.values())

        # Customers and projects you share with them.
        shared: dict[tuple, dict[str, Any]] = {}
        for t in assigned + following:
            key = (t["customer_id"], t["project_id"])
            entry = shared.setdefault(key, {"customer_id": t["customer_id"], "customer": t["customer"],
                                            "project_id": t["project_id"], "project": t["project"], "open": 0})
            entry["open"] += 1

        return {
            "person": person,
            "counts": {
                "assigned_open": len(assigned),
                "following_open": len(following),
                "overdue": sum(1 for t in assigned if t["overdue"]),
                "waiting": sum(1 for t in assigned if t["status"] == "waiting"),
                "done_30d": len(done),
            },
            "follow_up": follow_up,
            "assigned": sorted(assigned, key=urgency),
            "following": following,
            "done_recently": done[:20],
            "shared": sorted(shared.values(), key=lambda e: (e["customer"] is None, e["customer"] or "", e["project"] or "")),
            "meetings": self.meetings_with(person, area=area),
            "notes": self.notebook.blocks("person", person_id),
            "blocks": self.notebook.blocks("person", person_id),  # same, for the shared notebook UI
        }

    def meetings_with(self, person: dict[str, Any], *, area: str | None = None, limit: int = 12) -> list[dict[str, Any]]:
        """Meetings they were in: their name or email appears among the attendees."""
        if area == "personal":
            return []
        terms = [person["name"]] + ([person["email"]] if person.get("email") else [])
        where = " OR ".join("lower(m.attendees) LIKE ?" for _ in terms)
        rows = self.store._rows(
            self.hub._MEETING_SELECT + f" WHERE m.status <> 'cancelled' AND ({where})"
            " ORDER BY m.held_on DESC, m.starts_at DESC LIMIT ?",
            [*(f"%{t.lower()}%" for t in terms), limit],
        )
        return [dict(r) for r in rows]
