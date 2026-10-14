"""Isolated assignment integration: real SQLite/Git; fake Herdr, no real agents or NAS."""
import importlib.util
import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from todo_app.agents import Agents
from todo_app.attachments import Attachments
from todo_app.dispatch import Dispatch
from todo_app.hub import Hub
from todo_app.ideas import Ideas
from todo_app.notebook import Notebook
from todo_app.people import People
from todo_app.review import Review
from todo_app.store import Invalid


@pytest.fixture
def integration(store, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("todo_runtime", Path("todo_app/agent_dist/todo-agent.py"))
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    monkeypatch.setattr(runtime, "STATE", tmp_path / "state")
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    hub = Hub(store, tmp_path)
    notebook = Notebook(store)
    people = People(store, hub, notebook)
    review = Review(store, hub, Ideas(store, notebook), people)
    agents = Agents(store, hub)
    dispatch = Dispatch(store, agents, people, review, notebook, Attachments(store, tmp_path))
    # Local bare origin and canonical checkout; Git worktree creation is real.
    real_run = subprocess.run
    def git(*args):
        return real_run(["git", *args], check=True, text=True, capture_output=True).stdout.strip()
    origin = tmp_path / "origin.git"
    git("init", "--bare", str(origin))
    repo = tmp_path / "canonical repo"
    git("clone", str(origin), str(repo))
    git("-C", str(repo), "checkout", "-b", "main")
    git("-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--allow-empty", "-m", "fixture")
    git("-C", str(repo), "push", "origin", "main")
    project = store.create_project("Todo fixture", "work", repo_path=str(repo), default_branch="main")
    agent = dispatch.save_agent("Codex", executor="codex-herdr", machine="fixture")
    cfg = {"url": "http://fixture.invalid", "name": "fixture", "herdr_cli": "fake-herdr",
           "herdr_projects": {str(project["id"]): {"session": "default", "workspace": "wA", "project_root": str(repo)}}}
    calls, occupants, states, native_commands, reports = [], {}, {}, [], []
    lock = threading.Lock()
    fail = {"start": False, "prompt": False, "replacement": False}
    workspaces = [{"workspace_id": "wA", "worktree": {"checkout_path": str(repo)}}]
    def fake_run(argv, **kwargs):
        if argv[0] != "fake-herdr":
            return real_run(argv, **kwargs)
        with lock:
            calls.append(argv)
            if argv[1:3] == ["session", "list"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"sessions": [{"name": "default", "running": True}]}), "")
            assert argv[1:3] == ["--session", "default"]
            cmd = argv[3:5]
            if cmd == ["workspace", "get"]:
                result = {"workspace": workspaces[0]}
            elif cmd == ["workspace", "list"]:
                result = {"workspaces": workspaces}
            elif cmd == ["pane", "list"]:
                result = {"panes": []}
            elif cmd in (["tab", "create"], ["workspace", "create"]):
                assert "--no-focus" in argv
                n = len(occupants) + 1
                pane = f"wA:p{n}"
                occupants[pane] = None
                if not workspaces:
                    workspaces.append({"workspace_id": "wA", "worktree": {"checkout_path": argv[argv.index('--cwd') + 1]}})
                result = {"root_pane": {"pane_id": pane}, "tab": {"tab_id": f"wA:t{n}"}, "workspace": {"workspace_id": "wA"}}
            elif cmd == ["agent", "list"]:
                result = {"agents": [{"name": n, "pane_id": p, "agent": "codex", "terminal_id": "term-" + p} for p, n in occupants.items() if n]}
            elif cmd == ["pane", "run"]:
                pane = argv[5]
                bootstrap = shlex.split(argv[6])
                assert bootstrap[-2] == "codex-herdr-bootstrap"
                data = runtime.consume_codex_handoff(Path(bootstrap[-1]))
                native_commands.append(data["argv"])
                name = "bootstrap-" + pane.replace(":", "-")
                occupants[pane] = name
                states[name] = ["idle"]
                if fail["start"]:
                    raise subprocess.TimeoutExpired(argv, 40)
                result = {}
            elif cmd == ["agent", "rename"]:
                pane, name = argv[5:7]
                old = occupants[pane]
                occupants[pane] = name
                states[name] = states.pop(old)
                result = {}
            elif cmd == ["agent", "get"]:
                name = argv[5]
                pane = next(p for p, n in occupants.items() if n == name)
                state = states[name][0]
                if len(states[name]) > 1:
                    states[name].pop(0)
                result = {"agent": {"name": name, "pane_id": "wZ:p99" if fail["replacement"] else pane,
                                    "agent": "codex", "agent_status": state, "terminal_id": "term-" + pane, "interactive_ready": True}}
            elif cmd == ["agent", "prompt"]:
                if fail["prompt"]:
                    raise subprocess.TimeoutExpired(argv, 40)
                states[argv[5]] = ["working", "done"]
                result = {"submitted": True}
            elif cmd == ["agent", "send-keys"]:
                assert argv[-1] == "ctrl+c"
                states[argv[5]] = ["idle"]
                result = {}
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result}), "")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    def api(cfg, path, body, **kwargs):
        reports.append(body)
        fields = {k: v for k, v in body.items() if k != "name"}
        run = dispatch.report(int(path.rsplit("/", 1)[1]), cfg["name"], **fields)
        return {"status": run["status"], "outcome": run["outcome"]}
    monkeypatch.setattr(runtime, "api", api)
    monkeypatch.setattr(runtime, "attach_outputs_to_task", lambda *a: "")
    def assignment(title="Implement fixture"):
        task = store.create_task({"title": title, "project_id": project["id"], "assignee_id": agent["id"]})
        claimed = dispatch.claim("fixture", version="1.4")
        return task, claimed
    return locals()


def test_assignment_to_visible_worktree_and_parallel_tasks(integration):
    x = integration
    task1, run1 = x["assignment"]("First")
    task2, run2 = x["assignment"]("Second")
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda r: x["runtime"].run_agent(x["cfg"], r), [run1, run2]))
    for task, run in ((task1, run1), (task2, run2)):
        saved = x["agents"].get_run(run["id"])
        assert saved["status"] == "succeeded"
        assert saved["launch"]["workspace"] == "wA"
        assert saved["launch"]["status"] == "finished"
        path = x["runtime"].STATE / "worktrees" / f"task-{task['id']}"
        assert x["runtime"].common_dir(str(path)) == x["runtime"].common_dir(str(x["repo"]))
        assert x["git"]("-C", str(path), "branch", "--show-current") == run["branch"]
        assert x["store"].get_task(task["id"])["status"] == "waiting"  # never done
    starts = [a for a in x["calls"] if a[3:5] == ["pane", "run"]]
    assert len(starts) == 2 and starts[0][5] != starts[1][5]
    for args in x["native_commands"]:
        assert "--sandbox" in args and "workspace-write" in args
        assert "on-request" in args and "--dangerously-bypass-approvals-and-sandbox" not in args
        assert "--agent" not in args and "--mcp-config" not in args
    assert all(a[3:5] != ["workspace", "create"] for a in x["calls"])


@pytest.mark.parametrize("phase", ["start", "prompt"])
def test_uncertain_launch_is_not_retried(integration, phase):
    x = integration
    task, run = x["assignment"]()
    x["fail"][phase] = True
    with pytest.raises(RuntimeError):
        x["runtime"].run_agent(x["cfg"], run)
    saved = x["agents"].get_run(run["id"])
    assert saved["launch"]["status"] == "needs_inspection"
    with pytest.raises(Invalid, match="Inspect Herdr"):
        x["dispatch"].enqueue(task["id"])
    count = len(x["calls"])
    with pytest.raises(RuntimeError, match="existing Herdr receipt"):
        x["runtime"].run_agent(x["cfg"], run)
    assert len(x["calls"]) == count


def test_old_machine_cannot_claim_codex_and_review_is_always_pending(integration):
    x = integration
    task, run = x["assignment"]()
    task2 = x["store"].create_task({"title": "Another", "project_id": x["project"]["id"], "assignee_id": x["agent"]["id"]})
    assert x["dispatch"].claim("fixture", version="1.2") is None
    assert x["dispatch"].claim("fixture", version="1.4")["task_id"] == task2["id"]
    x["store"].update_settings(review_claude_changes=False)
    result = x["dispatch"].request_review(x["agents"].get_run(run["id"]), "Verified fixture")
    assert result["status"] == "pending"
    assert x["store"].get_task(task["id"])["status"] == "waiting"


def test_wrong_repository_and_workspace_are_refused(integration):
    x = integration
    _, run = x["assignment"]()
    route = x["cfg"]["herdr_projects"][str(x["project"]["id"])]
    route["project_root"] = str(x["tmp_path"])
    with pytest.raises(RuntimeError):
        x["runtime"].run_agent(x["cfg"], run)
    assert not x["calls"]
    route["project_root"] = str(x["repo"])
    x["workspaces"][0]["worktree"]["checkout_path"] = str(x["tmp_path"])
    with pytest.raises(RuntimeError, match="workspace identity"):
        x["runtime"].run_agent(x["cfg"], run)
    assert not any(a[3:5] == ["tab", "create"] for a in x["calls"])


def test_cancellation_only_interrupts_owned_named_agent(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    original = x["runtime"].monitor_herdr
    def cancel(cfg, record, receipt):
        x["dispatch"].stop(run["id"])
        original(cfg, record, receipt)
    monkeypatch.setattr(x["runtime"], "monitor_herdr", cancel)
    x["runtime"].run_agent(x["cfg"], run)
    saved = x["agents"].get_run(run["id"])
    assert saved["status"] == "cancelled" and saved["launch"]["status"] == "cancelled"
    interrupts = [a for a in x["calls"] if a[3:5] == ["agent", "send-keys"]]
    assert len(interrupts) == 1 and interrupts[0][5] == saved["launch"]["agent_name"]
    assert not any(a[3:5] == ["pane", "close"] for a in x["calls"])


def test_cancellation_refuses_replaced_agent(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    original = x["runtime"].monitor_herdr
    def cancel(cfg, record, receipt):
        x["dispatch"].stop(run["id"])
        x["fail"]["replacement"] = True
        original(cfg, record, receipt)
    monkeypatch.setattr(x["runtime"], "monitor_herdr", cancel)
    with pytest.raises(RuntimeError, match="identity changed"):
        x["runtime"].run_agent(x["cfg"], run)
    assert not any(a[3:5] == ["agent", "send-keys"] for a in x["calls"])
    assert x["agents"].get_run(run["id"])["launch"]["status"] == "needs_inspection"


def test_duplicate_enqueue_and_claim_serialize(integration):
    x = integration
    task, run = x["assignment"]()
    x["agents"].report(run["id"], "fixture", status="failed")
    def queue(_):
        try:
            return x["dispatch"].enqueue(task["id"])["id"]
        except Invalid:
            return None
    with ThreadPoolExecutor(4) as pool:
        assert len([r for r in pool.map(queue, range(4)) if r]) == 1
        claims = list(pool.map(lambda _: x["dispatch"].claim("fixture", version="1.4"), range(4)))
        assert len([r for r in claims if r]) == 1


def test_new_project_workspace_created_once_and_reused(integration):
    x = integration
    x["workspaces"].clear()
    route = x["cfg"]["herdr_projects"][str(x["project"]["id"])]
    route.pop("workspace")
    runs = [x["assignment"](title)[1] for title in ("First", "Second")]
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda r: x["runtime"].run_agent(x["cfg"], r), runs))
    assert len([a for a in x["calls"] if a[3:5] == ["workspace", "create"]]) == 1
    assert len([a for a in x["calls"] if a[3:5] == ["tab", "create"]]) == 1


def test_restart_observes_without_relaunch_or_resubmission(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    monkeypatch.setattr(x["runtime"], "monitor_herdr", lambda *a: None)
    x["runtime"].run_agent(x["cfg"], run)
    observed = threading.Event()
    monkeypatch.setattr(x["runtime"], "monitor_herdr", lambda *a: observed.set())
    calls = len(x["calls"])
    x["runtime"].recover_herdr(x["cfg"])
    assert observed.wait(2)
    assert len(x["calls"]) == calls


def test_restart_uncertain_submission_requires_inspection(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    monkeypatch.setattr(x["runtime"], "monitor_herdr", lambda *a: None)
    x["runtime"].run_agent(x["cfg"], run)
    receipt = next((x["runtime"].STATE / "herdr").glob("*/run-*.json"))
    record = json.loads(receipt.read_text())
    record["status"] = "submitting"
    x["runtime"].save_receipt(receipt, record)
    calls = len(x["calls"])
    x["runtime"].recover_herdr(x["cfg"])
    assert json.loads(receipt.read_text())["status"] == "needs_inspection"
    assert len(x["calls"]) == calls


def test_blocked_and_unknown_are_not_completion(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    original = x["runtime"].monitor_herdr
    seen = []
    def monitor(cfg, record, receipt):
        x["states"][record["agent_name"]] = ["blocked", "unknown", "working", "done"]
        original(cfg, record, receipt)
    real_api = x["runtime"].api
    def api(cfg, path, body, **kwargs):
        if body.get("launch", {}).get("status") in ("blocked", "unknown"):
            seen.append(body["launch"]["status"])
            assert "status" not in body
        return real_api(cfg, path, body, **kwargs)
    monkeypatch.setattr(x["runtime"], "api", api)
    monkeypatch.setattr(x["runtime"], "monitor_herdr", monitor)
    x["runtime"].run_agent(x["cfg"], run)
    assert seen == ["blocked", "unknown"]
    assert not any(a[3:5] == ["agent", "send-keys"] for a in x["calls"])


def test_reassignment_before_claim_does_not_start_old_agent(integration):
    x = integration
    task = x["store"].create_task({"title": "Changed", "project_id": x["project"]["id"], "assignee_id": x["agent"]["id"]})
    x["store"].update_task(task["id"], {"assignee_id": None})
    assert x["dispatch"].claim("fixture", version="1.4") is None
    assert not x["calls"]


def test_declined_approval_releases_capacity(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    monkeypatch.setattr(x["runtime"], "approvals", lambda: {})
    monkeypatch.setattr(x["runtime"], "ask", lambda *a: "deny")
    x["runtime"].handle(x["cfg"], run)
    assert x["runtime"]._task_runs == 0
    assert x["agents"].get_run(run["id"])["status"] == "declined"
    assert not x["calls"]


def test_operator_reconciliation_refuses_live_agent_then_releases_receipt(integration):
    x = integration
    task, run = x["assignment"]()
    x["fail"]["prompt"] = True
    with pytest.raises(RuntimeError):
        x["runtime"].run_agent(x["cfg"], run)
    with pytest.raises(RuntimeError, match="still hosts"):
        x["runtime"].reconcile_herdr(x["cfg"], run["id"])
    x["occupants"].clear()  # operator closed the fake agent, not the runtime
    x["runtime"].reconcile_herdr(x["cfg"], run["id"])
    assert x["agents"].get_run(run["id"])["launch"]["status"] == "cancelled"
    assert x["dispatch"].enqueue(task["id"])["status"] == "queued"


def test_late_running_report_cannot_revive_cancelled_run(integration):
    x = integration
    _, run = x["assignment"]()
    x["dispatch"].stop(run["id"])
    result = x["dispatch"].report(run["id"], "fixture", status="running", launch={"status": "blocked"})
    assert result["status"] == "cancelled"
    assert x["dispatch"].run_for_token(run["token"]) is None


def test_token_never_enters_commands_receipts_or_public_reports(integration):
    x = integration
    _, run = x["assignment"]()
    x["runtime"].run_agent(x["cfg"], run)
    secret = run["token"]
    assert not any(secret in json.dumps(value) for value in (x["calls"], x["native_commands"], x["reports"]))
    folder = next((x["runtime"].STATE / "herdr").iterdir())
    receipts = list(x["runtime"].receipt_files(folder))
    assert len(receipts) == 1 and secret not in receipts[0].read_text()
    assert not list(folder.glob("*.handoff.json"))
    args = x["native_commands"][0]
    assert "--no-daemon" in args
    assert any('bearer_token_env_var="TODO_WORKER_RUN_TOKEN"' in a for a in args)
    assert any('TODO_WORKER_RUN_TOKEN="exclude"' in a for a in args)


def test_handoff_is_private_consumed_once_and_env_only_on_exec(integration, tmp_path):
    import hashlib
    import os
    import sys
    x = integration
    _, run = x["assignment"]()
    folder = x["runtime"].STATE / "herdr" / "fixture"
    folder.mkdir(parents=True, mode=0o700)
    receipt = folder / f"run-{run['id']}.json"
    ticket = x["runtime"].write_codex_handoff(receipt, x["cfg"], run, str(x["repo"]))
    assert ticket.stat().st_mode & 0o777 == 0o600
    assert folder.stat().st_mode & 0o777 == 0o700
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    executable = fakebin / "codex"
    executable.write_text("#!" + sys.executable + "\n" + '''import hashlib, json, os, sys
secret = os.environ.get("TODO_WORKER_RUN_TOKEN", "")
print(json.dumps({"environment_matches": hashlib.sha256(secret.encode()).hexdigest() == os.environ["EXPECTED_DIGEST"],
                  "argv_is_clean": all(secret not in arg for arg in sys.argv),
                  "ticket_deleted": not os.path.exists(os.environ["TICKET_PATH"])}))
''')
    executable.chmod(0o700)
    result = x["real_run"]([sys.executable, x["runtime"].__file__, "codex-herdr-bootstrap", str(ticket)],
                          capture_output=True, text=True, env={"PATH": str(fakebin), "HOME": str(tmp_path),
                          "EXPECTED_DIGEST": hashlib.sha256(run["token"].encode()).hexdigest(), "TICKET_PATH": str(ticket)})
    assert result.returncode == 0
    assert run["token"] not in result.stdout + result.stderr
    assert json.loads(result.stdout) == {"environment_matches": True, "argv_is_clean": True, "ticket_deleted": True}
    with pytest.raises(FileNotFoundError):
        x["runtime"].consume_codex_handoff(ticket)


def test_native_codex_accepts_credential_and_filter_configuration(integration, tmp_path):
    import os
    import shutil
    x = integration
    cli = shutil.which("codex")
    if not cli:
        pytest.skip("Installed Codex CLI not available; native configuration compatibility needs rollout validation")
    _, run = x["assignment"]()
    args = x["runtime"].codex_native_args(x["cfg"], run, str(x["repo"]))
    args[0] = cli
    args += ["mcp", "list", "--json"]  # config parse/inventory only, no agent or HTTP request
    home = tmp_path / "isolated-codex-home"
    home.mkdir(mode=0o700)
    result = x["real_run"](args, text=True, capture_output=True, timeout=15,
                          env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CODEX_HOME": str(home),
                               x["runtime"].TOKEN_ENV: run["token"]})
    assert result.returncode == 0
    assert run["token"] not in result.stdout + result.stderr
    servers = json.loads(result.stdout)
    worker = next(s for s in servers if s["name"] == "todo_worker")
    assert worker["transport"]["bearer_token_env_var"] == x["runtime"].TOKEN_ENV
    assert not worker["transport"].get("http_headers")


def test_secret_errors_are_redacted_and_uncertain_handoff_is_cleaned(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    def fail(*args):
        raise RuntimeError("private handoff diagnostic: " + run["token"])
    monkeypatch.setattr(x["runtime"], "start_codex_handoff", fail)
    with pytest.raises(RuntimeError) as got:
        x["runtime"].run_agent(x["cfg"], run)
    assert run["token"] not in str(got.value)
    saved = x["agents"].get_run(run["id"])
    assert run["token"] not in json.dumps(saved)
    folder = next((x["runtime"].STATE / "herdr").iterdir())
    assert not list(folder.glob("*.handoff.json"))
    assert all(run["token"] not in p.read_text() for p in x["runtime"].receipt_files(folder))


def test_handoff_refuses_exposed_file_and_symlink(integration, tmp_path):
    x = integration
    _, run = x["assignment"]()
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    receipt = folder / "run-1.json"
    ticket = x["runtime"].write_codex_handoff(receipt, x["cfg"], run, str(x["repo"]))
    ticket.chmod(0o644)
    with pytest.raises(RuntimeError, match="Unsafe Codex handoff file"):
        x["runtime"].consume_codex_handoff(ticket)
    ticket.chmod(0o600)
    link = folder / "linked.handoff.json"
    link.symlink_to(ticket)
    with pytest.raises(OSError):
        x["runtime"].consume_codex_handoff(link)
    assert ticket.exists()


def test_restart_cleans_orphan_handoff_and_scrubs_legacy_receipt(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    monkeypatch.setattr(x["runtime"], "monitor_herdr", lambda *a: None)
    x["runtime"].run_agent(x["cfg"], run)
    folder = next((x["runtime"].STATE / "herdr").iterdir())
    receipt = next(x["runtime"].receipt_files(folder))
    record = json.loads(receipt.read_text())
    record["run"]["token"] = run["token"]
    record["status"] = "starting"
    receipt.write_text(json.dumps(record))  # simulate an older draft receipt
    ticket = x["runtime"].write_codex_handoff(receipt, x["cfg"], run, str(x["repo"]))
    before = len(x["calls"])
    x["runtime"].recover_herdr(x["cfg"])
    assert not ticket.exists()
    assert run["token"] not in receipt.read_text()
    assert json.loads(receipt.read_text())["status"] == "needs_inspection"
    assert len(x["calls"]) == before


def test_occupied_new_pane_is_not_controlled(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    original = x["runtime"].start_codex_handoff
    def occupy(cfg, record, ticket):
        x["occupants"][record["pane_id"]] = "someone-else"
        x["states"]["someone-else"] = ["idle"]
        original(cfg, record, ticket)
    monkeypatch.setattr(x["runtime"], "start_codex_handoff", occupy)
    with pytest.raises(RuntimeError, match="already occupied"):
        x["runtime"].run_agent(x["cfg"], run)
    assert not any(a[3:5] in (["pane", "run"], ["agent", "rename"], ["agent", "prompt"]) for a in x["calls"])
    assert list(x["occupants"].values()) == ["someone-else"]
    assert not list((x["runtime"].STATE / "herdr").glob("*/*.handoff.json"))


def test_herdr_failure_does_not_publish_terminal_diagnostics(integration, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    monkeypatch.setattr(x["runtime"].subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", run["token"]))
    with pytest.raises(RuntimeError) as got:
        x["runtime"].herdr_call(x["cfg"], "default", "agent", "list")
    assert run["token"] not in str(got.value)
    assert "exit 1" in str(got.value)


def test_bootstrap_failure_prints_no_secret_and_removes_ticket(integration, monkeypatch, tmp_path, capsys):
    import sys
    x = integration
    _, run = x["assignment"]()
    folder = tmp_path / "private-bootstrap"
    folder.mkdir(mode=0o700)
    ticket = x["runtime"].write_codex_handoff(folder / "run-1.json", x["cfg"], run, str(x["repo"]))
    def fail(*a):
        raise OSError(run["token"])
    monkeypatch.setattr(x["runtime"].os, "execvpe", fail)
    monkeypatch.setattr(sys, "argv", [x["runtime"].__file__, "codex-herdr-bootstrap", str(ticket)])
    with pytest.raises(SystemExit) as result:
        x["runtime"].main()
    assert result.value.code == 1
    captured = capsys.readouterr()
    assert run["token"] not in captured.out + captured.err
    assert "OSError" in captured.err and not ticket.exists()


def test_existing_task_config_is_private_before_secret_write(integration, tmp_path, monkeypatch):
    x = integration
    _, run = x["assignment"]()
    path = tmp_path / "task-mcp.json"
    path.write_text("stale config")
    path.chmod(0o644)
    original = x["runtime"].json.dump
    checked = []
    def check_before_write(data, stream):
        import os
        checked.append(os.fstat(stream.fileno()).st_mode & 0o777 == 0o600)
        return original(data, stream)
    monkeypatch.setattr(x["runtime"].json, "dump", check_before_write)
    x["runtime"].write_private_json(path, {"token": run["token"]})
    assert checked == [True]
    assert path.stat().st_mode & 0o777 == 0o600


def test_private_task_config_refuses_symlink(integration, tmp_path):
    x = integration
    target = tmp_path / "user-file"
    target.write_text("preserve")
    path = tmp_path / "task-config.json"
    path.symlink_to(target)
    with pytest.raises(OSError):
        x["runtime"].write_private_json(path, {"token": "fixture-only"})
    assert target.read_text() == "preserve"

@pytest.fixture
def native(integration, monkeypatch):
    x = integration
    rt = x['runtime']
    x['dispatch'].save_agent('Codex', executor='codex-app', machine='fixture')
    calls = []
    control = {'connected': True, 'consume': True, 'uncertain': False, 'cancel': False}
    class FakeApp:
        blocked = set()
        def __init__(self, cfg): self.thread = {}; self.turn = 'completed'
        def close(self): pass
        def rpc(self, method, params=None, **kwargs):
            params = params or {}
            calls.append((method, params))
            if method == 'remoteControl/status/read':
                return {'status': 'connected' if control['connected'] else 'disabled', 'installationId': 'fixture-install', 'serverName': 'Fixture computer'}
            if method == 'project/list': return {'data': [{'id': 'project-fixture', 'roots': [{'path': str(x['repo'])}]}], 'nextCursor': None}
            if method == 'thread/start':
                self.thread = {'id': 'thread-fixture', 'cwd': params['cwd'], 'projectId': params['projectId'], 'status': {'type': 'idle'}}
                if control['consume']:
                    ticket = Path(params['config']['mcp_servers']['todo_worker']['args'][-1])
                    data = rt.consume_private_handoff(ticket)
                    assert data['kind'] == 'worker'
                return {'thread': self.thread}
            if method == 'thread/read': return {'thread': self.thread}
            if method == 'thread/name/set': return {}
            if method == 'turn/start':
                if control['uncertain']: raise RuntimeError('fixture uncertain turn')
                if control['cancel']:
                    x['dispatch'].stop(x['run']['id'])
                    self.turn = 'inProgress'
                return {'turn': {'id': 'turn-fixture'}}
            if method == 'thread/turns/list': return {'data': [{'id': 'turn-fixture', 'status': self.turn}], 'nextCursor': None}
            if method == 'turn/interrupt':
                assert params == {'threadId': 'thread-fixture', 'turnId': 'turn-fixture'}
                self.turn = 'interrupted'
                return {}
            raise AssertionError(method)
    monkeypatch.setattr(rt, 'CodexAppServer', FakeApp)
    x.update(native_calls=calls, control=control)
    return x


def test_native_assignment_scoped_secret_and_review_boundary(native):
    x = native
    task, run = x['assignment']()
    x['runtime'].run_agent(x['cfg'], run)
    saved = x['agents'].get_run(run['id'])
    assert saved['status'] == 'succeeded'
    assert saved['launch']['thread_id'] == 'thread-fixture'
    assert x['store'].get_task(task['id'])['status'] == 'waiting'
    assert run['token'] not in json.dumps(x['native_calls'])
    start = next(p for m, p in x['native_calls'] if m == 'thread/start')
    assert start['approvalPolicy'] == 'on-request' and start['approvalsReviewer'] == 'user'
    assert start['sandbox'] == 'workspace-write' and start['ephemeral'] is False
    assert not list(x['runtime'].STATE.rglob('*.handoff*'))
    assert not any(m == 'project/create' for m, _ in x['native_calls'])
    with pytest.raises(RuntimeError): x['runtime'].run_agent(x['cfg'], run)
    assert sum(m == 'turn/start' for m, _ in x['native_calls']) == 1


@pytest.mark.parametrize('failure', ['connected', 'consume', 'uncertain'])
def test_native_fails_closed_without_launch_retry(native, failure):
    x = native
    task, run = x['assignment']()
    x['control'][failure] = failure == 'uncertain'
    with pytest.raises(RuntimeError): x['runtime'].run_agent(x['cfg'], run)
    assert sum(m == 'turn/start' for m, _ in x['native_calls']) == (1 if failure == 'uncertain' else 0)
    assert run['token'] not in json.dumps(x['native_calls'])
    assert not list(x['runtime'].STATE.rglob('*.handoff*'))
    if failure == 'uncertain':
        with pytest.raises(RuntimeError): x['runtime'].run_agent(x['cfg'], run)
        assert sum(m == 'turn/start' for m, _ in x['native_calls']) == 1


def test_native_cancel_only_recorded_turn(native):
    x = native
    task, run = x['assignment']()
    x['task'] = task
    x['run'] = run
    x['control']['cancel'] = True
    x['runtime'].run_agent(x['cfg'], run)
    assert x['agents'].get_run(run['id'])['status'] == 'cancelled'
    assert sum(m == 'turn/interrupt' for m, _ in x['native_calls']) == 1


def test_machine_wide_herdr_defaults_without_project_map(integration):
    x = integration
    x['cfg'].pop('herdr_projects')
    task, run = x['assignment']()
    x['runtime'].run_agent(x['cfg'], run)
    assert x['agents'].get_run(run['id'])['launch']['workspace'] == 'wA'


def test_native_offline_worker_does_not_restore_token(integration, monkeypatch):
    import io
    rt = integration['runtime']
    stdin = io.StringIO('\n'.join(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': m}) for i, m in enumerate(['initialize', 'tools/list', 'tools/call'])))
    stdout = io.StringIO()
    monkeypatch.setattr(rt.sys, 'stdin', stdin)
    monkeypatch.setattr(rt.sys, 'stdout', stdout)
    rt.codex_worker_proxy(integration['tmp_path'] / 'missing-ticket')
    results = [json.loads(line)['result'] for line in stdout.getvalue().splitlines()]
    assert results[1]['tools'] == [] and results[2]['isError']


def test_general_codex_assignment_needs_no_project_setup(integration):
    x = integration
    x['cfg'].pop('herdr_projects')
    x['workspaces'].clear()
    task = x['store'].create_task({'title': 'General research', 'assignee_id': x['agent']['id']})
    run = x['dispatch'].claim('fixture', version='1.4')
    assert run['project'] is None
    x['runtime'].run_agent(x['cfg'], run)
    assert x['agents'].get_run(run['id'])['status'] == 'succeeded'
    assert (x['runtime'].STATE / 'workspaces' / f"task-{task['id']}").is_dir()


def test_native_worker_bridge_uses_private_memory_only(integration, monkeypatch):
    import io
    rt = integration['runtime']
    task, run = integration['assignment']()
    folder = rt.STATE / 'private'
    folder.mkdir(parents=True, mode=0o700)
    ticket = rt.write_worker_handoff(folder / 'run.json', integration['cfg'], run)
    stdin = io.StringIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n')
    stdout = io.StringIO()
    observed = []
    def transport(request, **kwargs):
        observed.append(request.get_header('Authorization'))
        return io.StringIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'tools': [], 'diagnostic': run['token']}}))
    monkeypatch.setattr(rt.sys, 'stdin', stdin)
    monkeypatch.setattr(rt.sys, 'stdout', stdout)
    monkeypatch.setattr(rt.urllib.request, 'urlopen', transport)
    rt.codex_worker_proxy(ticket)
    assert observed == ['Bearer ' + run['token']]
    assert not ticket.exists()
    assert run['token'] not in stdout.getvalue()
    assert json.loads(stdout.getvalue())['result']['diagnostic'] == '[redacted]'


def test_native_rpc_leaves_permission_requests_unanswered(integration, monkeypatch):
    import io
    import queue
    rt = integration['runtime']
    client = rt.CodexAppServer.__new__(rt.CodexAppServer)
    client.sequence, client.blocked = 0, set()
    client.inbox = queue.Queue()
    client.inbox.put({'id': 900, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'owned'}})
    client.inbox.put({'id': 1, 'result': {'ok': True}})
    sent = []
    client.send = sent.append
    assert client.rpc('thread/read', {'threadId': 'owned'}) == {'ok': True}
    assert client.blocked == {'owned'}
    assert sent == [{'id': 1, 'method': 'thread/read', 'params': {'threadId': 'owned'}}]


def test_native_identity_mismatch_prevents_control(native):
    rt = native['runtime']
    class WrongThread:
        def rpc(self, *args): return {'thread': {'id': 'other', 'cwd': '/tmp', 'projectId': 'wrong'}}
    with pytest.raises(RuntimeError, match='identity changed'):
        rt.app_owned_thread(WrongThread(), {'thread_id': 'owned', 'cwd': '/tmp', 'codex_project': 'expected'})


def test_linux_approval_file_never_contains_run_token(integration, monkeypatch):
    x = integration
    rt = x['runtime']
    _, run = x['assignment']()
    rt.describe_agent(x['cfg'], run)
    monkeypatch.setattr(rt, 'MAC', False)
    seen = []
    def terminal(argv, **kwargs):
        request = Path(argv[-1])
        data = request.read_text()
        seen.append(data)
        assert run['token'] not in data and 'token_hash' not in data
        assert request.stat().st_mode & 0o777 == 0o600
        request.with_suffix('.answer').write_text('deny')
    monkeypatch.setattr(rt.subprocess, 'Popen', terminal)
    assert rt.ask(x['cfg'], run) == 'deny'
    assert len(seen) == 1
    assert not list(rt.STATE.glob('ask-*'))


def test_native_raw_proxy_websocket_handshake_frames(integration):
    import io
    import base64
    import hashlib
    import struct
    rt = integration['runtime']
    incoming, outgoing = io.BytesIO(), io.BytesIO()
    transport = rt.LocalWebSocket(incoming, outgoing)
    assert b'Upgrade: websocket' in outgoing.getvalue()
    accept = base64.b64encode(hashlib.sha1((transport.key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
    response = b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n'
    payload = json.dumps({'id': 1, 'result': {'ok': True}}).encode()
    incoming.write(response + b'\x89\x02hi' + bytes([1, 10]) + payload[:10] + bytes([128, len(payload)-10]) + payload[10:])
    incoming.seek(0)
    transport.handshake()
    assert transport.message() == {'id': 1, 'result': {'ok': True}}
    outgoing.seek(0); outgoing.truncate()
    transport.frame(b'x' * 200)
    frame = outgoing.getvalue()
    assert frame[0] == 129 and frame[1] == 254 and struct.unpack('!H', frame[2:4])[0] == 200
    mask = frame[4:8]
    assert bytes(v ^ mask[i % 4] for i, v in enumerate(frame[8:])) == b'x' * 200
    incoming = io.BytesIO(b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: wrong\r\n\r\n')
    bad = rt.LocalWebSocket(incoming, io.BytesIO())
    with pytest.raises(RuntimeError, match='handshake rejected'): bad.handshake()


def test_native_connection_error_reports_real_state(integration):
    rt = integration['runtime']
    class RelayError:
        def rpc(self, *args):
            return {'status': 'errored', 'installationId': 'fixture-install'}
    with pytest.raises(RuntimeError, match='reports errored; pairing alone'):
        rt.app_connected(RelayError())


@pytest.mark.parametrize('with_task', [False, True])
def test_daemon_poll_loop_starts_and_reserves_capacity(integration, monkeypatch, with_task):
    x = integration
    rt = x['runtime']
    cfg = {**x['cfg'], 'max_task_runs': 1}
    monkeypatch.setattr(rt, 'load_config', lambda: cfg)
    monkeypatch.setattr(rt, 'log', lambda _: None)
    run = x['assignment']()[1] if with_task else None
    bodies, started = [], []
    def poll(cfg, path, body, **kwargs):
        assert path == '/api/agent/poll'
        bodies.append(dict(body))
        if len(bodies) == 2: raise KeyboardInterrupt  # bounded actual daemon loop, no service/network
        return {'run': run}
    class Thread:
        def __init__(self, **kwargs): self.arguments = kwargs
        def start(self): started.append(self.arguments)
    monkeypatch.setattr(rt, 'api', poll)
    monkeypatch.setattr(rt.threading, 'Thread', Thread)
    with pytest.raises(KeyboardInterrupt): rt.loop()
    assert bodies[0]['version'] == '1.4' and bodies[0]['accept_tasks'] is True
    assert bodies[1]['accept_tasks'] is (not with_task)
    assert rt._task_runs == int(with_task)
    if with_task:
        assert started[0]['target'] == rt.handle
        assert started[0]['args'] == (cfg, run, True)
    else:
        assert started == []
