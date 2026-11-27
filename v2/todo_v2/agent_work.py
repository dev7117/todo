"""Read-only GitHub projection. No database access; each poll publishes one snapshot."""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: object = None


class NoRedirects(HTTPRedirectHandler):
    # Never forward the server credential to a redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_get(url: str, headers: dict[str, str]) -> Response:
    request = Request(url, headers=headers, method="GET")
    try:
        with build_opener(NoRedirects).open(request, timeout=10) as result:
            return Response(result.status, dict(result.headers), json.load(result))
    except HTTPError as error:
        # Error bodies can contain sensitive request details; never read or log them.
        return Response(error.code, dict(error.headers))


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value: str | None) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0


def linked_issues(body: str, repo: str) -> list[int]:
    """Only explicit same-repo references; a bare number after a keyword is local."""
    pattern = r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs|part\s+of)\s*:?\s+(?:(?P<repo>[\w.-]+/[\w.-]+)#|https://github\.com/(?P<urlrepo>[\w.-]+/[\w.-]+)/issues/|#)(?P<number>\d+)\b"
    return list(dict.fromkeys(
        int(match["number"]) for match in re.finditer(pattern, body, re.I)
        if (match["repo"] or match["urlrepo"] or repo).lower() == repo.lower()
    ))


def ci_state(runs: list[dict], sha: str) -> str:
    latest: dict[object, dict] = {}
    for run in runs:
        path = (run.get("path") or "").split("@", 1)[0]
        if run.get("head_sha") != sha or path.rsplit("/", 1)[-1] == "claude.yml":
            continue
        key = run.get("workflow_id", path)
        order = lambda r: (r.get("run_number", 0), r.get("run_attempt", 1), r.get("id", 0))
        if key not in latest or order(run) > order(latest[key]):
            latest[key] = run
    if not latest:
        return "absent"
    if any(r.get("conclusion") in {"failure", "timed_out", "cancelled", "action_required", "startup_failure"} for r in latest.values()):
        return "failing"
    if any(r.get("status") != "completed" for r in latest.values()):
        return "running"
    if all(r.get("conclusion") in {"success", "neutral", "skipped"} for r in latest.values()):
        return "green"
    return "unknown"


def approved(reviews: list[dict]) -> bool:
    latest: dict[str, str] = {}
    for review in sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r.get("id", 0))):
        # Comments don't revoke a review; dismissed approvals do.
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["login"]] = review["state"]
    return "APPROVED" in latest.values() and "CHANGES_REQUESTED" not in latest.values()


def project(repo: str, raw: dict, reviews: list[dict] | None = None, runs: list[dict] | None = None) -> dict:
    is_pr = "head" in raw
    ci = ci_state(runs or [], raw["head"]["sha"]) if is_pr else "absent"
    approval = approved(reviews or [])
    closed = raw["state"] == "closed"
    if closed:
        state = "merged" if raw.get("merged_at") else "closed"
    elif not is_pr:
        state = "issue open"
    elif raw.get("draft"):
        state = "draft"
    elif ci == "failing":
        state = "CI failing"
    elif ci == "running":
        state = "CI running"
    else:
        state = "approved" if approval else "in review"
    waiting = any(label["name"] == "needs-dev" for label in raw.get("labels", [])) or (
        is_pr and not closed and not raw.get("draft") and approval and ci == "green"
        and raw.get("mergeable") is True and raw.get("mergeable_state") == "clean"
    )
    return {
        "repo": repo, "number": raw["number"], "kind": "pr" if is_pr else "issue",
        "title": raw["title"], "url": f"https://github.com/{repo}/{'pull' if is_pr else 'issues'}/{raw['number']}",
        "author": (raw.get("user") or {}).get("login", "unknown"), "updated_at": raw["updated_at"],
        "closed_at": raw.get("merged_at") or raw.get("closed_at"), "state": state, "ci": ci,
        "waiting_on_dev": bool(waiting), "linked_issues": linked_issues(raw.get("body") or "", repo) if is_pr else [],
        "prs": [],
    }


class PollError(Exception):
    """Only constant, safe messages cross this boundary."""


class AgentWork:
    def __init__(self, cfg: Config, transport: Callable = github_get, clock: Callable = time.time):
        self.cfg, self.transport, self.clock = cfg, transport, clock
        self.configured = bool(cfg.github_token and cfg.github_repos)
        self.last_poll_at: str | None = None
        self.last_updated_at: str | None = None
        self.last_error: str | None = None
        self.next_refresh = 0.0
        self.failures = 0
        self.cache: dict[str, tuple[str | None, object, bool]] = {}
        self.groups: list[dict] = []
        self.recent: list[dict] = []
        self.waiting: list[dict] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None

    def start(self) -> None:
        if self.configured:
            self.worker = threading.Thread(target=self._background, name="github-reader", daemon=True)
            self.worker.start()

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=11)

    def _background(self) -> None:
        while not self.stop.is_set():
            self.refresh()
            self.stop.wait(self.cfg.github_poll_minutes * 60)

    def health(self) -> dict:
        return {"configured": self.configured, "last_poll_at": self.last_poll_at, "last_error": self.last_error}

    def payload(self) -> dict:
        result = {**self.health(), "last_updated_at": self.last_updated_at,
                  "next_refresh_at": iso(self.next_refresh) if self.next_refresh else None,
                  "waiting_on_dev": self.waiting, "repos": self.groups, "recent": self.recent}
        # Only explicit public fields leave this module. Defend even against a remote
        # title echoing the credential; never return credential fragments/redactions.
        def safe(value):
            if isinstance(value, str) and self.cfg.github_token:
                return value.replace(self.cfg.github_token, "[removed]")
            if isinstance(value, dict):
                return {key: safe(val) for key, val in value.items()}
            if isinstance(value, list):
                return [safe(val) for val in value]
            return value
        return safe(result)

    def _backoff(self, headers: dict[str, str]) -> None:
        now = self.clock()
        delay = min(3600, 60 * 2 ** min(self.failures, 6))
        self.failures += 1
        try:
            reset = float(headers.get("x-ratelimit-reset", "0"))
            delay = max(delay, reset - now)
        except ValueError:
            pass
        retry = headers.get("retry-after", "0")
        try:
            delay = max(delay, float(retry))
        except ValueError:
            try:
                delay = max(delay, parsedate_to_datetime(retry).timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
        self.next_refresh = max(self.next_refresh, now + delay)

    def _get(self, path: str, touched: dict) -> tuple[object, bool]:
        if self.stop.is_set():
            raise PollError("GitHub refresh interrupted.")
        cached = self.cache.get(path)
        headers = {"Authorization": f"Bearer {self.cfg.github_token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Todo-v2"}
        if cached and cached[0]:
            headers["If-None-Match"] = cached[0]
        result = self.transport(self.cfg.github_api_base + path, headers)
        response_headers = {key.lower(): value for key, value in result.headers.items()}
        if result.status in {403, 429}:
            self._backoff(response_headers)
            raise PollError("GitHub access or rate limit reached. Refresh will retry after a cooldown.")
        if result.status not in {200, 304}:
            raise PollError("GitHub could not be read. Check access and try again later.")
        try:
            low = int(response_headers.get("x-ratelimit-remaining", "5000")) <= 10
        except ValueError:
            low = False
        if low:
            self._backoff(response_headers)
            raise PollError("GitHub rate limit is low. Refresh will retry after a cooldown.")
        if result.status == 304:
            if not cached:
                raise PollError("GitHub cache could not be refreshed. Try again later.")
            entry = cached
        else:
            entry = (response_headers.get("etag"), result.body, 'rel="next"' in response_headers.get("link", ""))
        touched[path] = entry
        return copy.deepcopy(entry[1]), entry[2]

    def _pages(self, path: str, touched: dict, key: str | None = None, cutoff: float = 0) -> list[dict]:
        rows = []
        page = 1
        while True:
            body, more = self._get(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}", touched)
            batch = body[key] if key else body
            if not isinstance(batch, list):
                raise PollError("GitHub returned an unexpected response. Try again later.")
            rows.extend(batch)
            if not more or (cutoff and batch and timestamp(batch[-1]["updated_at"]) < cutoff):
                return rows
            page += 1

    def refresh(self) -> None:
        if not self.configured or not self.lock.acquire(blocking=False):
            return
        try:
            now = self.clock()
            if now < self.next_refresh:
                return
            self.last_poll_at, self.next_refresh = iso(now), now + 60
            touched: dict = {}
            groups, recent, waiting = [], [], []
            cutoff = now - 7 * 86400
            for repo in self.cfg.github_repos:
                base = f"/repos/{repo}"
                # Stable URLs let every unchanged page be revalidated with an ETag.
                # Closed pages are ordered by update, stopping once older than our window.
                opened = self._pages(f"{base}/issues?state=open&sort=updated&direction=desc", touched)
                closed = self._pages(f"{base}/issues?state=closed&sort=updated&direction=desc", touched, cutoff=cutoff)
                raw_items = opened + [r for r in closed if timestamp(r.get("closed_at")) >= cutoff]
                active = []
                for raw in raw_items:
                    reviews, runs = [], []
                    if "pull_request" in raw:
                        raw, _ = self._get(f"{base}/pulls/{raw['number']}", touched)
                        if raw["state"] == "open":
                            reviews = self._pages(f"{base}/pulls/{raw['number']}/reviews", touched)
                            runs = self._pages(f"{base}/actions/runs?{urlencode({'head_sha': raw['head']['sha']})}", touched, "workflow_runs")
                    item = project(repo, raw, reviews, runs)
                    if item["state"] in {"merged", "closed"}:
                        if timestamp(item["closed_at"]) >= cutoff:
                            recent.append(item)
                    else:
                        active.append(item)
                issues = {i["number"]: i for i in active if i["kind"] == "issue"}
                roots = list(issues.values())
                for pr in (i for i in active if i["kind"] == "pr"):
                    parents = [issues[n] for n in pr["linked_issues"] if n in issues]
                    for parent in parents:
                        parent["prs"].append(pr)
                        parent["state"] = "in progress"
                    if not parents:
                        roots.append(pr)
                waiting.extend({**i, "prs": []} for i in active if i["waiting_on_dev"])
                groups.append({"repo": repo, "items": sorted(roots, key=lambda i: i["updated_at"], reverse=True)})
            self.groups, self.recent, self.waiting = groups, sorted(recent, key=lambda i: i["closed_at"], reverse=True), waiting
            self.cache = touched  # discard obsolete heads and closed pages
            self.last_updated_at, self.last_error, self.failures = iso(now), None, 0
        except PollError as error:
            self.last_error = str(error)
        except Exception:
            # Never expose/log transport exceptions, URLs, headers, bodies or tokens.
            self.last_error = "GitHub refresh failed. Check access and try again later."
            self._backoff({})
        finally:
            self.lock.release()
