"""Offline contract tests using invented, recorded REST response shapes."""
import copy
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from todo_v2 import config, migrate
from todo_v2.agent_work import AgentWork, Response, approved, ci_state, iso, linked_issues, project
from todo_v2.app import create_app

FIXTURE = json.loads((Path(__file__).parent / "fixtures/github/responses.json").read_text())
NOW = 1795777200.0
TOKEN = "distinctive-fake-credential-never-return-this-value"


class FakeGitHub:
    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)
        self.calls = []
        self.status = 200
        self.headers = {}

    def __call__(self, url, headers):
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        parsed = urlsplit(url)
        assert parsed.netloc == "api.github.com"
        query = parse_qs(parsed.query)
        key = parsed.path
        if key.endswith("/issues"):
            key += "?state=" + query["state"][0]
        elif key.endswith("/actions/runs"):
            key += "?head_sha=" + query["head_sha"][0]
        assert query.get("page", ["1"]) == ["1"]
        data = self.data[key]
        etag = '"' + str(hash(json.dumps(data, sort_keys=True))) + '"'
        status = 304 if self.status == 200 and headers.get("If-None-Match") == etag else self.status
        self.calls.append((url, status))
        return Response(status, {"ETag": etag, **self.headers}, copy.deepcopy(data))


def setup_reader(tmp_path):
    cfg = replace(config.load(), db_path=tmp_path / "v2.db", github_token=TOKEN,
                  github_repos=("example/repo-a", "example/repo-b"), v1_db_path=None)
    fake = FakeGitHub()
    now = [NOW]
    reader = AgentWork(cfg, fake, lambda: now[0])
    return cfg, fake, now, reader


def raw_pr():
    return copy.deepcopy(FIXTURE["/repos/example/repo-a/pulls/11"])


def run(conclusion="success", status="completed", **kwargs):
    return {"workflow_id": 1, "id": 10, "run_number": 1, "run_attempt": 1, "head_sha": "head-a",
            "path": ".github/workflows/test.yml", "status": status, "conclusion": conclusion, **kwargs}


def review(state="APPROVED", user="reviewer-fern", **kwargs):
    return {"id": 1, "state": state, "user": {"login": user}, "submitted_at": iso(NOW - 100), **kwargs}


@pytest.mark.parametrize("draft,ci,reviews,expected", [
    (True, "failure", [review()], "draft"),
    (True, None, [review()], "draft"),
    (False, "failure", [review()], "CI failing"),
    (False, "timed_out", [], "CI failing"),
    (False, "cancelled", [], "CI failing"),
    (False, None, [review()], "CI running"),
    (False, "success", [review()], "approved"),
    (False, "success", [], "in review"),
    (False, "absent", [], "in review"),
])
def test_pr_states_and_precedence(draft, ci, reviews, expected):
    pr = raw_pr()
    pr["draft"] = draft
    runs = [] if ci == "absent" else [run(ci, "in_progress" if ci is None else "completed")]
    if ci == "failure":
        runs += [run(None, "queued", workflow_id=2)]
    assert project("example/repo-a", pr, reviews, runs)["state"] == expected


def test_closed_and_merged():
    pr = raw_pr()
    pr.update(state="closed", closed_at=iso(NOW), draft=True)
    assert project("example/repo-a", pr)["state"] == "closed"
    pr["merged_at"] = iso(NOW)
    assert project("example/repo-a", pr)["state"] == "merged"


@pytest.mark.parametrize("changes,ci,reviews,expected", [
    ({}, "success", [review()], True),
    ({"draft": True}, "success", [review()], False),
    ({}, "failure", [review()], False),
    ({}, "pending", [review()], False),
    ({"mergeable_state": "dirty"}, "success", [review()], False),
    ({"mergeable": None}, "success", [review()], False),
    ({}, "absent", [review()], False),
    ({}, "success", [], False),
    ({"state": "closed"}, "success", [review()], False),
    ({"labels": [{"name": "needs-dev"}], "draft": True}, "failure", [], True),
])
def test_waiting(changes, ci, reviews, expected):
    pr = raw_pr()
    pr.update(changes)
    runs = [] if ci == "absent" else [run(None, "in_progress") if ci == "pending" else run(ci)]
    assert project("example/repo-a", pr, reviews, runs)["waiting_on_dev"] is expected


def test_latest_reviews_and_change_requests():
    assert approved([review()])
    assert not approved([review(), review("CHANGES_REQUESTED", id=2)])
    assert approved([review("CHANGES_REQUESTED"), review(id=2)])
    assert not approved([review(), review("CHANGES_REQUESTED", user="reviewer-oak")])
    assert not approved([review(), review("DISMISSED", id=2)])
    assert approved([review(), review("COMMENTED", id=2)])
    assert not approved([review("PENDING")])


@pytest.mark.parametrize("keyword", ["Closes", "Fixes", "Resolves", "Refs", "Part of"])
def test_same_repo_links(keyword):
    assert linked_issues(f"{keyword} #7", "example/repo-a") == [7]
    assert linked_issues(f"{keyword} example/repo-a#7", "example/repo-a") == [7]
    assert linked_issues(f"{keyword} https://github.com/example/repo-a/issues/7", "example/repo-a") == [7]
    assert linked_issues(f"{keyword} example/repo-b#7", "example/repo-a") == []
    assert linked_issues(f"{keyword} https://github.com/example/repo-b/issues/7", "example/repo-a") == []


def test_actions_latest_per_workflow_head_and_ignored_workflow():
    assert ci_state([run("failure"), run(id=11, run_number=2)], "head-a") == "green"
    assert ci_state([run("failure"), run(id=11, run_attempt=2)], "head-a") == "green"
    assert ci_state([run(), run("failure", workflow_id=2)], "head-a") == "failing"
    assert ci_state([run(), run("failure", head_sha="old-head", workflow_id=2)], "head-a") == "green"
    ignored = run("failure", workflow_id=2, path=".github/workflows/claude.yml")
    assert ci_state([ignored], "head-a") == "absent"
    assert ci_state([run(), ignored], "head-a") == "green"
    assert ci_state([run(None, "queued")], "head-a") == "running"


def test_poll_grouping_recent_window_and_etags(tmp_path):
    cfg, fake, now, reader = setup_reader(tmp_path)
    reader.refresh()
    payload = reader.payload()
    assert payload["last_error"] is None
    assert len(payload["repos"]) == 2
    a = {i["number"]: i for i in payload["repos"][0]["items"]}
    assert a[1]["state"] == "in progress"
    assert a[1]["prs"][0]["number"] == 11
    assert a[2]["state"] == "issue open"
    assert a[2]["waiting_on_dev"]
    assert {i["state"] for i in payload["recent"]} == {"merged", "closed"}
    assert 99 not in {i["number"] for i in payload["recent"]}
    assert len(payload["waiting_on_dev"]) == 2
    initial_count = len(fake.calls)
    now[0] += 300
    reader.refresh()
    assert len(fake.calls) == initial_count * 2
    assert {status for _, status in fake.calls[initial_count:]} == {304}
    assert reader.payload()["repos"] == payload["repos"]
    assert reader.payload()["recent"] == payload["recent"]
    assert all("checks" not in url for url, _ in fake.calls)
    assert not any("/pulls/21" in url for url, _ in fake.calls)


@pytest.mark.parametrize("merged", [False, True])
def test_closed_pr_uses_issue_list_merge_timestamp(tmp_path, merged):
    cfg, fake, now, reader = setup_reader(tmp_path)
    raw = fake.data["/repos/example/repo-a/issues?state=closed"][1]
    raw["pull_request"]["merged_at"] = iso(NOW - 120) if merged else None
    reader.refresh()
    assert reader.last_error is None
    item = next(item for item in reader.recent if item["number"] == 21)
    assert item["kind"] == "pr"
    assert item["url"] == "https://github.com/example/repo-a/pull/21"
    assert item["state"] == ("merged" if merged else "closed")
    assert item["closed_at"] == (iso(NOW - 120) if merged else raw["closed_at"])
    assert not any("/pulls/21" in url for url, _ in fake.calls)


def test_refresh_floor_and_label_changes(tmp_path):
    cfg, fake, now, reader = setup_reader(tmp_path)
    reader.refresh()
    count = len(fake.calls)
    now[0] += 59
    reader.refresh()
    assert len(fake.calls) == count
    fake.data["/repos/example/repo-a/issues?state=open"][1]["labels"] = []
    now[0] += 1
    reader.refresh()
    assert len(fake.calls) > count
    assert len(reader.waiting) == 1


@pytest.mark.parametrize("status,headers", [
    (403, {}), (429, {"Retry-After": "600"}),
    (200, {"X-RateLimit-Remaining": "200", "X-RateLimit-Reset": str(NOW + 900)}),
])
def test_backoff_preserves_snapshot_and_safe_error(tmp_path, status, headers):
    cfg, fake, now, reader = setup_reader(tmp_path)
    reader.refresh()
    before = reader.payload()["repos"]
    now[0] += 60
    fake.status, fake.headers = status, headers
    reader.refresh()
    assert "cooldown" in reader.last_error
    assert reader.payload()["repos"] == before
    assert TOKEN not in json.dumps(reader.payload())
    count = len(fake.calls)
    now[0] = reader.next_refresh - 1
    reader.refresh()
    assert len(fake.calls) == count
    now[0] += 1
    fake.status, fake.headers = 200, {}
    reader.refresh()
    assert reader.last_error is None


def test_quota_above_backoff_threshold_can_poll(tmp_path):
    cfg, fake, now, reader = setup_reader(tmp_path)
    fake.headers = {"X-RateLimit-Remaining": "201"}
    reader.refresh()
    assert reader.last_error is None
    assert reader.payload()["repos"]


def test_unauthorized_poll_never_leaks(tmp_path, monkeypatch):
    cfg, fake, now, reader = setup_reader(tmp_path)
    fake.status = 401
    fake.data["/repos/example/repo-a/issues?state=open"] = {"message": f"Bad credentials: {TOKEN}"}
    reader.refresh()
    message = "GitHub could not be read. Check access and try again later."
    assert reader.last_error == message
    assert reader.payload()["last_error"] == message
    assert TOKEN not in json.dumps(reader.payload())
    monkeypatch.setattr(reader, "start", lambda: None)
    with TestClient(create_app(cfg, agent_work=reader)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["github"]["last_error"] == message
        assert TOKEN not in health.text
        assert TOKEN not in client.get("/api/agent-work").text


def test_transport_exception_never_leaks(tmp_path):
    cfg, fake, now, reader = setup_reader(tmp_path)
    def fail(*args):
        raise RuntimeError(TOKEN)
    reader.transport = fail
    reader.refresh()
    assert reader.last_error == "GitHub refresh failed. Check access and try again later."
    assert TOKEN not in json.dumps(reader.payload())


@pytest.mark.parametrize("token,repos", [("", ("example/repo-a",)), (TOKEN, ()), ("", ())])
def test_unconfigured_and_existing_routes(tmp_path, token, repos):
    cfg, fake, now, reader = setup_reader(tmp_path)
    cfg = replace(cfg, github_token=token, github_repos=repos)
    reader = AgentWork(cfg, fake)
    with TestClient(create_app(cfg, agent_work=reader)) as client:
        assert client.post("/api/agent-work/refresh").json()["configured"] is False
        assert client.get("/health").json()["github"] == {"configured": False, "last_poll_at": None, "last_error": None}
        assert client.get("/api/tasks").status_code == 200
        assert client.post("/api/tasks", json={"title": "Plan the garden"}).status_code == 201
        assert client.get("/api/from-v1").json() == {"enabled": False, "items": []}
    assert fake.calls == []


def test_token_absent_from_all_json_and_database(tmp_path, monkeypatch, caplog):
    cfg, fake, now, reader = setup_reader(tmp_path)
    fake.data["/repos/example/repo-a/issues?state=open"][0]["title"] = TOKEN
    reader.refresh()
    monkeypatch.setattr(reader, "start", lambda: None)
    with TestClient(create_app(cfg, agent_work=reader)) as client:
        for path in ("/api/agent-work", "/health", "/api/tasks", "/api/from-v1", "/openapi.json", "/api/nope"):
            assert TOKEN not in client.get(path).text
        assert TOKEN not in client.post("/api/agent-work/refresh").text
        assert client.get("/health").json()["github"]["configured"] is True
        assert client.get("/health").json()["schema_version"] == migrate.discover()[-1].version
    assert TOKEN not in repr(cfg)
    assert TOKEN not in caplog.text
    assert TOKEN.encode() not in cfg.db_path.read_bytes()
    with sqlite3.connect(cfg.db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fts_tables = {f"{source}_fts{suffix}" for source in ("tasks", "ideas", "blocks")
                  for suffix in ("", "_data", "_idx", "_docsize", "_config")}
    assert tables == {"schema_migrations", "tasks", "task_history", "projects", "ideas", "blocks", "sqlite_sequence"} | fts_tables


@pytest.mark.parametrize("value,expected", [("0", 5), ("4", 5), ("5", 5), ("9", 9), ("16", 15), ("oops", 5)])
def test_config_clamp(monkeypatch, value, expected):
    monkeypatch.setenv("TODO_V2_GITHUB_POLL_MINUTES", value)
    monkeypatch.setenv("TODO_V2_GITHUB_REPOS", "example/repo-a, example/repo-b,example/repo-a,invalid,../bad")
    cfg = config.load()
    assert cfg.github_poll_minutes == expected
    assert cfg.github_repos == ("example/repo-a", "example/repo-b")


def test_config_ignores_api_base_environment(monkeypatch):
    monkeypatch.setenv("TODO_V2_GITHUB_API_BASE", "http://example.invalid")
    assert config.load().github_api_base == "https://api.github.com"


def test_pagination_revalidates_every_page(tmp_path):
    cfg, fake, now, reader = setup_reader(tmp_path)
    calls = []
    def paged(url, headers):
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        status = 304 if headers.get("If-None-Match") else 200
        calls.append(status)
        return Response(status, {"ETag": str(page), "Link": '<unused>; rel="next"' if page == 1 else ""}, [{"number": page}])
    reader.transport = paged
    touched = {}
    assert reader._pages("/repos/example/repo-a/issues", touched) == [{"number": 1}, {"number": 2}]
    reader.cache = touched
    assert reader._pages("/repos/example/repo-a/issues", {}) == [{"number": 1}, {"number": 2}]
    assert calls == [200, 200, 304, 304]


def test_get_refresh_is_404(tmp_path, monkeypatch):
    cfg, fake, now, reader = setup_reader(tmp_path)
    monkeypatch.setattr(reader, "start", lambda: None)
    with TestClient(create_app(cfg, agent_work=reader)) as client:
        assert client.get("/api/agent-work/refresh").status_code == 404


def test_http_refresh_floor_and_safe_health_error(tmp_path, monkeypatch):
    cfg, fake, now, reader = setup_reader(tmp_path)
    monkeypatch.setattr(reader, "start", lambda: None)
    with TestClient(create_app(cfg, agent_work=reader)) as client:
        assert client.get("/api/agent-work").status_code == 200
        assert client.get("/api/agent-work?refresh=true").status_code == 200
        assert fake.calls == []
        response = client.post("/api/agent-work/refresh")
        assert response.status_code == 200
        assert response.json()["last_error"] is None
        count = len(fake.calls)
        now[0] += 59
        client.post("/api/agent-work/refresh")
        assert len(fake.calls) == count
        now[0] += 1
        fake.status = 429
        body = client.post("/api/agent-work/refresh").json()
        assert "cooldown" in body["last_error"]
        assert client.get("/health").json()["github"]["last_error"] == body["last_error"]


def test_recent_items_expire_even_on_304(tmp_path):
    cfg, fake, now, reader = setup_reader(tmp_path)
    reader.refresh()
    now[0] += 8 * 86400
    reader.refresh()
    assert reader.recent == []


def test_background_polls_and_stops(tmp_path):
    import threading
    cfg, fake, now, reader = setup_reader(tmp_path)
    finished = threading.Event()
    original = reader.refresh
    def poll():
        original()
        finished.set()
    reader.refresh = poll
    reader.start()
    assert finished.wait(2)
    reader.close()
    assert not reader.worker.is_alive()
    assert reader.last_updated_at == iso(NOW)
