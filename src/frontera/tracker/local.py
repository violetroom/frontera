"""A tracker backed by one JSON file per ticket, for tests and pre-Jira use."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import Kind, Risk, Status, Ticket
from .base import TrackerError

_FIELDS = {
    "key", "project", "summary", "status", "kind", "tier", "description", "current_tier",
    "promotions", "attempts", "agent_eligible", "risk", "acceptance_criteria", "context_pack",
    "allowed_paths", "pin_endpoint", "last_endpoint", "review_verdict", "handoff_notes",
}


def ticket_from_dict(d: dict[str, Any]) -> Ticket:
    unknown = set(d) - _FIELDS
    if unknown:
        raise TrackerError(f"{d.get('key', '?')}: unknown fields {sorted(unknown)}")
    try:
        d = dict(d)
        d["status"] = Status(d["status"])
        d["kind"] = Kind(d["kind"])
        if "risk" in d:
            d["risk"] = Risk(d["risk"])
        return Ticket(**d)
    except (KeyError, ValueError, TypeError) as e:
        raise TrackerError(f"{d.get('key', '?')}: invalid ticket: {e}") from None


class LocalTracker:
    def __init__(self, root: Path):
        self.root = root

    def get(self, key: str) -> Ticket:
        path = self.root / f"{key}.json"
        if not path.exists():
            raise TrackerError(f"no ticket {key} in {self.root}")
        return ticket_from_dict(json.loads(path.read_text()))

    def list(self, project: str | None = None, statuses: set[Status] | None = None) -> list[Ticket]:
        if not self.root.is_dir():
            raise TrackerError(f"tracker directory {self.root} does not exist")
        tickets = [ticket_from_dict(json.loads(p.read_text())) for p in sorted(self.root.glob("*.json"))]
        return [
            t for t in tickets
            if (project is None or t.project == project) and (statuses is None or t.status in statuses)
        ]
