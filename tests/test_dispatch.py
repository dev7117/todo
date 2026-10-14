"""Agent assignees: assign a task to an agent, a machine runs it, the agent reports over /mcp/agent."""
import json
from datetime import datetime, timedelta, timezone

from tests.test_mcp import AUTH, call

TOKEN = {"Authorization": "Bearer test-token"}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def poll(client, name="Omarchy", version="1.2", **extra):
    return client.post("/api/agent/poll", json={"name": name, "platform": "linux", "version": version, **extra},
                       headers=TOKEN).json()["run"]


def report(client, run_id, name="Omarchy", **fields):
    return client.post(f"/api/agent/runs/{run_id}", json={"name": name, **fields}, headers=TOKEN).json()


def worker(client, token, tool, **arguments):
    response = client.post("/mcp/agent", headers={**ACCEPT, "Authorization": f"Bearer {token}"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": tool, "arguments": arguments}})
    if response.status_code != 200:
        return True, response.status_code
    result = response.json()["result"]
    if result.get("isError"):
        return True, result["content"][0]["text"]
    payload = result.get("structuredContent")
    if payload is None:
        payload = json.loads(result["content"][0]["text"])
    return False, payload


def setup(client, *, machine="Omarchy"):
    client.post("/api/agent/ping", json={"name": machine, "platform": "linux", "version": "1.2"}, headers=TOKEN)
    project = client.post("/api/projects", json={"name": "todo", "area": "work", "repo_path": "~/Work/todo-app"}).json()
    agent = client.post("/api/people", json={"name": "Maintainer", "kind": "agent"}).json()
    profile = client.put(f"/api/people/{agent['id']}/agent", json={"claude_agent": "maintainer"}).json()
    task = client.post("/api/tasks", json={"title": "Add version to /health", "project_id": project["id"],
                                            "notes": "Return the git sha too."}).json()
    return project, profile, task


def assign(client, task, agent):
    return client.patch(f"/api/tasks/{task['id']}", json={"assignee_id": agent["id"]}).json()


def test_assigning_queues_one_run(client):
    _, agent, task = setup(client)
    assign(client, task, agent)
    runs = client.get(f"/api/tasks/{task['id']}/runs").json()
    assert len(runs) == 1 and runs[0]["kind"] == "task" and runs[0]["status"] == "queued"
    assert runs[0]["agent"] == "Omarchy" and runs[0]["branch"] == f"agent/{task['id']}-add-version-to-health"
    # Assigning again (or starting it by hand) doesn't double up.
    client.patch(f"/api/tasks/{task['id']}", json={"assignee_id": None})
    assign(client, task, agent)
    assert len(client.get(f"/api/tasks/{task['id']}/runs").json()) == 1
    assert "already on it" in client.post(f"/api/tasks/{task['id']}/dispatch", json={}).json()["detail"]
    history = client.get(f"/api/tasks/{task['id']}").json()["updates"]
    assert any("Queued for Maintainer on Omarchy" in u["body"] for u in history)


def test_general_work_without_a_repo(client):
    _, agent, _ = setup(client)
    other = client.post("/api/projects", json={"name": "No repo", "area": "work"}).json()
    for task in (client.post("/api/tasks", json={"title": "Research CRMs", "project_id": other["id"]}).json(),
                 client.post("/api/tasks", json={"title": "Plan the offsite"}).json()):
        assign(client, task, agent)
        run = client.get(f"/api/tasks/{task['id']}/runs").json()[0]
        assert run["status"] == "queued" and run["cwd"] is None and run["branch"] is None
        claimed = poll(client)
        assert claimed["project"] is None and claimed["task_id"] == task["id"]
        report(client, claimed["id"], status="running")
        _, assignment = worker(client, claimed["token"], "get_assignment")
        assert assignment["repo"] is None and "scratch folder" in assignment["how"]
        error, result = worker(client, claimed["token"], "request_review", summary="Top 3: A, B, C")
        assert not error and result["status"] == "pending"
        report(client, claimed["id"], status="succeeded", exit_code=0)


def test_no_machine_means_a_note_not_a_run(client):
    agent = client.post("/api/people", json={"name": "Maintainer", "kind": "agent"}).json()
    client.put(f"/api/people/{agent['id']}/agent", json={"claude_agent": "maintainer"})
    task = client.post("/api/tasks", json={"title": "Something"}).json()
    updated = assign(client, task, agent)
    assert updated["assignee_id"] == agent["id"]
    assert client.get(f"/api/tasks/{task['id']}/runs").json() == []
    assert any("No machine is connected" in u["body"] for u in updated["updates"])


def test_old_agents_and_busy_machines_skip_task_runs(client):
    _, agent, task = setup(client)
    assign(client, task, agent)
    from todo_app.deps import dispatch

    # (Straight to claim: an empty long poll takes 25s.)
    assert dispatch.claim("Omarchy", version="1.1") is None
    assert dispatch.claim("Omarchy", version="1.2", accept_tasks=False) is None
    run = poll(client)
    assert run["kind"] == "task" and run["token"] and run["mcp_path"] == "/mcp/agent"
    assert run["agent"]["claude_agent"] == "maintainer"
    assert run["project"]["repo_path"] == "~/Work/todo-app" and run["project"]["default_branch"] == "main"
    assert "get_assignment" in run["prompt"]


def test_task_runs_wait_for_their_machine(client):
    from todo_app.deps import store

    _, agent, task = setup(client)
    assign(client, task, agent)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    with store.tx() as c:
        c.execute("UPDATE launcher_runs SET requested_at = ?", (old,))
    assert client.get(f"/api/tasks/{task['id']}/runs").json()[0]["status"] == "queued"


def test_full_run_ends_in_review(client):
    _, agent, task = setup(client)
    assign(client, task, agent)
    run = poll(client)
    token = run["token"]

    report(client, run["id"], status="running", session_id="sess-1")
    t = client.get(f"/api/tasks/{task['id']}").json()
    assert t["status"] == "in_progress"

    error, assignment = worker(client, token, "get_assignment")
    assert not error and assignment["task"]["title"] == "Add version to /health"
    assert assignment["task"]["notes"] == "Return the git sha too." and assignment["repo"]["branch"] == run["branch"]

    assert not worker(client, token, "add_subtask", title="Add the endpoint field")[0]
    block = client.get(f"/api/tasks/{task['id']}").json()["blocks"][0]
    assert not worker(client, token, "check_subtask", block_id=block["id"])[0]
    assert not worker(client, token, "report_progress", note="Endpoint done, tests next")[0]
    assert worker(client, token, "set_status", status="done")[0]  # done is the user's call

    error, result = worker(client, token, "request_review", summary="Added version + sha; pytest green",
                           pr_url="https://github.com/dev7117/todo/pull/9")
    assert not error and result["status"] == "pending"
    t = client.get(f"/api/tasks/{task['id']}").json()
    assert t["status"] == "waiting" and t["waiting_on"] == "your review"
    assert t["external_url"] == "https://github.com/dev7117/todo/pull/9"
    sources = {u["source"] for u in t["updates"]}
    assert "agent: Maintainer" in sources

    proposal = client.get(f"/api/proposals/{result['proposal_id']}").json()
    assert proposal["source"] == "agent: Maintainer" and proposal["changes"][0]["action"] == "complete"

    report(client, run["id"], status="succeeded", exit_code=0, tokens=24_310, cached_tokens=180_000)
    t = client.get(f"/api/tasks/{task['id']}").json()
    assert t["status"] == "waiting" and t["waiting_on"] == "your review"  # left as the agent set it
    done = client.get(f"/api/tasks/{task['id']}/runs").json()[0]
    assert done["outcome"] == "review" and done["pr_url"].endswith("/9") and done["tokens"] == 24_310
    assert done["cached_tokens"] == 180_000
    assert "token_hash" not in done

    # The token died with the run.
    assert worker(client, token, "get_assignment") == (True, 401)

    # Approving the proposal completes the task.
    client.post(f"/api/proposals/{proposal['id']}/decide", json={"approve": "all"})
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "done"


def test_tokens_are_scoped(client):
    project, agent, task = setup(client)
    other = client.post("/api/tasks", json={"title": "Not yours", "project_id": project["id"]}).json()
    assign(client, task, agent)
    run = poll(client)
    report(client, run["id"], status="running")
    token = run["token"]
    assert "only change the task you were given" in worker(client, token, "report_progress", note="hi",
                                                         task_id=other["id"])[1]
    # A child of its task is fine.
    from todo_app.deps import store

    with store.tx() as c:
        c.execute("UPDATE tasks SET parent_id = ? WHERE id = ?", (task["id"], other["id"]))
    assert not worker(client, token, "report_progress", note="hi", task_id=other["id"])[0]
    # The run token isn't the API token: /mcp refuses it, and /mcp/agent refuses the API token.
    assert client.post("/mcp", headers={**ACCEPT, "Authorization": f"Bearer {token}"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    assert worker(client, "test-token", "get_assignment") == (True, 401)
    # The agent's endpoint only has its own tools.
    tools = client.post("/mcp/agent", headers={**ACCEPT, "Authorization": f"Bearer {token}"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_assignment", "report_progress", "set_status", "add_subtask",
                                          "check_subtask", "add_note", "update_note", "attach_file", "ask",
                                          "request_review"}


def test_question_then_reply_resumes(client):
    _, agent, task = setup(client)
    assign(client, task, agent)
    run = poll(client)
    report(client, run["id"], status="running", session_id="sess-1")
    assert not worker(client, run["token"], "ask", question="Short sha or full?")[0]
    report(client, run["id"], status="succeeded", exit_code=0)
    t = client.get(f"/api/tasks/{task['id']}").json()
    assert t["status"] == "waiting" and t["waiting_on"] == "you"

    again = client.post(f"/api/tasks/{task['id']}/dispatch", json={"message": "Short, 7 chars"}).json()
    assert again["session_id"] == "sess-1" and again["message"] == "Short, 7 chars"
    second = poll(client)
    assert second["session_id"] == "sess-1" and "replied" in second["prompt"]
    report(client, second["id"], status="running")
    _, assignment = worker(client, second["token"], "get_assignment")
    assert assignment["reply"] == "Short, 7 chars" and "Short, 7 chars" in assignment["new_from_user"]


def test_run_without_review_hands_back(client):
    _, agent, task = setup(client)
    assign(client, task, agent)
    run = poll(client)
    report(client, run["id"], status="running")
    report(client, run["id"], output="tests failed\n")
    report(client, run["id"], status="failed", exit_code=1)
    t = client.get(f"/api/tasks/{task['id']}").json()
    assert t["status"] == "waiting" and t["waiting_on"] == "you"
    assert "tests failed" in t["updates"][-1]["body"]


def test_stop_a_running_agent(client):
    _, agent, task = setup(client)
    assign(client, task, agent)
    run = poll(client)
    report(client, run["id"], status="running")
    assert client.post(f"/api/runs/{run['id']}/cancel").json()["status"] == "cancelled"
    # The machine learns on its next report.
    assert report(client, run["id"], output="still going\n")["status"] == "cancelled"
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "waiting"


def test_setup_over_mcp(client):
    client.post("/api/agent/ping", json={"name": "Omarchy", "platform": "linux", "version": "1.2"}, headers=TOKEN)
    error, templates = call(client, "get_agent_templates")
    assert not error and "todo-worker" in templates["worker_skill"]["content"]
    assert "scratch folder" in templates["general_agent"]["content"]
    assert {"python", "general"} <= set(templates["allowed_tools_presets"])

    error, project = call(client, "set_project_repo", project="todo", repo_path="~/Work/todo-app")
    assert not error and project["repo_path"] == "~/Work/todo-app"
    error, machines = call(client, "list_machines")
    assert machines[0]["runs_agents"] is True

    error, agent = call(client, "save_agent", name="Maintainer", claude_agent="maintainer", machine="Omarchy",
                        projects=["todo"], allowed_tools="Read Edit Bash(git *)")
    assert not error and agent["claude_agent"] == "maintainer" and agent["projects"] == [project["id"]]
    error, again = call(client, "save_agent", name="Maintainer", model="sonnet")
    assert again["claude_agent"] == "maintainer" and again["machine"] == "Omarchy" and again["model"] == "sonnet"

    _, check = call(client, "check_agent_setup", agent="Maintainer")
    assert not check["ok"]
    assert {c["check"]: c["ok"] for c in check["checks"]} == {
        "profile": True, "where it works": True, "machine": True, "todo-agent version": True, "smoke run": False}

    error, run = call(client, "test_agent", agent="Maintainer")
    assert not error and run["kind"] == "check" and run["status"] == "queued"
    claimed = poll(client)
    assert claimed["kind"] == "check" and "Setup check" in claimed["prompt"]
    assert claimed["project"]["repo_path"] == "~/Work/todo-app"  # its project's repo
    report(client, claimed["id"], status="running")
    _, assignment = worker(client, claimed["token"], "get_assignment")
    assert assignment["setup_check"] is True
    assert worker(client, claimed["token"], "request_review", summary="x")[0]  # no task to touch
    assert not worker(client, claimed["token"], "report_progress", note="ready: maintainer agent, todo-worker skill")[0]
    report(client, claimed["id"], status="succeeded", exit_code=0)
    _, check = call(client, "check_agent_setup", agent="Maintainer")
    assert check["ok"]
    _, got = call(client, "get_run", run_id=claimed["id"])
    assert got["outcome"] == "ready" and "ready: maintainer" in got["output_tail"]

    # The prompt walks Claude through the whole thing.
    prompt = client.post("/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
                                                     "params": {"name": "setup_agent", "arguments": {"name": "Maintainer"}}}).json()
    text = prompt["result"]["messages"][0]["content"]["text"]
    assert ".claude/agents/maintainer.md" in text and "save_agent" in text


def test_agent_name_cant_take_a_persons(client):
    client.post("/api/people", json={"name": "Dana"})
    error, message = call(client, "save_agent", name="Dana", claude_agent="dana")
    assert error and "is a person" in message


def test_general_agent_setup_check(client):
    client.post("/api/agent/ping", json={"name": "Omarchy", "platform": "linux", "version": "1.2"}, headers=TOKEN)
    _, agent = call(client, "save_agent", name="Researcher", claude_agent="researcher")
    _, run = call(client, "test_agent", agent="Researcher")
    claimed = poll(client)
    assert claimed["id"] == run["id"] and claimed["project"] is None


def test_deliverables_land_on_the_task(client):
    import base64

    agent = client.post("/api/people", json={"name": "Helpdesk", "kind": "agent"}).json()
    client.post("/api/agent/ping", json={"name": "Omarchy", "platform": "linux", "version": "1.2"}, headers=TOKEN)
    client.put(f"/api/people/{agent['id']}/agent", json={"claude_agent": "helpdesk"})
    task = client.post("/api/tasks", json={"title": "Create a PDF with the words ASDF"}).json()
    assign(client, task, agent)
    run = poll(client)
    assert "only sees what's on the task" in run["prompt"]
    report(client, run["id"], status="running")
    token = run["token"]

    _, assignment = worker(client, token, "get_assignment")
    assert "add_note" in assignment["deliver"] and assignment["files"] == []

    error, note = worker(client, token, "add_note", title="Findings", body="ASDF is four letters.")
    assert not error
    assert not worker(client, token, "update_note", block_id=note["block_id"], body="More.", append=True)[0]
    # It can't rewrite the user's blocks.
    mine = client.post(f"/api/tasks/{task['id']}/blocks", json={"title": "Mine", "body": "hands off"}).json()
    assert "only revise blocks you wrote" in worker(client, token, "update_note", block_id=mine["id"], body="x")[1]

    pdf = b"%PDF-1.4 fake"
    error, f = worker(client, token, "attach_file", name="asdf.pdf", content_base64=base64.b64encode(pdf).decode())
    assert not error and f["content_type"] == "application/pdf"
    # todo-agent uploading ./outputs/ after the run.
    up = client.post(f"/api/agent/runs/{run['id']}/files?machine=Omarchy&name=notes.md", content=b"# Notes",
                     headers={**TOKEN, "Content-Type": "text/markdown"})
    assert up.status_code == 200
    assert client.post(f"/api/agent/runs/{run['id']}/files?machine=Other&name=x.md", content=b"x",
                       headers=TOKEN).status_code == 400
    assert client.post(f"/api/agent/runs/{run['id']}/files?machine=Omarchy&name=x.md", content=b"x").status_code == 401

    full = client.get(f"/api/tasks/{task['id']}").json()
    assert {f["name"] for f in full["files"]} == {"asdf.pdf", "notes.md"}
    assert client.get(full["files"][-1]["url"]).content == pdf
    block = next(b for b in full["blocks"] if b["title"] == "Findings")
    assert block["body"] == "ASDF is four letters.\n\nMore." and block["source"] == "agent: Helpdesk"
    assert any(u["body"].startswith("Attached asdf.pdf") for u in full["updates"])
    _, again = worker(client, token, "get_assignment")
    assert {f["name"] for f in again["files"]} == {"asdf.pdf", "notes.md"}


def test_upload_files_to_a_task_from_the_app(client):
    task = client.post("/api/tasks", json={"title": "Taxes"}).json()
    f = client.post(f"/api/tasks/{task['id']}/files?name=w2.pdf", content=b"%PDF", headers={"Content-Type": "application/pdf"}).json()
    assert f["name"] == "w2.pdf"
    assert client.delete(f"/api/files/{f['id']}").status_code == 204
    assert client.get(f"/api/tasks/{task['id']}").json()["files"] == []


def test_codex_profile_and_launch_receipt_api(client):
    project, agent, task = setup(client)
    response = client.put(f"/api/people/{agent['id']}/agent", json={"executor": "codex-herdr"})
    assert response.status_code == 200 and response.json()["executor"] == "codex-herdr"
    assign(client, task, agent)
    run = poll(client, version="1.4")
    assert run["agent"]["executor"] == "codex-herdr"
    receipt = {"session": "default", "workspace": "wA", "pane_id": "wA:p9", "terminal_id": "term-fixture", "status": "blocked"}
    report(client, run["id"], status="running", launch=receipt)
    saved = client.get(f"/api/tasks/{task['id']}/runs").json()[0]
    assert saved["launch"] == receipt and "token" not in saved and "token_hash" not in saved
    error, assignment = worker(client, run["token"], "get_assignment")
    assert not error and "selected Codex harness" in assignment["deliver"]
    check = client.get(f"/api/people/{agent['id']}/agent/check").json()
    assert check["executor"] == "codex-herdr"
