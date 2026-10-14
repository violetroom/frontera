"""Entry point inside GitHub Actions: authorize the event, then run, review, fix or promote.

Only events caused by an account with admin or maintain permission on the repo
do anything. Strangers on a public repo get no response at all.
"""

from __future__ import annotations

import base64
import json
import re
import os
from pathlib import Path

from .commands import CommandError, is_owner, parse_command
from .config import Config
from .gh import Gh
from .models import Status
from .runner import Ctx, drain, fix_ticket, promote_ticket, review_ticket, run_ticket
from .tracker import make_tracker
from .tracker.github import GitHubTracker, STATE_LABELS
from .verify import git

DRAIN_LIMIT = 3
READY_LABEL = STATE_LABELS[Status.READY]


class GitHubForge:
    def __init__(self, gh: Gh, repo: str, workdir: Path, token: str):
        self.gh, self.repo, self.workdir, self.token = gh, repo, workdir, token

    def push(self, branch: str) -> None:
        auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        git(self.workdir, "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}",
            "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    def open_pr(self, title: str, body: str, head: str, base: str) -> int:
        pr = self.gh.api("POST", f"repos/{self.repo}/pulls",
                         {"title": title, "body": body, "head": head, "base": base, "draft": True})
        return pr["number"]

    def close_pr(self, number: int, comment: str) -> None:
        self.gh.api("POST", f"repos/{self.repo}/issues/{number}/comments", {"body": comment})
        self.gh.api("PATCH", f"repos/{self.repo}/pulls/{number}", {"state": "closed"})

    def review_pr(self, number: int, body: str) -> None:
        self.gh.api("POST", f"repos/{self.repo}/pulls/{number}/reviews", {"body": body, "event": "COMMENT"})


def ticket_number_for_pr(tracker: GitHubTracker, pr_number: int) -> int | None:
    """The ticket an agent PR belongs to, from its same-repo branch agent/<PROJECT>-<n>-..."""
    pr = tracker.gh.api("GET", f"repos/{tracker.repo}/pulls/{pr_number}")
    head = pr.get("head") or {}
    if (head.get("repo") or {}).get("full_name") != tracker.repo:
        return None  # forks never map to tickets
    m = re.match(rf"agent/{re.escape(tracker.project)}-(\d+)-", head.get("ref", ""))
    return int(m.group(1)) if m else None


def _owner_command(text: str, url: str | None) -> str:
    quoted = " ".join(text.split())[:500]
    return f'"{quoted}"' + (f" ({url})" if url else "")


def handle(cfg: Config, event_name: str, event: dict, ctx: Ctx) -> str:
    tracker: GitHubTracker = ctx.tracker
    actor = (event.get("sender") or {}).get("login", "")

    if event_name == "issues":
        if event.get("action") != "labeled" or (event.get("label") or {}).get("name") != READY_LABEL:
            return "ignored: not a state:ready label"
        if not is_owner(tracker.permission(actor)):
            return f"ignored: {actor} is not an owner"
        issue = event["issue"]
        result = run_ticket(ctx, tracker.get(issue["number"]))
        drained = drain(ctx, DRAIN_LIMIT, skip={issue["number"]})
        return "; ".join([f"#{issue['number']}: {result}", *drained])

    if event_name == "issue_comment":
        if event.get("action") != "created":
            return "ignored: not a new comment"
        comment = event["comment"]
        try:
            cmd = parse_command(comment.get("body", ""))
        except CommandError as e:
            if is_owner(tracker.permission(comment["user"]["login"])):
                tracker.gh.api("POST", f"repos/{tracker.repo}/issues/{event['issue']['number']}/comments",
                               {"body": f"`{e}`"})
            return f"ignored: {e}"
        if cmd is None:
            return "ignored: no /frontera command"
        author = comment["user"]["login"]
        if not (is_owner(tracker.permission(author)) and author == actor):
            return f"ignored: {author} is not an owner"
        owner = _owner_command(comment["body"], comment.get("html_url"))
        number = event["issue"]["number"]
        if event["issue"].get("pull_request"):
            number = ticket_number_for_pr(tracker, number)
            if number is None:
                tracker.gh.api("POST", f"repos/{tracker.repo}/issues/{event['issue']['number']}/comments",
                               {"body": "Frontera commands on a PR work only on agent PRs (branch `agent/...`)."})
                return "ignored: not an agent PR"
        ticket = tracker.get(number)
        if cmd.verb == "run":
            return run_ticket(ctx, ticket, owner, cmd.endpoint)
        if cmd.verb == "review":
            return review_ticket(ctx, ticket, owner)
        if cmd.verb == "fix":
            return fix_ticket(ctx, ticket, owner)
        return promote_ticket(ctx, ticket, owner)

    if event_name == "workflow_dispatch":
        if not is_owner(tracker.permission(actor)):
            return f"ignored: {actor} is not an owner"
        inputs = event.get("inputs") or {}
        command, note = inputs.get("command", ""), inputs.get("note", "")
        owner = _owner_command(f"workflow_dispatch {command} by @{actor}: {note}", None)
        out = []
        if command == "run-awaiting":
            per_endpoint: dict[str, int] = {}
            for t in tracker.list(statuses={Status.AWAITING_COMMAND}):
                lane = cfg.lanes.get(t.lane)
                ep = cfg.endpoints.get(lane.endpoints[0]) if lane else None
                cap = (ep.max_runs_per_command if ep else None) or 3
                if ep and per_endpoint.get(ep.name, 0) >= cap:
                    continue
                r = run_ticket(ctx, t, owner)
                out.append(f"{t.key}: {r}")
                if ep:
                    per_endpoint[ep.name] = per_endpoint.get(ep.name, 0) + 1
                if r == "transient":
                    break  # usage limit or outage: leave the rest waiting
        elif command == "review-pending":
            for t in tracker.list(statuses={Status.IN_REVIEW}):
                if t.review_verdict is None:
                    out.append(f"{t.key}: {review_ticket(ctx, t, owner)}")
        elif command == "drain":
            out = drain(ctx, DRAIN_LIMIT)
        else:
            return f"ignored: unknown command {command!r}"
        return "; ".join(out) or "nothing to do"

    return f"ignored: event {event_name}"


def main_action(cfg: Config, event_name: str, event_path: str) -> int:
    event = json.loads(Path(event_path).read_text())
    tracker = make_tracker(cfg)
    if not isinstance(tracker, GitHubTracker):
        raise SystemExit("frontera action needs tracker.kind github")
    project = cfg.projects[tracker.project]
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    ctx = Ctx(cfg, project, tracker, GitHubForge(tracker.gh, tracker.repo, project.repo, token), run_url)
    print(handle(cfg, event_name, event, ctx))
    return 0
