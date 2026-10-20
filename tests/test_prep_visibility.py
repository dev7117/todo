"""Prep is one queue item, expires on the meeting boundary, and retains history."""
from datetime import datetime, timedelta

from todo_app.attachments import Attachments
from todo_app.cadences import Cadences
from todo_app.hub import Hub
from todo_app.notebook import Notebook
from todo_app.people import People


def prep(store, tmp_path):
    hub = Hub(store, tmp_path)
    cad = Cadences(store, hub, Attachments(store, tmp_path))
    customer = store.create_customer("Acme")
    project = store.create_project("Ops", area="work", customer="Acme")
    cadence = cad.create(customer["id"], "Review", {"calendar": "Review"},
                         project_id=project["id"], prep_days=3)
    cad.set_steps(cadence["id"], [{"title": "Read report", "due_hours_before": 48},
                                {"title": "Draft agenda", "due_hours_before": 24}])
    start = "2026-10-05T09:30:00-04:00"
    store.now = lambda: datetime.fromisoformat(start) - timedelta(seconds=1)
    hub.sync_calendar([{"calendar_id": "series-instance-20261005", "customer_id": customer["id"],
                        "title": "Review", "starts_at": start}],
                      window_start="2026-10-05", window_end="2026-10-06")
    occ = cad.prepare(cadence["id"])
    return hub, cad, customer, project, occ, datetime.fromisoformat(start)


def ids(tasks):
    return {t["id"] for t in tasks}


def test_one_prep_parent_expires_at_start_without_writes(store, tmp_path):
    hub, cad, customer, project, occ, start = prep(store, tmp_path)
    root = occ["prep_task_id"]
    children = [s["task"]["id"] for s in occ["steps"]]
    for task_id in [root, *children]:
        store.update_task(task_id, {"today": True})
    unrelated = store.create_task({"title": "Meeting follow-up", "due_on": "2026-10-04",
                                   "today": True, "project_id": project["id"]}, source="meeting: Review")
    # A regular grouped task is not mistaken for a generated prep step.
    ordinary_child = store.create_task({"title": "Separate commitment"})
    with store.tx() as c:
        c.execute("UPDATE tasks SET parent_id = ? WHERE id = ?", (root, ordinary_child["id"]))
    expected = {root, unrelated["id"], ordinary_child["id"]}
    assert ids(store.list_tasks()) == expected
    assert ids(store.today_view()["open"]) == {root, unrelated["id"]}
    assert store.counts()["overdue"] == 1
    assert store.counts()["open"] == 3
    assert store.list_projects()[0]["open_count"] == 2
    assert store.list_customers()[0]["open_count"] == 2
    assert store.get_task(root)["children_total"] == 3
    assert all(not store.get_task(i)["overdue"] for i in children)
    before = list(store.conn.iterdump())

    # No ensure(), hourly timer, status change, or cleanup write is needed.
    store.now = lambda: start
    assert ids(store.list_tasks()) == {unrelated["id"], ordinary_child["id"]}
    assert ids(store.today_view()["open"]) == {unrelated["id"]}
    assert {t["id"] for t in store.bar_state()["tasks"]} == {unrelated["id"]}
    assert store.counts()["today"] == 1
    assert store.counts()["open"] == 2
    assert store.list_customers()[0]["open_count"] == 1
    assert store.list_projects()[0]["open_count"] == 1
    assert not store.search("Prep", include_closed=False)
    assert root in ids(store.search("Prep", include_closed=True))
    assert cad.occurrence(occ["id"])["steps"][0]["task"]["status"] == "todo"
    assert list(store.conn.iterdump()) == before


def test_completed_history_and_ordinary_groups_preserved(store, tmp_path):
    _, _, _, _, occ, start = prep(store, tmp_path)
    child = occ["steps"][0]["task"]["id"]
    store.update_task(child, {"status": "done", "today": True})
    store.update_task(occ["prep_task_id"], {"status": "done", "today": True})
    history = store.task_updates(child)
    store.now = lambda: start + timedelta(days=1)
    closed = ids(store.list_tasks(status=["done"]))
    assert {child, occ["prep_task_id"]} <= closed
    assert store.task_updates(child) == history
    a = store.create_task({"title": "Regular one"})
    b = store.create_task({"title": "Regular two"})
    with store.tx() as c:
        c.execute("UPDATE tasks SET parent_id = ? WHERE id = ?", (a["id"], b["id"]))
    assert {a["id"], b["id"]} <= ids(store.list_tasks())


def test_cancelled_meeting_and_skipped_occurrence_hide_prep(store, tmp_path):
    hub, cad, _, _, occ, _ = prep(store, tmp_path)
    hub.sync_calendar([], window_start="2026-10-05", window_end="2026-10-06")
    assert not store.list_tasks()
    assert store.get_task(occ["prep_task_id"])["status"] == "todo"
    cad.ensure()
    assert cad.occurrence(occ["id"])["status"] == "skipped"


def test_current_calendar_start_and_offsets_control_expiry(store, tmp_path):
    _, _, _, _, occ, start = prep(store, tmp_path)
    # UTC representation of a rescheduled start; no local-date/string comparison.
    moved = "2026-10-05T15:00:00+00:00"
    with store.tx() as c:
        c.execute("UPDATE meetings SET starts_at = ? WHERE id = ?", (moved, occ["meeting_id"]))
    store.now = lambda: start
    assert ids(store.list_tasks()) == {occ["prep_task_id"]}
    store.now = lambda: datetime.fromisoformat(moved)
    assert not store.list_tasks()


def test_people_counts_match_single_visible_prep(store, tmp_path):
    hub, _, _, _, occ, start = prep(store, tmp_path)
    people = People(store, hub, Notebook(store))
    person = people.create("Colleague")
    for task_id in [occ["prep_task_id"], *[s["task"]["id"] for s in occ["steps"]]]:
        store.update_task(task_id, {"assignee_id": person["id"]})
    assert people.list()[0]["assigned_open"] == 1
    assert people.list()[0]["overdue"] == 0
    store.now = lambda: start
    assert people.list()[0]["assigned_open"] == 0


def test_recurring_instances_are_independent_and_ensure_is_idempotent(store, tmp_path):
    hub, cad, customer, _, occ, start = prep(store, tmp_path)
    hub.sync_calendar([
        {"calendar_id": "series-instance-20261005", "customer_id": customer["id"],
         "title": "Review", "starts_at": start.isoformat()},
        {"calendar_id": "series-instance-20261006", "customer_id": customer["id"],
         "title": "Review", "starts_at": (start + timedelta(days=1)).isoformat()},
    ], window_start="2026-10-05", window_end="2026-10-06")
    made = cad.ensure()["created"]
    assert len(made) == 1
    assert cad.ensure()["created"] == []
    next_root = cad.occurrence(made[0])["prep_task_id"]
    assert ids(store.list_tasks()) == {occ["prep_task_id"], next_root}
    store.now = lambda: start
    assert ids(store.list_tasks()) == {next_root}


def test_rest_task_list_and_counts_expire_without_scheduler(client, store, tmp_path, monkeypatch):
    from todo_app import api

    _, _, _, _, occ, start = prep(store, tmp_path)
    monkeypatch.setattr(api, "store", store)
    assert ids(client.get("/api/tasks").json()) == {occ["prep_task_id"]}
    assert client.get("/api/counts").json()["open"] == 1
    store.now = lambda: start
    assert client.get("/api/tasks").json() == []
    assert client.get("/api/counts").json()["open"] == 0
    assert client.get("/api/today").json()["open"] == []
