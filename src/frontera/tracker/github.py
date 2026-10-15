"""GitHub Issues as the tracker: labels carry kind/tier/state, a bot comment carries counters.

See docs/GITHUB.md. Only comments by ``bot_logins`` are read as state: the
repos are public and anyone can comment.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import quote

from ..gh import Gh
from ..issueform import parse_list, parse_sections
from ..models import Kind, Risk, Status, Ticket
from .base import TrackerError

STATE_LABELS: dict[Status, str] = {
    Status.READY: "state:ready",
    Status.AGENT_WORKING: "state:working",
    Status.AWAITING_COMMAND: "state:awaiting-command",
    Status.IN_REVIEW: "state:in-review",
    Status.CHANGES_REQUESTED: "state:changes-requested",
    Status.NEEDS_DECISION: "state:needs-decision",
    Status.AGENT_FAILED: "state:failed",
}
LABEL_STATES = {v: k for k, v in STATE_LABELS.items()}
MARKER = "frontera-state"
_META_RE = re.compile(r"<!--\s*" + MARKER + r"\s+(\{.*?\})\s*-->", re.S)


@dataclass
class Meta:
    """Counters kept in the bot's state comment."""
    attempts: int = 0
    promotions: int = 0
    last_endpoint: str | None = None
    pr_number: int | None = None
    branch: str | None = None
    execution_id: str | None = None
    gate_failures: int = 0
    handoff_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Meta":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


def _label_names(issue: dict) -> list[str]:
    return [l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])]


def ticket_from_issue(issue: dict, project: str, meta: Meta) -> Ticket:
    labels = _label_names(issue)
    sec = parse_sections(issue.get("body") or "")

    def prefixed(prefix: str) -> list[str]:
        return [l[len(prefix):] for l in labels if l.startswith(prefix)]

    try:
        kind = Kind((prefixed("kind:") or [sec.get("kind", "")])[0].strip().lower())
    except ValueError:
        raise TrackerError(f"#{issue['number']}: needs a kind:code or kind:research label") from None
    try:
        tier = int(sec.get("tier") or (prefixed("tier:") or ["1"])[0])
        current = int((prefixed("tier:") or [tier])[0])
    except ValueError:
        raise TrackerError(f"#{issue['number']}: tier must be a number") from None
    risk_text = (prefixed("risk:") or [sec.get("risk") or "low"])[0].strip().lower()
    states = [LABEL_STATES[l] for l in labels if l in LABEL_STATES]
    if issue.get("state") == "closed":
        status = Status.DONE
    elif len(states) > 1:
        raise TrackerError(f"#{issue['number']}: has more than one state: label")
    else:
        status = states[0] if states else Status.BACKLOG

    return Ticket(
        key=f"{project}-{issue['number']}",
        number=issue["number"],
        project=project,
        summary=issue.get("title", ""),
        description=sec.get("description", ""),
        status=status,
        kind=kind,
        tier=tier,
        current_tier=current,
        risk=Risk(risk_text) if risk_text in {r.value for r in Risk} else Risk.LOW,
        agent_eligible="human-only" not in labels,
        acceptance_criteria=parse_list(sec.get("acceptance criteria", "")),
        context_pack=parse_list(sec.get("context files", "")),
        allowed_paths=parse_list(sec.get("allowed paths", "")),
        pin_endpoint=(prefixed("pin:") or [None])[0],
        review_verdict=(prefixed("review:") or [None])[0],
        attempts=meta.attempts,
        promotions=meta.promotions,
        last_endpoint=meta.last_endpoint,
        pr_number=meta.pr_number,
        handoff_notes=list(meta.handoff_notes),
    )


class GitHubTracker:
    def __init__(self, gh: Gh, repo: str, project: str, bot_logins: tuple[str, ...] = ("github-actions[bot]",)):
        self.gh = gh
        self.repo = repo
        self.project = project
        self.bot_logins = set(bot_logins)

    # ---- reads -----------------------------------------------------------------
    def number(self, key: str | int) -> int:
        if isinstance(key, int):
            return key
        m = re.fullmatch(rf"(?:{re.escape(self.project)}-|#)?(\d+)", key)
        if not m:
            raise TrackerError(f"{key!r} is not a {self.project} issue key")
        return int(m.group(1))

    def issue(self, key: str | int) -> dict:
        issue = self.gh.api("GET", f"repos/{self.repo}/issues/{self.number(key)}")
        if issue.get("pull_request"):
            raise TrackerError(f"#{issue['number']} is a pull request, not a ticket")
        return issue

    def _state_comment(self, number: int) -> tuple[int | None, Meta]:
        comments = self.gh.api("GET", f"repos/{self.repo}/issues/{number}/comments?per_page=100") or []
        for c in reversed(comments):
            if c.get("user", {}).get("login") not in self.bot_logins:
                continue
            m = _META_RE.search(c.get("body") or "")
            if m:
                return c["id"], Meta.from_dict(json.loads(m.group(1)))
        return None, Meta()

    def meta(self, key: str | int) -> Meta:
        return self._state_comment(self.number(key))[1]

    def get(self, key: str | int) -> Ticket:
        issue = self.issue(key)
        return ticket_from_issue(issue, self.project, self.meta(issue["number"]))

    def list(self, project: str | None = None, statuses: set[Status] | None = None) -> list[Ticket]:
        if project not in (None, self.project):
            return []
        wanted = statuses or set(STATE_LABELS)
        tickets: dict[int, Ticket] = {}
        for status in wanted:
            label = STATE_LABELS.get(status)
            if label is None:
                continue
            issues = self.gh.api("GET", f"repos/{self.repo}/issues?state=open&per_page=100&labels={quote(label)}") or []
            for issue in issues:
                if issue.get("pull_request") or issue["number"] in tickets:
                    continue
                try:
                    tickets[issue["number"]] = ticket_from_issue(issue, self.project, self.meta(issue["number"]))
                except TrackerError:
                    continue  # malformed tickets are reported when run directly
        return [tickets[n] for n in sorted(tickets)]

    def active_counts(self) -> dict[str, int]:
        issues = self.gh.api("GET", f"repos/{self.repo}/issues?state=open&per_page=100&labels={quote(STATE_LABELS[Status.AGENT_WORKING])}") or []
        counts: dict[str, int] = {}
        for issue in issues:
            for label in _label_names(issue):
                if label.startswith("on:"):
                    counts[label[3:]] = counts.get(label[3:], 0) + 1
        return counts

    # ---- writes ----------------------------------------------------------------
    def _remove_label(self, number: int, label: str) -> None:
        try:
            self.gh.api("DELETE", f"repos/{self.repo}/issues/{number}/labels/{quote(label, safe='')}")
        except Exception:
            pass  # already absent

    def _replace_prefixed(self, number: int, prefix: str, new: str | None, current: list[str]) -> None:
        for label in current:
            if label.startswith(prefix) and label != new:
                self._remove_label(number, label)
        if new and new not in current:
            self.gh.api("POST", f"repos/{self.repo}/issues/{number}/labels", {"labels": [new]})

    def set_status(self, ticket: Ticket, status: Status, endpoint: str | None = None) -> None:
        current = _label_names(self.issue(ticket.number))
        self._replace_prefixed(ticket.number, "state:", STATE_LABELS.get(status), current)
        self._replace_prefixed(ticket.number, "on:", f"on:{endpoint}" if endpoint else None, current)
        ticket.status = status

    def set_tier(self, ticket: Ticket, tier: int) -> None:
        current = _label_names(self.issue(ticket.number))
        self._replace_prefixed(ticket.number, "tier:", f"tier:{tier}", current)
        ticket.current_tier = tier

    def set_review(self, ticket: Ticket, verdict: str) -> None:
        current = _label_names(self.issue(ticket.number))
        self._replace_prefixed(ticket.number, "review:", f"review:{verdict}", current)
        ticket.review_verdict = verdict

    def comment(self, ticket: Ticket, body: str) -> None:
        self.gh.api("POST", f"repos/{self.repo}/issues/{ticket.number}/comments", {"body": body})

    def save_meta(self, ticket: Ticket, meta: Meta, summary: str) -> None:
        body = f"<!-- {MARKER} {json.dumps(asdict(meta))} -->\n**Frontera status**\n\n{summary}\n"
        comment_id, _ = self._state_comment(ticket.number)
        if comment_id is None:
            self.gh.api("POST", f"repos/{self.repo}/issues/{ticket.number}/comments", {"body": body})
        else:
            self.gh.api("PATCH", f"repos/{self.repo}/issues/comments/{comment_id}", {"body": body})

    def permission(self, login: str) -> str:
        try:
            r = self.gh.api("GET", f"repos/{self.repo}/collaborators/{quote(login, safe='')}/permission")
        except Exception:
            return "none"
        r = r or {}
        return r.get("role_name") or r.get("permission") or "none"
