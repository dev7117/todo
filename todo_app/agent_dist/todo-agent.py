#!/usr/bin/env python3
"""todo-agent: runs customer-hub tools from the todo app on this machine (macOS or Linux).

The todo app lives on the NAS; this agent connects to it, waits for runs you start from a
customer hub, and executes them here:

  interactive  opens a terminal window with a real TTY (CLI logins, anything that asks
               questions); the session is recorded and sent back to the hub
  headless     runs in the background (e.g. `claude -p "..."`); output streams back to the
               hub and you get a notification when it finishes

Before running a command it hasn't seen, it asks you HERE: a dialog on macOS, a terminal
prompt on Linux. It fingerprints the command itself, so the server can't vouch for a command
you never approved. "Always allow" remembers that exact command; editing it asks again.

  todo-agent run          the agent loop (launchd / systemd run this)
  todo-agent install      set it up to start at login (needs ~/.config/todo/agent.json)
  todo-agent uninstall    stop it and remove the login item
  todo-agent status       config, service state, connection check
  todo-agent herdr-reconcile <run id>
                          release an uncertain receipt only after its agent is closed
  todo-agent approvals    commands you've always-allowed;  `todo-agent forget` clears them
  todo-agent upload <meeting id> <file>… [--step <id>] [--note <text>]
                          attach files to a cadence meeting (reports, exports); scripts run
                          for a meeting get its id as $TODO_OCCURRENCE_ID

A tool run for a cadence meeting's prep step uploads the step's output files (the paths/globs
set on the step) that the run wrote, to that meeting.

Agents: when you assign a todo task to an agent, this machine works it. It makes a git
worktree of the project's repo for the task (branch agent/<id>-<slug>, from origin/<default
branch>), or for a task without a repo a scratch folder (workspaces/task-<id>; the agent file
then comes from ~/.claude/agents), then runs `claude -p --agent <name>` there, headless, with an MCP config that only
reaches the todo app's agent endpoint with a token for this one run. Claude reports progress on
the task and finishes with a PR. The first time an agent runs (or after its tools, model or
repo change) you're asked to approve it, like any new command. Worktrees live in the state
dir (worktrees/task-<id>) until you remove them (`git worktree prune` after deleting).

Config ~/.config/todo/agent.json: {"url": "...", "token": "...", "name": "MacBook",
"terminal": "Terminal" | "iTerm" | "Ghostty", "claude": "/path/to/claude", "max_task_runs": 2,
"repo_paths": {"<project name>": "~/code/elsewhere"}} (terminal is macOS only; Linux uses
xdg-terminal-exec; claude defaults to the one on your login shell's PATH; repo_paths
overrides a project's repo path on this machine). Standard library only, Python 3.9+.

Shared Codex harnesses (todo-agent 1.4): codex-herdr uses the explicitly named default
Herdr session, or agent.json {"herdr": {"session": "my-session"}}. No per-project routing
is required: canonical Git common-dir identity reuses the project's workspace. Optional
herdr_projects overrides can select a verified workspace/session/project_root. General
work uses task scratch folders grouped under the machine's Todo workspace. herdr_cli
defaults to herdr on PATH. No-focus creation and user approval prompts are preserved.
codex-app connects with `codex app-server proxy` to an ALREADY connected local daemon.
Optional {"codex_app": {"socket": "/path/to/control.sock"}} selects that daemon explicitly.
It never starts/pairs/enables the daemon. Persistent project/thread/turn identities are
recorded; permission requests are left to the native app/Remote. A private stdio MCP
bridge consumes the one-use token ticket, keeping secrets out of shared daemon config,
argv and environment. Old chats reopen with an offline worker, never restored tokens.
Receipts under state/herdr/<server hash>/ cover both routes, survive restart and forbid
uncertain retries. `herdr-reconcile RUN_ID` also verifies native recorded turn identity.
Run credentials use a private 0600 one-use handoff in the 0700 launch namespace. A Python
bootstrap consumes/unlinks it, then execs Codex --no-daemon with native bearer_token_env_var.
No secret is sent as terminal input, argv, receipt metadata, or shell export. Model shells
exclude that variable. Interrupted handoffs are removed on recovery; launches never retry.

"""
from __future__ import annotations

import glob
import hashlib
import base64
import json
import mimetypes
import os
import platform as _platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.4"
MAC = sys.platform == "darwin"
HOME = Path.home()
# TODO_AGENT_HOME runs a second agent (e.g. against a local dev server) with its own config,
# approvals and state, while keeping your real home for claude / gh / git logins.
_ALT = os.environ.get("TODO_AGENT_HOME")
CONFIG_DIR = Path(_ALT).expanduser() if _ALT else HOME / ".config" / "todo"
CONFIG = CONFIG_DIR / "agent.json"
APPROVALS = CONFIG_DIR / "agent-approvals.json"
STATE = (CONFIG_DIR / "state") if _ALT else (
    (HOME / "Library" / "Application Support" / "todo-agent") if MAC else (HOME / ".local" / "state" / "todo-agent"))
PLIST = HOME / "Library" / "LaunchAgents" / "dev.todo.agent.plist"
UNIT = HOME / ".config" / "systemd" / "user" / "todo-agent.service"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-Z\\-_]|\r|\x08|\x04|\^D")
APPROVAL_TIMEOUT = 600
_lock = threading.Lock()
_task_runs = 0  # agent runs going right now (capped by max_task_runs)


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        sys.exit(f"No config at {CONFIG}. Install with: curl -fsSL http://<todo>/agent/install.sh | sh")
    cfg["url"] = cfg["url"].rstrip("/")
    cfg.setdefault("name", socket.gethostname().split(".")[0])
    cfg.setdefault("terminal", "Terminal")
    return cfg


def api(cfg: dict, path: str, body: dict, timeout: float = 15) -> dict:
    request = urllib.request.Request(
        cfg["url"] + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['token']}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def upload(cfg: dict, occurrence_id: int, path: Path, *, step_id=None, note: str = "") -> dict:
    """Attach a file to a cadence meeting."""
    params = {"name": path.name, "source": "agent", "note": note}
    if step_id:
        params["step_id"] = str(step_id)
    request = urllib.request.Request(
        f"{cfg['url']}/api/occurrences/{occurrence_id}/files?{urllib.parse.urlencode(params)}",
        data=path.read_bytes(), method="POST",
        headers={"Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                 "Authorization": f"Bearer {cfg['token']}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def attach_outputs(cfg: dict, run: dict, started: float) -> None:
    """After a prep-step run: upload the step's output files that this run wrote."""
    if not run.get("occurrence_id") or not run.get("outputs"):
        return
    found: list[str] = []
    for pattern in run["outputs"]:
        pattern = os.path.expanduser(pattern)
        if not os.path.isabs(pattern):
            pattern = os.path.join(workdir(run), pattern)
        found += [f for f in glob.glob(pattern, recursive=True) if os.path.isfile(f) and os.path.getmtime(f) >= started - 2]
    lines = []
    for f in sorted(set(found))[:20]:
        try:
            upload(cfg, run["occurrence_id"], Path(f), step_id=run.get("step_id"))
            lines.append(f"[attached {os.path.basename(f)} to the meeting]")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"[couldn't attach {os.path.basename(f)}: {exc}]")
    if not found:
        lines.append(f"[no new files matched {', '.join(run['outputs'])}]")
    report(cfg, run["id"], output="\n" + "\n".join(lines) + "\n")


def report(cfg: dict, run_id: int, **fields) -> str | None:
    """Send status / output; returns the run's status on the server ('cancelled' = stop)."""
    try:
        return api(cfg, f"/api/agent/runs/{run_id}", {"name": cfg["name"], **fields}).get("status")
    except Exception as exc:  # noqa: BLE001 - the run goes on; the hub just lags
        log(f"run {run_id}: couldn't report ({exc})")
        return None


# ----- approvals -----

def fingerprint(run: dict) -> str:
    return hashlib.sha256(f"{run['command']}\0{run.get('cwd') or ''}".encode()).hexdigest()


def approvals() -> dict:
    try:
        return json.loads(APPROVALS.read_text())
    except (OSError, ValueError):
        return {}


def remember(run: dict) -> None:
    with _lock:
        data = approvals()
        data[fingerprint(run)] = {
            "label": run["label"], "command": run["command"], "cwd": run.get("cwd"),
            "approved_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        APPROVALS.write_text(json.dumps(data, indent=2) + "\n")
        APPROVALS.chmod(0o600)


def ask(cfg: dict, run: dict) -> str:
    """'once' | 'always' | 'deny'."""
    where = run.get("cwd") or "~"
    how = "in a terminal window" if run["mode"] == "terminal" else "in the background"
    if MAC:
        text = (f"Todo wants to run “{run['label']}” for {run.get('customer') or 'you'} on this Mac, {how}:\n\n"
                f"{run['command']}\n\nin {where}\n\nNew or changed command.")
        script = (
            f'display dialog {_as(text)} with title "Todo" buttons {{"Don\'t run", "Run once", "Always allow"}} '
            f'default button "Run once" cancel button "Don\'t run" with icon caution giving up after {APPROVAL_TIMEOUT}'
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        out = result.stdout
        if "gave up:true" in out or result.returncode != 0:
            return "deny"
        return "always" if "Always allow" in out else "once" if "Run once" in out else "deny"
    # Linux: the agent is a background service, so ask in a terminal window.
    STATE.mkdir(parents=True, exist_ok=True)
    request = STATE / f"ask-{run['id']}.json"
    answer = STATE / f"ask-{run['id']}.answer"
    answer.unlink(missing_ok=True)
    write_private_json(request, {"label": run["label"], "customer": run.get("customer"),
                                     "command": run["command"], "cwd": run.get("cwd"), "how": how})
    subprocess.Popen(["xdg-terminal-exec", sys.executable, os.path.abspath(__file__), "prompt", str(request)],
                     start_new_session=True)
    deadline = time.time() + APPROVAL_TIMEOUT
    while time.time() < deadline:
        if answer.exists():
            decision = answer.read_text().strip()
            answer.unlink(missing_ok=True)
            request.unlink(missing_ok=True)
            return decision if decision in ("once", "always") else "deny"
        time.sleep(0.5)
    request.unlink(missing_ok=True)
    return "deny"


def prompt(path: str) -> None:
    """Runs in a terminal window (Linux): show the command and ask."""
    run = json.loads(Path(path).read_text())
    print(f"\n  Todo wants to run \033[1m{run['label']}\033[0m for {run.get('customer') or 'you'}, {run['how']}\n")
    print(f"  in   {run.get('cwd') or '~'}")
    print(f"  run  {run['command']}\n")
    print("  New or changed command. 'always' remembers this exact command.\n")
    reply = input("  Run it? [o]nce / [a]lways / [N]o: ").strip().lower()
    decision = "always" if reply.startswith("a") else "once" if reply.startswith("o") or reply == "y" else "deny"
    Path(path).with_suffix(".answer").write_text(decision)


def _as(text: str) -> str:
    """An AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ----- running -----

def environment(cfg: dict, run: dict) -> dict:
    return {
        **os.environ,
        "TODO_URL": cfg["url"],
        "TODO_RUN_ID": str(run["id"]),
        "TODO_TASK_ID": str(run.get("task_id") or ""),
        "TODO_CUSTOMER": run.get("customer") or "",
        "TODO_CUSTOMER_ID": str(run.get("customer_id") or ""),
        "TODO_OCCURRENCE_ID": str(run.get("occurrence_id") or ""),
        "TODO_STEP_ID": str(run.get("step_id") or ""),
    }


def workdir(run: dict) -> str:
    path = os.path.expanduser(run.get("cwd") or "~")
    return path if os.path.isdir(path) else str(HOME)


def user_shell() -> str:
    return os.environ.get("SHELL") or ("/bin/zsh" if MAC else "/bin/bash")


def notify(title: str, body: str, failed: bool = False) -> None:
    if MAC:
        subprocess.run(["osascript", "-e", f"display notification {_as(body[:200])} with title {_as(title)}"
                        + (' sound name "Basso"' if failed else "")], check=False)
    else:
        subprocess.run(["notify-send", "-a", "Todo", *(["-u", "critical"] if failed else []), title, body], check=False)


def run_headless(cfg: dict, run: dict) -> None:
    started = time.time()
    report(cfg, run["id"], status="running")
    proc = subprocess.Popen(
        [user_shell(), "-lc", run["command"]], cwd=workdir(run), env=environment(cfg, run),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    )
    buffer: list[str] = []
    tail: list[str] = []
    done = threading.Event()

    def flush() -> None:
        while not done.wait(2):
            send()

    def send() -> None:
        with _lock:
            chunk, buffer[:] = "".join(buffer), []
        if chunk:
            report(cfg, run["id"], output=chunk)

    threading.Thread(target=flush, daemon=True).start()
    assert proc.stdout
    for line in proc.stdout:
        line = ANSI.sub("", line)
        with _lock:
            buffer.append(line)
        tail[:] = (tail + [line.rstrip()])[-3:]
    code = proc.wait()
    done.set()
    send()
    attach_outputs(cfg, run, started)
    report(cfg, run["id"], status="succeeded" if code == 0 else "failed", exit_code=code)
    notify(f"{run['label']} {'finished' if code == 0 else f'failed ({code})'}",
           "\n".join(t for t in tail if t) or (run.get("customer") or ""), failed=code != 0)


def run_terminal(cfg: dict, run: dict) -> None:
    """A terminal window with a real TTY; `script` records the session for the hub."""
    STATE.mkdir(parents=True, exist_ok=True)
    base = STATE / f"run-{run['id']}"
    record, exit_file, wrapper = base.with_suffix(".log"), base.with_suffix(".exit"), base.with_suffix(".sh")
    for f in (record, exit_file):
        f.unlink(missing_ok=True)
    env = environment(cfg, run)
    exports = "".join(f"export {k}={shlex.quote(env[k])}\n" for k in (
        "TODO_URL", "TODO_RUN_ID", "TODO_CUSTOMER", "TODO_CUSTOMER_ID", "TODO_OCCURRENCE_ID", "TODO_STEP_ID"))
    started = time.time()
    inner = f"{shlex.quote(user_shell())} -lc {shlex.quote(run['command'])}"
    recorder = (f"script -q {shlex.quote(str(record))} {inner}" if MAC
                else f"script -q -e -c {shlex.quote(inner)} {shlex.quote(str(record))}")
    wrapper.write_text(
        "#!/bin/bash\n"
        f"printf '\\033]0;%s\\007' {shlex.quote(run['label'] + ' · todo')}\n"
        f"cd {shlex.quote(workdir(run))} || exit 1\n{exports}"
        f"echo {shlex.quote('▶ ' + run['label'])}; echo\n"
        f"{recorder}\ncode=$?\necho \"$code\" > {shlex.quote(str(exit_file))}\n"
        f"echo; echo \"[{run['label']} exited with $code]\"; printf 'Press Enter to close '; read -r _\n"
    )
    wrapper.chmod(0o700)
    open_terminal(cfg, str(wrapper))
    report(cfg, run["id"], status="running")
    sent = 0
    deadline = time.time() + 12 * 3600
    while time.time() < deadline and not exit_file.exists():
        sent = _send_new(cfg, run, record, sent)
        time.sleep(2)
    sent = _send_new(cfg, run, record, sent)
    try:
        code = int(exit_file.read_text().strip())
    except (OSError, ValueError):
        report(cfg, run["id"], status="failed", error="The terminal session didn't report back")
        return
    attach_outputs(cfg, run, started)
    report(cfg, run["id"], status="succeeded" if code == 0 else "failed", exit_code=code)
    for f in (record, exit_file, wrapper):
        f.unlink(missing_ok=True)


def _send_new(cfg: dict, run: dict, record: Path, sent: int) -> int:
    try:
        data = record.read_bytes()
    except OSError:
        return sent
    if len(data) > sent:
        report(cfg, run["id"], output=ANSI.sub("", data[sent:].decode("utf-8", "replace")))
    return len(data)


def open_terminal(cfg: dict, script: str) -> None:
    if not MAC:
        subprocess.Popen(["xdg-terminal-exec", "bash", script], start_new_session=True)
        return
    app = cfg.get("terminal", "Terminal")
    command = f"/bin/bash {shlex.quote(script)}"
    if app == "Ghostty":
        subprocess.Popen(["open", "-na", "Ghostty", "--args", "-e", "/bin/bash", script])
    elif app == "iTerm":
        subprocess.run(["osascript", "-e", f'tell application "iTerm" to create window with default profile command {_as(command)}'], check=False)
    else:
        subprocess.run(["osascript", "-e", f'tell application "Terminal"\nactivate\ndo script {_as(command)}\nend tell'], check=False)


# ----- agents: a task run (or a setup check) is Claude Code in a worktree -----

def repo_for(cfg: dict, run: dict):
    """The repo to work in, or None for general work (a scratch folder, no git)."""
    project = run.get("project")
    if not project:
        return None
    return os.path.expanduser((cfg.get("repo_paths") or {}).get(project["name"]) or project["repo_path"])


def describe_agent(cfg: dict, run: dict) -> None:
    """What the approval shows and fingerprints: the agent file, its tools and model, and the
    repo. The prompt and task change every run, so they're not part of it."""
    agent = run["agent"]
    run["cwd"] = repo_for(cfg, run) or "(a scratch folder, no repo)"
    if agent.get("executor") == "codex-app":
        run["command"] = "Codex app/Remote via existing connected local daemon (workspace-write, user approvals) " + json.dumps(cfg.get("codex_app") or {}, sort_keys=True) + " model=" + str(agent.get("model"))
        return
    if agent.get("executor") == "codex-herdr":
        route = herdr_route(cfg, run)
        run["command"] = "codex (workspace-write, on-request) in Herdr " + json.dumps(route, sort_keys=True) + " model=" + str(agent.get("model"))
        return
    run["command"] = (f"claude --agent {agent['claude_agent']}"
                      + (f" --model {agent['model']}" if agent.get("model") else "")
                      + f" --allowedTools {shlex.quote(agent.get('allowed_tools') or '(defaults)')}")


def git(repo: str, *args: str) -> str:
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def workspace(run: dict, repo) -> str:
    """Where Claude works: a git worktree of the repo, or for general work a scratch folder of
    the task's own. Both are reused when the agent comes back to the task."""
    if repo is None:
        path = STATE / "workspaces" / (f"check-{run['id']}" if run["kind"] == "check" else f"task-{run['task_id']}")
        (path / "outputs").mkdir(parents=True, exist_ok=True)
        return str(path)
    # Git fetch/worktree setup writes shared refs/config. Serialize setup only;
    # task execution remains parallel, including across daemon processes.
    import fcntl
    locks = STATE / "repo-locks"
    locks.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(common_dir(repo).encode()).hexdigest()
    with (locks / key).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return worktree(run, repo)


def worktree(run: dict, repo: str) -> str:
    """The task's worktree (reused when the agent comes back to it), on its own branch."""
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
        raise RuntimeError(f"{repo} isn't a git checkout (set the project's repo path, or repo_paths in {CONFIG})")
    base = run["project"]["default_branch"]
    git(repo, "fetch", "--quiet", "origin", base)
    root = STATE / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    if run["kind"] == "check":
        path = root / f"check-{run['id']}"
        git(repo, "worktree", "add", "--detach", str(path), f"origin/{base}")
        return str(path)
    path = root / f"task-{run['task_id']}"
    if path.exists():
        return str(path)
    git(repo, "worktree", "prune")
    branch = run["branch"]
    exists = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                            capture_output=True).returncode == 0
    if exists:
        git(repo, "worktree", "add", str(path), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), f"origin/{base}")
    return str(path)


def claude_command(cfg: dict, run: dict, mcp_config: Path) -> list:
    agent = run["agent"]
    claude = os.environ.get("TODO_AGENT_CLAUDE") or cfg.get("claude") or "claude"
    cmd = [claude, "-p", run["prompt"], "--agent", agent["claude_agent"],
           "--mcp-config", str(mcp_config), "--strict-mcp-config",
           "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"]
    if agent.get("model"):
        cmd += ["--model", agent["model"]]
    if agent.get("max_turns"):
        cmd += ["--max-turns", str(agent["max_turns"])]
    if run.get("session_id") and run["kind"] == "task":
        cmd += ["--resume", run["session_id"]]
    # Last: it takes a list. The todo tools are always allowed (they only reach this task).
    return cmd + ["--allowedTools", *tool_rules(agent.get("allowed_tools") or ""), "mcp__todo"]


def tool_rules(text: str) -> list:
    """'Read Edit Bash(git *)' → ['Read', 'Edit', 'Bash(git *)']: split on spaces and commas
    outside parentheses."""
    rules, current, depth = [], "", 0
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch in " ,\n" and depth == 0:
            if current:
                rules.append(current)
            current = ""
        else:
            current += ch
    return rules + ([current] if current else [])


def readable(event: dict) -> tuple:
    """A stream-json event → (text for the log, session_id, usage). Usage is counted in tokens
    (subscription users don't pay the API price): new input incl. cache writes + output, and
    cache reads apart."""
    kind = event.get("type")
    session, usage, lines = event.get("session_id"), None, []
    if kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                lines.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                args = block.get("input") or {}
                hint = args.get("command") or args.get("file_path") or args.get("pattern") or args.get("note") or ""
                lines.append(f"→ {block.get('name')} {str(hint)[:160]}".rstrip())
    elif kind == "result":
        u = event.get("usage") or {}
        if u:
            usage = {"tokens": sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens")),
                     "cached_tokens": int(u.get("cache_read_input_tokens") or 0)}
        lines.append(f"[{event.get('subtype', 'done')}: {event.get('num_turns', '?')} turns"
                     + (f", {usage['tokens']:,} tokens" if usage else "") + "]")
    elif kind == "system" and event.get("subtype") == "init":
        lines.append(f"[claude {event.get('model', '')} in {event.get('cwd', '')}]")
    return ("\n".join(lines) + "\n") if lines else "", session, usage


def attach_outputs_to_task(cfg: dict, run: dict, path: str) -> str:
    """Upload what the agent saved in ./outputs/ (new or changed since the last run) to its
    task: the user sees the task, never this folder. Returns lines for the run log."""
    folder = Path(path) / "outputs"
    if run["kind"] != "task" or not folder.is_dir():
        return ""
    manifest = STATE / f"outputs-task-{run['task_id']}.json"
    try:
        sent = json.loads(manifest.read_text())
    except (OSError, ValueError):
        sent = {}
    lines = []
    for f in sorted(p for p in folder.rglob("*") if p.is_file() and not p.name.startswith("."))[:50]:
        rel = str(f.relative_to(folder))
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        if sent.get(rel) == digest:
            continue
        name = rel.replace(os.sep, "-")
        try:
            request = urllib.request.Request(
                f"{cfg['url']}/api/agent/runs/{run['id']}/files?"
                + urllib.parse.urlencode({"machine": cfg["name"], "name": name}),
                data=f.read_bytes(), method="POST",
                headers={"Content-Type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                         "Authorization": f"Bearer {cfg['token']}"},
            )
            urllib.request.urlopen(request, timeout=120).read()
            sent[rel] = digest
            lines.append(f"[attached {name} to the task]")
        except Exception as exc:  # noqa: BLE001
            detail = exc.read().decode(errors="replace") if isinstance(exc, urllib.error.HTTPError) else str(exc)
            lines.append(f"[couldn't attach {name}: {detail}]")
    manifest.write_text(json.dumps(sent))
    return ("\n".join(lines) + "\n") if lines else ""


# ----- opt-in interactive Codex assignments (no external launcher dependency) -----

TOKEN_ENV = "TODO_WORKER_RUN_TOKEN"


def codex_native_args(cfg: dict, run: dict, cwd: str) -> list[str]:
    """Native config contains a credential variable name, never a credential value."""
    args = ["codex", "--no-daemon", "--cd", cwd, "--sandbox", "workspace-write",
            "--ask-for-approval", "on-request", "-c",
            'mcp_servers={todo_worker={url=' + json.dumps(cfg["url"] + run["mcp_path"])
            + ',bearer_token_env_var="' + TOKEN_ENV + '",required=true}}',
            "-c", 'shell_environment_policy={inherit="all",ignore_default_excludes=false,filters={'
            + TOKEN_ENV + '="exclude"}}']
    if run["agent"].get("model"):
        args += ["--model", run["agent"]["model"]]
    return args


def receipt_files(folder: Path):
    return (p for p in folder.glob("run-*.json") if re.fullmatch(r"run-[0-9]+\.json", p.name))


def handoff_path(receipt: Path) -> Path:
    return receipt.with_suffix(".handoff.json")


def write_private_json(path: Path, data: dict) -> None:
    """Apply owner-only permissions before writing a task-runtime secret."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("Unsafe private task configuration file")
        os.fchmod(stream.fileno(), 0o600)
        os.ftruncate(stream.fileno(), 0)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())


def write_one_use_handoff(receipt: Path, data: dict) -> Path:
    """A single-use private file outside the repository, created before terminal input."""
    path = handoff_path(receipt)
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise RuntimeError("Codex handoff directory must be private and owned by this user")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def write_codex_handoff(receipt: Path, cfg: dict, run: dict, cwd: str) -> Path:
    return write_one_use_handoff(receipt, {"token": run["token"], "argv": codex_native_args(cfg, run, cwd)})


def write_worker_handoff(receipt: Path, cfg: dict, run: dict) -> Path:
    return write_one_use_handoff(receipt, {"kind": "worker", "token": run["token"], "url": cfg["url"] + run["mcp_path"]})


def consume_private_handoff(path: Path) -> dict:
    """Validate, consume and unlink before exec; values must never be printed."""
    import fcntl
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise RuntimeError("Unsafe Codex handoff directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("Unsafe Codex handoff file")
        if info.st_size > 64_000:
            raise RuntimeError("Codex handoff is too large")
        try:
            data = json.load(stream)
        finally:
            # Never leave an unread or malformed credential on disk after consumption.
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError("Codex handoff identity changed")
            path.unlink()
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    return data


def consume_codex_handoff(path: Path) -> dict:
    data = consume_private_handoff(path)
    if (not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]
            or not isinstance(data.get("argv"), list) or not data["argv"] or data["argv"][0] != "codex"
            or not all(isinstance(a, str) and data["token"] not in a for a in data["argv"])):
        raise RuntimeError("Invalid Codex handoff")
    return data


def codex_herdr_bootstrap(path: Path) -> None:
    data = consume_codex_handoff(path)
    env = os.environ.copy()
    env[TOKEN_ENV] = data["token"]
    # Direct exec: no shell substitution, secret shell export, or extra token argv.
    os.execvpe("codex", data["argv"], env)


def start_codex_handoff(cfg: dict, record: dict, ticket: Path) -> None:
    existing = herdr_call(cfg, record["session"], "agent", "list")["agents"]
    if any(a.get("pane_id") == record["pane_id"] or a.get("name") == record["agent_name"] for a in existing):
        raise RuntimeError("New Codex pane or launch name is already occupied; no command was sent")
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "codex-herdr-bootstrap", str(ticket)])
    herdr_call(cfg, record["session"], "pane", "run", record["pane_id"], command)
    # No retries of pane run. Only observe this newly created pane.
    deadline = time.monotonic() + 30
    named = False
    while True:
        agents = herdr_call(cfg, record["session"], "agent", "list")["agents"]
        if any(a.get("name") == record["agent_name"] and a.get("pane_id") != record["pane_id"] for a in agents):
            raise RuntimeError("New Codex launch name is already in use")
        agent = next((a for a in agents if a.get("pane_id") == record["pane_id"]), None)
        if agent:
            if ticket.exists():
                raise RuntimeError("New pane agent did not consume the credential handoff; refusing to adopt it")
            if agent.get("agent") != "codex" or not agent.get("terminal_id"):
                raise RuntimeError("Unexpected occupant of the new Codex pane")
            if record.get("terminal_id") and record["terminal_id"] != agent["terminal_id"]:
                raise RuntimeError("New Codex terminal identity changed")
            record["terminal_id"] = agent["terminal_id"]
            if not named:
                herdr_call(cfg, record["session"], "agent", "rename", record["pane_id"], record["agent_name"])
                named = True
            current = owned_agent(cfg, record)
            if current.get("agent_status") == "blocked":
                raise RuntimeError("New Codex requires manual startup input in Herdr")
            if current.get("interactive_ready") and current.get("agent_status") in ("idle", "done"):
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("New Codex did not become ready; inspect its pane before retrying")
        time.sleep(0.25)


def receipt_data(record: dict) -> dict:
    """Receipts are recovery metadata, never a credential store (including older drafts)."""
    token = (record.get("run") or {}).get("token")
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(v) for key, v in value.items() if key not in ("token", "token_hash")}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str) and token:
            return value.replace(token, "[redacted]")
        return value
    return clean(record)


def task_error(exc: Exception, run: dict) -> str:
    text = str(exc)
    if run.get("token"):
        text = text.replace(run["token"], "[redacted]")
    return text[:2000]


def herdr_call(cfg: dict, session: str, *args: str) -> dict:
    """Every command targets the selected session, never the focused session."""
    command = [cfg.get("herdr_cli", "herdr"), "--session", session, *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    if result.returncode:
        # Terminal diagnostics may contain private process output; never forward them.
        raise RuntimeError(f"Herdr {args[0]} operation failed (exit {result.returncode}); inspect the recorded pane")
    data = json.loads(result.stdout)
    if data.get("error"):
        raise RuntimeError("Herdr operation failed; inspect the recorded pane")
    return data.get("result", data)


def common_dir(path: str) -> str:
    raw = git(path, "rev-parse", "--git-common-dir")
    return str((Path(raw) if Path(raw).is_absolute() else Path(path) / raw).resolve())


def canonical_root(repo: str) -> str:
    """Git's primary worktree, verified by common-dir; no project-name guessing."""
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            root = str(Path(line[9:]).resolve())
            if common_dir(root) != common_dir(repo):
                raise RuntimeError("Repository primary-worktree identity mismatch")
            return root
    raise RuntimeError("Cannot establish the repository's primary worktree")


def harness_location(cfg: dict, run: dict) -> dict:
    repo = repo_for(cfg, run)
    if repo:
        repo = str(Path(repo).resolve())
        return {"repo": repo, "project_root": canonical_root(repo)}
    return {"repo": None, "project_root": str((STATE / "workspaces").resolve())}


def herdr_route(cfg: dict, run: dict) -> dict:
    if run.get("kind") != "task":
        raise RuntimeError("Codex harnesses execute task assignments, not legacy setup checks")
    location = harness_location(cfg, run)
    # One named session per machine. Per-project entries are optional overrides only.
    defaults = cfg.get("herdr") or {}
    project = run.get("project") or {}
    override = (cfg.get("herdr_projects") or {}).get(str(project.get("id")))
    if override is None:
        override = (cfg.get("herdr_projects") or {}).get(project.get("name"))
    route = {"session": defaults.get("session", "default"), **(override or {})}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", route.get("session", "")):
        raise RuntimeError("Set the machine's herdr.session to an explicit running session name")
    if route.get("project_root"):
        root = Path(os.path.expanduser(route["project_root"]))
        if not location["repo"] or not root.is_absolute() or not root.is_dir() or common_dir(str(root)) != common_dir(location["repo"]):
            raise RuntimeError("Herdr project_root must be a checkout of the task repository")
        location["project_root"] = str(root.resolve())
    if not location["repo"] and defaults.get("general_workspace"):
        route["workspace"] = defaults["general_workspace"]
    if run["agent"].get("allowed_tools") or run["agent"].get("max_turns"):
        raise RuntimeError("Claude allowed_tools/max_turns cannot be translated to Codex")
    return {**route, **location}


def location_key(path: str, route: dict) -> str | None:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_dir():
        return None
    candidate = candidate.resolve()
    root = Path(route["project_root"])
    if not route["repo"] and (candidate == root or root in candidate.parents):
        return "scratch:" + str(root)
    try:
        return common_dir(str(candidate))
    except RuntimeError:
        return "directory:" + str(candidate)


def herdr_workspace(cfg: dict, route: dict) -> str | None:
    session = route["session"]
    # Session list is a local discovery command and does not use inherited pane context.
    result = subprocess.run([cfg.get("herdr_cli", "herdr"), "session", "list", "--json"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode or not any(s["name"] == session and s["running"]
                                  for s in json.loads(result.stdout)["sessions"]):
        raise RuntimeError("Selected Herdr session is absent or stopped")
    explicit = route.get("workspace")
    workspaces = ([herdr_call(cfg, session, "workspace", "get", explicit)["workspace"]] if explicit else
                  herdr_call(cfg, session, "workspace", "list")["workspaces"])
    matches, unknown = [], []
    identity = location_key(route["project_root"], route)
    for ws in workspaces:
        wid = ws["workspace_id"]
        paths = [(ws.get("worktree") or {}).get("checkout_path")]
        paths += [p.get("foreground_cwd") or p.get("cwd")
                  for p in herdr_call(cfg, session, "pane", "list", "--workspace", wid)["panes"]]
        keys = set()
        for path in filter(None, paths):
            try:
                key = location_key(path, route)
                if key:
                    keys.add(key)
            except (RuntimeError, OSError):
                pass
        if identity in keys:
            matches.append(wid)
        elif not keys:
            unknown.append(wid)
    if len(matches) == 1:
        return matches[0]
    if explicit or matches or unknown:
        raise RuntimeError("Herdr workspace identity is ambiguous or mismatched; select a verified workspace explicitly")
    return None


def save_receipt(path: Path, data: dict) -> None:
    """Atomic durable state, private even with a permissive user umask."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(receipt_data(data), stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def launch_public(record: dict) -> dict:
    return {k: record[k] for k in ("session", "workspace", "tab", "pane_id", "agent_name", "cwd",
                                 "project_root", "terminal_id", "route", "thread_id", "turn_id", "codex_project", "remote_server", "app_installation", "status", "error") if k in record}


def owned_agent(cfg: dict, record: dict) -> dict:
    agent = herdr_call(cfg, record["session"], "agent", "get", record["agent_name"])["agent"]
    if (agent.get("name") != record["agent_name"] or agent.get("pane_id") != record["pane_id"]
            or agent.get("agent") != "codex"
            or (record.get("terminal_id") and agent.get("terminal_id") != record["terminal_id"])):
        raise RuntimeError("Recorded Herdr agent identity changed; manual inspection required")
    return agent


def monitor_herdr(cfg: dict, record: dict, receipt: Path) -> None:
    """Observe only our new agent. Blocked/unknown never imply completion."""
    run = record["run"]
    while True:
        response = api(cfg, f"/api/agent/runs/{run['id']}",
                       {"name": cfg["name"], "launch": launch_public(record)})
        if response["status"] == "cancelled":
            record["status"] = "cancel_pending"
            save_receipt(receipt, record)
            api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "launch": launch_public(record)})
            owned_agent(cfg, record)
            # Named-agent input is identity-validated by Herdr. Never close a raw pane:
            # its occupant could change between a read and a destructive close.
            herdr_call(cfg, record["session"], "agent", "send-keys", record["agent_name"], "ctrl+c")
            deadline = time.monotonic() + 10
            while owned_agent(cfg, record).get("agent_status", "unknown") not in ("idle", "done"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Cancellation sent; agent has not settled. Inspect the pane; no automatic retry")
                time.sleep(0.25)  # observe the one interrupt; never send it again
            public = launch_public(record)
            public["status"] = "cancelled"
            api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "launch": public})
            record["status"] = "cancelled"
            save_receipt(receipt, record)
            return
        if response["status"] in ("failed", "expired", "declined"):
            raise RuntimeError("Server run ended while its interactive agent may still be alive")
        agent = owned_agent(cfg, record)
        status = agent.get("agent_status", "unknown")
        record["status"] = status
        record["saw_activity"] = record.get("saw_activity", False) or status in ("working", "blocked")
        save_receipt(receipt, record)
        if status in ("idle", "done") and (record["saw_activity"] or response.get("outcome") in ("review", "question")):
            attached = attach_outputs_to_task(cfg, run, record["cwd"])
            public = launch_public(record)
            public["status"] = "finished"
            api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "status": "succeeded",
                                                 "output": attached or None, "launch": public})
            record["status"] = "finished"
            save_receipt(receipt, record)
            return
        if not record["saw_activity"] and time.time() - record.get("submitted_at", time.time()) > 15:
            raise RuntimeError("No prompt activity observed; inspect the pane before retrying")
        time.sleep(2)


def run_herdr(cfg: dict, run: dict) -> None:
    import fcntl  # Linux opt-in; existing macOS/general/tool execution stays unchanged
    route = herdr_route(cfg, run)
    namespace = hashlib.sha256(cfg["url"].encode()).hexdigest()[:12]
    folder = STATE / "herdr" / namespace
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = folder / f"run-{run['id']}.json"
    with (folder / f"task-{run['task_id']}.lock").open("a") as task_lock:
        fcntl.flock(task_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Even a cancelled/failed server run may own a live terminal after network loss.
        for previous in receipt_files(folder):
            record = json.loads(previous.read_text())
            if record["run"]["task_id"] == run["task_id"] and record["status"] not in ("finished", "cancelled"):
                raise RuntimeError(f"Inspect existing Herdr receipt {previous}; automatic relaunch refused")
        if receipt.exists():
            raise RuntimeError("This run already has a one-shot launch receipt")
        path = workspace(run, route["repo"])
        if route["repo"] and (common_dir(path) != common_dir(route["project_root"]) or git(path, "branch", "--show-current") != run["branch"]):
            raise RuntimeError("Task worktree repository or branch does not match the assignment")
        record = {"run": receipt_data({"run": run})["run"], "session": route["session"], "project_root": route["project_root"],
                  "cwd": path, "agent_name": f"todo-{namespace[:6]}-{run['id']}", "status": "prepared"}
        try:
            # Serialize project workspace resolution/creation across tasks and processes.
            with (folder / "topology.lock").open("a") as topology:
                fcntl.flock(topology, fcntl.LOCK_EX)
                wid = herdr_workspace(cfg, route)
                record.update(workspace=wid, status="creating")
                save_receipt(receipt, record)  # before the first mutation
                api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "launch": launch_public(record)})
                if api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"]})["status"] == "cancelled":
                    record["status"] = "cancelled"
                    save_receipt(receipt, record)
                    return
                args = (["tab", "create", "--workspace", wid] if wid else ["workspace", "create"])
                result = herdr_call(cfg, route["session"], *args, "--cwd", path,
                                    "--label", run["label"] if wid else (run.get("project") or {}).get("name", "Todo assignments"), "--no-focus")
                record.update(workspace=wid or result["workspace"]["workspace_id"],
                              pane_id=result["root_pane"]["pane_id"], tab=result["tab"]["tab_id"], status="starting")
                if result["root_pane"].get("terminal_id"):
                    record["terminal_id"] = result["root_pane"]["terminal_id"]
                save_receipt(receipt, record)
            # Native Codex config; no Claude flags, approval bypass, HOME changes or fabricated HERDR_ENV.
            if api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"]})["status"] == "cancelled":
                record["status"] = "cancelled"
                save_receipt(receipt, record)
                report(cfg, run["id"], launch=launch_public(record))
                return
            ticket = write_codex_handoff(receipt, cfg, run, path)
            start_codex_handoff(cfg, record, ticket)
            agent = owned_agent(cfg, record)
            if not agent.get("terminal_id"):
                raise RuntimeError("Herdr did not return a stable terminal identity")
            record["terminal_id"] = agent["terminal_id"]
            record["status"] = "submitting"
            save_receipt(receipt, record)
            if api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"]})["status"] == "cancelled":
                record["status"] = "cancelled"  # fresh idle agent has received no task prompt
                api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "launch": launch_public(record)})
                save_receipt(receipt, record)
                return
            # One submission only. A timeout is uncertain, never a reason to resend.
            herdr_call(cfg, route["session"], "agent", "prompt", record["agent_name"], run["prompt"])
            record["status"] = "submitted"
            record["submitted_at"] = time.time()
            save_receipt(receipt, record)
            report(cfg, run["id"], status="running", branch=run["branch"], launch=launch_public(record),
                   output=f"[Codex in Herdr {record['session']} / {record['workspace']} / {record['pane_id']}]\n")
            monitor_herdr(cfg, record, receipt)
        except Exception as exc:
            message = task_error(exc, run)
            record.update(status="needs_inspection", error=message)
            save_receipt(receipt, record)
            report(cfg, run["id"], status="failed", error=message, launch=launch_public(record))
            raise RuntimeError(message) from None
        finally:
            handoff_path(receipt).unlink(missing_ok=True)


# ----- native Codex app / connected-computer assignments -----

class LocalWebSocket:
    """WebSocket client over the native raw-byte proxy; no TCP listener or dependencies."""
    LIMIT = 16 * 1024 * 1024

    def __init__(self, incoming, outgoing):
        self.incoming, self.outgoing = incoming, outgoing
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.key = base64.b64encode(os.urandom(16)).decode()
        self.outgoing.write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                             "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: "
                             + self.key + "\r\n\r\n").encode())
        self.outgoing.flush()

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.incoming.read(size - len(data))
            if not part:
                raise RuntimeError("Native transport closed")
            data.extend(part)
        return bytes(data)

    def handshake(self):
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            if len(header) >= 16384:
                raise RuntimeError("Invalid native handshake")
            header.extend(self.exact(1))
        lines = header.decode("ascii").split("\r\n")
        fields = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        fields = {k.lower(): v.strip() for k, v in fields.items()}
        expected = base64.b64encode(hashlib.sha1((self.key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if (lines[0].split()[1] != "101" or fields.get("sec-websocket-accept") != expected
                or fields.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in fields.get("connection", "").lower()):
            raise RuntimeError("Native WebSocket handshake rejected; no task launched")
        self.ready.set()

    def frame(self, payload, opcode=1):
        import struct
        if len(payload) > self.LIMIT:
            raise RuntimeError("Native message exceeds transport limit")
        size = len(payload)
        length = bytes([size | 128]) if size < 126 else (b"\xfe" + struct.pack("!H", size) if size <= 65535 else b"\xff" + struct.pack("!Q", size))
        mask = os.urandom(4)
        data = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        with self.lock:
            self.outgoing.write(bytes([128 | opcode]) + length + mask + data)
            self.outgoing.flush()

    def message(self):
        import struct
        fragments = bytearray()
        started = False
        while True:
            first, second = self.exact(2)
            final, opcode = bool(first & 128), first & 15
            if first & 112 or second & 128:
                raise RuntimeError("Invalid native WebSocket frame")
            size = second & 127
            if size == 126: size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127: size = struct.unpack("!Q", self.exact(8))[0]
            if size > self.LIMIT or len(fragments) + size > self.LIMIT:
                raise RuntimeError("Native message exceeds transport limit")
            if opcode >= 8 and (not final or size > 125):
                raise RuntimeError("Invalid native control frame")
            payload = self.exact(size)
            if opcode == 8: raise RuntimeError("Native transport closed")
            if opcode == 9:
                self.frame(payload, 10)
                continue
            if opcode == 10: continue
            if opcode == 1 and not started: started = True
            elif opcode != 0 or not started: raise RuntimeError("Invalid native message frame")
            fragments.extend(payload)
            if final: return json.loads(fragments.decode("utf-8"))


class CodexAppServer:
    """Stdio proxy to an existing local daemon. Never start, stop, pair or enable it."""
    def __init__(self, cfg: dict):
        import queue
        self.inbox = queue.Queue(maxsize=256)
        self.blocked = set()
        self.sequence = 0
        command = [cfg.get("codex", "codex"), "app-server", "proxy"]
        if (cfg.get("codex_app") or {}).get("socket"):
            command += ["--sock", cfg["codex_app"]["socket"]]
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
        try:
            self.transport = LocalWebSocket(self.proc.stdout, self.proc.stdin)
        except Exception:
            self.close()
            raise
        def read():
            try:
                self.transport.handshake()
                while True:
                    self.inbox.put(self.transport.message())
            except Exception:
                pass  # no native diagnostic values in public logs
            finally:
                self.inbox.put(None)
        threading.Thread(target=read, daemon=True).start()
        try:
            self.rpc("initialize", {"clientInfo": {"name": "todo_agent", "title": "Todo", "version": VERSION},
                                    "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def send(self, message: dict) -> None:
        if not self.transport.ready.wait(10):
            raise RuntimeError("Native WebSocket handshake unavailable; no task launched")
        self.transport.frame(json.dumps(message).encode())

    def rpc(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        import queue
        self.sequence += 1
        rid = self.sequence
        self.send({"id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Codex app {method} response is uncertain; inspect the native task before retrying")
            try:
                message = self.inbox.get(timeout=remaining)
            except queue.Empty:
                raise RuntimeError(f"Codex app {method} timed out; inspect before retrying") from None
            if message is None:
                raise RuntimeError("Existing Codex daemon is unavailable; connect this machine in the app first")
            if message.get("id") == rid and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"Codex app rejected {method}; check native compatibility and setup")
                return message.get("result", {})
            # Requests remain unanswered here. The native app/Remote owns user decisions.
            tid = (message.get("params") or {}).get("threadId")
            if "method" in message and "id" in message and tid:
                self.blocked.add(tid)
            if message.get("method") == "serverRequest/resolved" and tid:
                self.blocked.discard(tid)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()  # this proxy only, never the shared daemon or a model process
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def app_connected(client: CodexAppServer, record: dict | None = None) -> dict:
    status = client.rpc("remoteControl/status/read")
    if status.get("status") != "connected" or not status.get("installationId"):
        state = status.get("status")
        state = state if state in ("disabled", "connecting", "errored") else "unavailable"
        raise RuntimeError("Native Codex Remote reports " + state + "; pairing alone does not establish a live connection. "
                           "Resolve the host connection before starting this task; Todo will not enable or pair it automatically")
    if record and status["installationId"] != record.get("app_installation"):
        raise RuntimeError("Connected Codex installation changed; inspect the original task")
    return status


def app_project(client: CodexAppServer, route: dict, name: str, key: str) -> str:
    matches, cursor = [], None
    identity = location_key(route["project_root"], route)
    while True:
        page = client.rpc("project/list", {"limit": 100, "cursor": cursor})
        for project in page["data"]:
            roots = project.get("roots") or []
            if any(location_key(r["path"], route) == identity for r in roots):
                matches.append(project["id"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    if len(matches) > 1:
        raise RuntimeError("Several native Codex projects match this repository; resolve that ambiguity in the app")
    if matches:
        return matches[0]
    return client.rpc("project/create", {"name": name, "roots": [{"path": route["project_root"]}],
                                          "idempotencyKey": "todo-" + key})["project"]["id"]


def worker_stdio_config(ticket: Path) -> dict:
    # Only a private file path, not the token, enters the shared daemon's persisted config.
    return {"mcp_servers": {"todo_worker": {"command": sys.executable,
            "args": [str(Path(__file__).resolve()), "codex-worker-proxy", str(ticket)], "required": True}}}


def app_owned_thread(client: CodexAppServer, record: dict) -> dict:
    thread = client.rpc("thread/read", {"threadId": record["thread_id"], "includeTurns": False})["thread"]
    if (thread.get("id") != record["thread_id"] or str(Path(thread.get("cwd", "")).resolve()) != record["cwd"]
            or thread.get("projectId") != record["codex_project"]):
        raise RuntimeError("Recorded native Codex task identity changed; no control command sent")
    return thread


def app_turn(client: CodexAppServer, record: dict) -> dict:
    cursor = None
    while True:
        page = client.rpc("thread/turns/list", {"threadId": record["thread_id"], "limit": 100,
                                                "itemsView": "notLoaded", "sortDirection": "desc", "cursor": cursor})
        turn = next((t for t in page["data"] if t["id"] == record["turn_id"]), None)
        if turn:
            return turn
        cursor = page.get("nextCursor")
        if not cursor:
            raise RuntimeError("Recorded native Codex turn is unavailable; inspect before retrying")


def monitor_codex_app(cfg: dict, record: dict, receipt: Path, client: CodexAppServer | None = None) -> None:
    owned_connection = client is None
    client = client or CodexAppServer(cfg)
    try:
        app_connected(client, record)
        run = record["run"]
        while True:
            response = api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "launch": launch_public(record)})
            thread = app_owned_thread(client, record)
            turn = app_turn(client, record)
            if response["status"] == "cancelled":
                if turn["status"] == "inProgress":
                    record["status"] = "cancel_pending"
                    save_receipt(receipt, record)
                    client.rpc("turn/interrupt", {"threadId": record["thread_id"], "turnId": record["turn_id"]})
                    # Interrupt is one-shot and scoped to this recorded turn. Observe only.
                    deadline = time.monotonic() + 10
                    while app_turn(client, record)["status"] == "inProgress":
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Native task interrupt is uncertain; inspect in Codex, no automatic retry")
                        time.sleep(0.25)
                record["status"] = "cancelled"
                api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "launch": launch_public(record)})
                save_receipt(receipt, record)
                return
            if response["status"] in ("failed", "expired", "declined"):
                raise RuntimeError("Todo run ended while its native task may still be active")
            if turn["status"] in ("completed", "failed", "interrupted"):
                record["status"] = "finished"
                result = "succeeded" if turn["status"] == "completed" else "failed"
                attached = attach_outputs_to_task(cfg, run, record["cwd"])
                api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "status": result,
                        "output": attached or None, "launch": launch_public(record),
                        "error": None if result == "succeeded" else "Native Codex turn " + turn["status"]})
                save_receipt(receipt, record)
                return
            flags = (thread.get("status") or {}).get("activeFlags", [])
            record["status"] = "blocked" if flags or record["thread_id"] in client.blocked else "working"
            save_receipt(receipt, record)
            time.sleep(2)
    finally:
        if owned_connection:
            client.close()


def run_codex_app(cfg: dict, run: dict) -> None:
    import fcntl
    route = harness_location(cfg, run)
    namespace = hashlib.sha256(cfg["url"].encode()).hexdigest()[:12]
    folder = STATE / "herdr" / namespace  # common legacy receipt namespace prevents cross-route duplicates
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = folder / f"run-{run['id']}.json"
    with (folder / f"task-{run['task_id']}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for previous in receipt_files(folder):
            prior = json.loads(previous.read_text())
            if prior["run"]["task_id"] == run["task_id"] and prior["status"] not in ("finished", "cancelled"):
                raise RuntimeError("Existing assignment receipt requires inspection; native launch refused")
        if receipt.exists():
            raise RuntimeError("This native run already has a one-shot receipt")
        client = CodexAppServer(cfg)
        try:
            connected = app_connected(client)
            path = workspace(run, route["repo"])
            if route["repo"] and (common_dir(path) != common_dir(route["repo"]) or git(path, "branch", "--show-current") != run["branch"]):
                raise RuntimeError("Native task worktree identity mismatch")
            record = {"run": receipt_data({"run": run})["run"], "route": "codex-app", "cwd": path,
                      "project_root": route["project_root"], "app_installation": connected["installationId"],
                      "remote_server": connected.get("serverName"), "status": "creating"}
            save_receipt(receipt, record)
            api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "launch": launch_public(record)})
            if api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"]})["status"] == "cancelled":
                record["status"] = "cancelled"
                save_receipt(receipt, record)
                return
            with (folder / "topology.lock").open("a") as topology:
                fcntl.flock(topology, fcntl.LOCK_EX)
                identity = location_key(route["project_root"], route)
                record["codex_project"] = app_project(client, route, (run.get("project") or {}).get("name", "Todo assignments"),
                                                       hashlib.sha256(identity.encode()).hexdigest())
                save_receipt(receipt, record)
            if api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"]})["status"] == "cancelled":
                record["status"] = "cancelled"
                save_receipt(receipt, record)
                return
            ticket = write_worker_handoff(receipt, cfg, run)
            params = {"cwd": path, "projectId": record["codex_project"], "ephemeral": False,
                      "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": "workspace-write",
                      "config": worker_stdio_config(ticket), "environments": [], "serviceName": "todo-agent"}
            if run["agent"].get("model"):
                params["model"] = run["agent"]["model"]
            # A new persistent native thread per Todo run. Never attach to an arbitrary existing chat.
            record["status"] = "starting"
            save_receipt(receipt, record)
            thread = client.rpc("thread/start", params)["thread"]
            record["thread_id"] = thread["id"]
            save_receipt(receipt, record)
            if ticket.exists():
                raise RuntimeError("Native worker credential handoff was not consumed; no task prompt sent")
            app_owned_thread(client, record)
            client.rpc("thread/name/set", {"threadId": record["thread_id"], "name": run["label"]})
            record["status"] = "submitting"
            save_receipt(receipt, record)
            if api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"]})["status"] == "cancelled":
                record["status"] = "cancelled"
                save_receipt(receipt, record)
                return
            turn = client.rpc("turn/start", {"threadId": record["thread_id"], "input": [{"type": "text", "text": run["prompt"]}]})["turn"]
            record.update(turn_id=turn["id"], status="submitted")
            save_receipt(receipt, record)
            api(cfg, f"/api/agent/runs/{run['id']}", {"name": cfg["name"], "status": "running", "session_id": record["thread_id"],
                    "branch": run.get("branch"), "launch": launch_public(record),
                    "output": "[Native Codex task on " + (record.get("remote_server") or "connected computer") + "]\n"})
            monitor_codex_app(cfg, record, receipt, client)
        except Exception as exc:
            if "record" in locals():
                message = task_error(exc, run)
                record.update(status="needs_inspection", error=message)
                save_receipt(receipt, record)
                report(cfg, run["id"], status="failed", error=message, launch=launch_public(record))
            raise RuntimeError(task_error(exc, run)) from None
        finally:
            handoff_path(receipt).unlink(missing_ok=True)
            client.close()


def worker_proxy_offline(message: dict) -> dict:
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "todo-worker-offline", "version": VERSION},
                "instructions": "This Todo run is no longer connected. Use Todo to start a fresh scoped run; never mark its task done directly."}
    if method == "tools/list":
        return {"tools": []}
    if method == "ping":
        return {}
    return {"isError": True, "content": [{"type": "text", "text": "Todo run is unavailable; start a fresh scoped run from Todo."}]}


def codex_worker_proxy(ticket: Path) -> None:
    """Native stdio MCP → existing stateless HTTP worker. No listener or shared-daemon secret."""
    try:
        data = consume_private_handoff(ticket)
    except FileNotFoundError:
        data = None  # Reopening an old native chat must not restore an expired credential.
    if data and (data.get("kind") != "worker" or not data.get("token") or not data.get("url", "").endswith("/mcp/agent")):
        raise RuntimeError("Invalid private worker handoff")
    protocol = None
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue  # stateless endpoint needs no initialized notification acknowledgement
        try:
            if data:
                request = urllib.request.Request(data["url"], data=json.dumps(message).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                   "Authorization": "Bearer " + data["token"], **({"MCP-Protocol-Version": protocol} if protocol else {})})
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        payload = json.load(response)
                except urllib.error.HTTPError as exc:
                    if exc.code not in (401, 403):
                        raise
                    data = None
                    payload = {"jsonrpc": "2.0", "id": message["id"], "result": worker_proxy_offline(message)}
                if message.get("method") == "initialize":
                    protocol = payload.get("result", {}).get("protocolVersion")
            else:
                payload = {"jsonrpc": "2.0", "id": message["id"], "result": worker_proxy_offline(message)}
        except Exception:
            payload = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "Todo worker transport unavailable"}}
        text = json.dumps(payload)
        if data:
            text = text.replace(data["token"], "[redacted]")
        print(text, flush=True)


def run_agent(cfg: dict, run: dict) -> None:
    if run["agent"].get("executor") == "codex-app":
        run_codex_app(cfg, run)
        return
    if run["agent"].get("executor") == "codex-herdr":
        run_herdr(cfg, run)
        return
    mcp_config = STATE / f"mcp-{run['id']}.json"
    path = None
    try:
        repo = repo_for(cfg, run)
        path = workspace(run, repo)
        STATE.mkdir(parents=True, exist_ok=True)
        write_private_json(mcp_config, {"mcpServers": {"todo": {
            "type": "http", "url": cfg["url"] + run["mcp_path"],
            "headers": {"Authorization": f"Bearer {run['token']}"}}}})
        report(cfg, run["id"], status="running", branch=run.get("branch") if run["kind"] == "task" and repo else None,
               output=f"[{'worktree' if repo else 'scratch folder'} {path}]\n")
        command = " ".join(shlex.quote(a) for a in claude_command(cfg, run, mcp_config))
        proc = subprocess.Popen(
            [user_shell(), "-lc", command], cwd=path, env=environment(cfg, run), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
        )
        buffer: list = []
        meta: dict = {}
        cancelled = threading.Event()
        done = threading.Event()

        def send() -> None:
            with _lock:
                chunk, buffer[:] = "".join(buffer), []
                fields = {k: meta.pop(k) for k in list(meta)}
            if chunk or fields:
                if report(cfg, run["id"], output=chunk or None, **fields) == "cancelled" and not cancelled.is_set():
                    cancelled.set()
                    proc.terminate()

        def flush() -> None:
            while not done.wait(2):
                send()

        threading.Thread(target=flush, daemon=True).start()
        assert proc.stdout
        last = ""
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                text, session, usage = ANSI.sub("", line), None, None
            else:
                text, session, usage = readable(event) if isinstance(event, dict) else ("", None, None)
            with _lock:
                if text:
                    buffer.append(text)
                    last = text.strip().splitlines()[-1] if text.strip() else last
                if session:
                    meta["session_id"] = session
                if usage:
                    meta.update(usage)
        code = proc.wait()
        done.set()
        send()
        if cancelled.is_set():
            notify(f"{run['label']} stopped", "Stopped from the todo app")
            return
        attached = attach_outputs_to_task(cfg, run, path)
        report(cfg, run["id"], status="succeeded" if code == 0 else "failed", exit_code=code, output=attached or None)
        notify(f"{run['label']} {'finished' if code == 0 else f'failed ({code})'}", last, failed=code != 0)
    finally:
        mcp_config.unlink(missing_ok=True)
        if run["kind"] == "check" and path:
            if repo_for(cfg, run):
                subprocess.run(["git", "-C", repo_for(cfg, run), "worktree", "remove", "--force", path], capture_output=True)
            else:
                shutil.rmtree(path, ignore_errors=True)


def handle(cfg: dict, run: dict, reserved: bool = False) -> None:
    log(f"run {run['id']}: {run['label']} ({run.get('kind', 'tool')})")
    global _task_runs
    agent_run = run.get("kind") in ("task", "check")
    if agent_run and not reserved:
        with _lock:
            _task_runs += 1
    try:
        if agent_run:
            describe_agent(cfg, run)
        if fingerprint(run) not in approvals():
            decision = ask(cfg, run)
            if decision == "deny":
                log(f"run {run['id']}: declined")
                report(cfg, run["id"], status="declined", error="Not approved on " + cfg["name"])
                return
            if decision == "always":
                remember(run)
        if agent_run:
            run_agent(cfg, run)
        else:
            (run_terminal if run["mode"] == "terminal" else run_headless)(cfg, run)
    except Exception as exc:  # noqa: BLE001
        message = task_error(exc, run)
        log(f"run {run['id']}: {message}")
        report(cfg, run["id"], status="failed", error=message)
    finally:
        if agent_run:
            with _lock:
                _task_runs -= 1


def reconcile_herdr(cfg: dict, run_id: int) -> None:
    """Explicit operator recovery after closing the uncertain agent, never a launch retry."""
    import fcntl
    namespace = hashlib.sha256(cfg["url"].encode()).hexdigest()[:12]
    folder = STATE / "herdr" / namespace
    receipt = folder / f"run-{run_id}.json"
    record = receipt_data(json.loads(receipt.read_text()))
    with (folder / f"task-{record['run']['task_id']}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handoff_path(receipt).unlink(missing_ok=True)
        save_receipt(receipt, record)
        if record["status"] not in ("needs_inspection", "cancel_pending"):
            raise RuntimeError("Only an uncertain receipt can be reconciled")
        if record.get("route") == "codex-app":
            client = CodexAppServer(cfg)
            try:
                app_connected(client, record)
                if not record.get("thread_id") or not record.get("turn_id"):
                    raise RuntimeError("Native launch lacks a complete identity; inspect manually, automatic reconciliation refused")
                app_owned_thread(client, record)
                if app_turn(client, record)["status"] == "inProgress":
                    raise RuntimeError("Recorded native turn is still active; interrupt it yourself before reconciliation")
            finally:
                client.close()
        elif not record.get("pane_id"):
            raise RuntimeError("Launch returned no pane identity; inspect the selected workspace manually. Receipt cannot be cleared automatically")
        if record.get("route") != "codex-app":
            agents = herdr_call(cfg, record["session"], "agent", "list")["agents"]
            if any(a.get("name") == record["agent_name"] or a.get("pane_id") == record["pane_id"] for a in agents):
                raise RuntimeError("The recorded agent or pane still hosts an agent; close it yourself before reconciliation")
        public = launch_public(record)
        public.update(status="cancelled", error="Operator reconciled after verifying no live agent in the recorded pane")
        api(cfg, f"/api/agent/runs/{run_id}", {"name": cfg["name"], "status": "failed", "launch": public,
                                            "error": public["error"]})
        record.update(status="cancelled", error=public["error"])
        save_receipt(receipt, record)


def recover_herdr(cfg: dict) -> None:
    """Restart observation, never repeat start/prompt after a daemon restart."""
    import fcntl
    global _task_runs
    namespace = hashlib.sha256(cfg["url"].encode()).hexdigest()[:12]
    folder = STATE / "herdr" / namespace
    for receipt in receipt_files(folder):
        record = receipt_data(json.loads(receipt.read_text()))
        task_lock = (folder / f"task-{record['run']['task_id']}.lock").open("a")
        try:
            fcntl.flock(task_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            task_lock.close()  # another daemon owns it; don't delete its handoff
            continue
        handoff_path(receipt).unlink(missing_ok=True)
        save_receipt(receipt, record)  # scrub any legacy receipt token
        if record["status"] in ("finished", "cancelled", "needs_inspection"):
            task_lock.close()
            continue
        if record["status"] not in ("submitted", "working", "blocked", "unknown", "idle", "done"):
            record.update(status="needs_inspection", error="Daemon stopped during launch; inspect before retrying")
            save_receipt(receipt, record)
            report(cfg, record["run"]["id"], status="failed", error=record["error"], launch=launch_public(record))
            task_lock.close()
            continue
        with _lock:
            _task_runs += 1

        def observe(record=record, receipt=receipt, task_lock=task_lock):
            global _task_runs
            try:
                if record.get("route") == "codex-app":
                    monitor_codex_app(cfg, record, receipt)
                else:
                    monitor_herdr(cfg, record, receipt)
            except Exception as exc:
                record.update(status="needs_inspection", error=str(exc))
                save_receipt(receipt, record)
                report(cfg, record["run"]["id"], status="failed", error=str(exc), launch=launch_public(record))
            finally:
                task_lock.close()
                with _lock:
                    _task_runs -= 1
        threading.Thread(target=observe, daemon=True).start()


def loop() -> None:
    global _task_runs
    cfg = load_config()
    log(f"todo-agent {VERSION} as {cfg['name']!r} → {cfg['url']}")
    recover_herdr(cfg)
    backoff = 2
    body = {"name": cfg["name"], "platform": "macos" if MAC else "linux", "version": VERSION}
    while True:
        body["accept_tasks"] = _task_runs < int(cfg.get("max_task_runs", 2))
        try:
            run = api(cfg, "/api/agent/poll", body, timeout=45).get("run")
            backoff = 2
        except urllib.error.HTTPError as exc:
            log(f"server said {exc.code}: {'wrong token?' if exc.code == 401 else exc.reason}")
            time.sleep(60 if exc.code == 401 else backoff)
            continue
        except Exception as exc:  # noqa: BLE001 - offline, NAS rebooting, laptop asleep
            log(f"can't reach {cfg['url']}: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if run:
            reserved = run.get("kind") in ("task", "check")
            if reserved:
                with _lock:
                    _task_runs += 1  # reserve before the next poll, including approval time
            threading.Thread(target=handle, args=(cfg, run, reserved), daemon=True).start()


# ----- install / status -----

def install() -> None:
    load_config()
    me = os.path.abspath(__file__)
    if MAC:
        logs = HOME / "Library" / "Logs" / "todo-agent.log"
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.todo.agent</string>
  <key>ProgramArguments</key><array><string>{sys.executable}</string><string>{me}</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>EnvironmentVariables</key><dict><key>SHELL</key><string>{os.environ.get('SHELL', '/bin/zsh')}</string></dict>
  <key>StandardOutPath</key><string>{logs}</string>
  <key>StandardErrorPath</key><string>{logs}</string>
</dict></plist>
""")
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
        print(f"Installed: starts at login (launchd). Logs: {logs}")
    else:
        UNIT.parent.mkdir(parents=True, exist_ok=True)
        UNIT.write_text(f"""[Unit]
Description=todo-agent (runs customer hub tools from the todo app)
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={sys.executable} {me} run
Restart=always
RestartSec=5

[Install]
WantedBy=graphical-session.target
""")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "todo-agent.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "todo-agent.service"], check=True)
        print("Installed: systemd user service todo-agent. Logs: journalctl --user -u todo-agent -f")


def uninstall() -> None:
    if MAC:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
        PLIST.unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", "todo-agent.service"], capture_output=True)
        UNIT.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print("Removed the login item. Config and approvals are kept in", CONFIG_DIR)


def status() -> None:
    cfg = load_config()
    print(f"todo-agent {VERSION} · {cfg['name']} · {_platform.system()} · {cfg['url']}")
    try:
        api(cfg, "/api/agent/ping", {"name": cfg["name"], "platform": "macos" if MAC else "linux",
                                      "version": VERSION}, timeout=5)
    except urllib.error.HTTPError as exc:
        print(f"server: {exc.code} {'(wrong token)' if exc.code == 401 else exc.reason}")
    except Exception as exc:  # noqa: BLE001
        print(f"server: unreachable ({exc})")
    else:
        print("server: ok")
    print(f"always-allowed commands: {len(approvals())}")


def upload_cli(args: list) -> None:
    cfg = load_config()
    step = note = None
    files = []
    rest = iter(args[1:])
    for a in rest:
        if a == "--step":
            step = next(rest, None)
        elif a == "--note":
            note = next(rest, None)
        else:
            files.append(Path(a).expanduser())
    if not args[0].isdigit() or not files:
        sys.exit("usage: todo-agent upload <meeting id> <file>… [--step <id>] [--note <text>]")
    failed = False
    for f in files:
        try:
            got = upload(cfg, int(args[0]), f, step_id=step, note=note or "")
            print(f"attached {got['name']} ({got['bytes']} bytes): {cfg['url']}{got['url']}")
        except (OSError, urllib.error.URLError) as exc:
            detail = exc.read().decode(errors="replace") if isinstance(exc, urllib.error.HTTPError) else str(exc)
            print(f"couldn't attach {f}: {detail}", file=sys.stderr)
            failed = True
    sys.exit(1 if failed else 0)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        loop()
    elif cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "status":
        status()
    elif cmd == "prompt" and len(sys.argv) == 3:
        prompt(sys.argv[2])
    elif cmd == "approvals":
        for fp, a in approvals().items():
            print(f"{a['approved_at']}  {a['label']}: {a['command']}  ({fp[:10]})")
    elif cmd == "codex-herdr-bootstrap" and len(sys.argv) == 3:
        try:
            codex_herdr_bootstrap(Path(sys.argv[2]))
        except Exception as exc:
            # No exception values, config, token, argv or environment in terminal output.
            print(f"Codex credential handoff failed ({type(exc).__name__}); inspect the private handoff lifecycle", file=sys.stderr)
            sys.exit(1)
    elif cmd == "codex-worker-proxy" and len(sys.argv) == 3:
        try:
            codex_worker_proxy(Path(sys.argv[2]))
        except Exception:
            print("Todo worker bridge unavailable", file=sys.stderr)
            sys.exit(1)
    elif cmd == "herdr-reconcile" and len(sys.argv) == 3 and sys.argv[2].isdigit():
        reconcile_herdr(load_config(), int(sys.argv[2]))
        print("Receipt reconciled; a new run may now be requested in Todo")
    elif cmd == "upload" and len(sys.argv) >= 4:
        upload_cli(sys.argv[2:])
    elif cmd == "forget":
        APPROVALS.unlink(missing_ok=True)
        print("Forgot all approvals; every command will ask again.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
