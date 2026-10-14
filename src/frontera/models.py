"""Ticket and run data shared by the tracker, routing and dispatcher layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Status(StrEnum):
    BACKLOG = "Backlog"
    READY = "Ready"
    AGENT_WORKING = "Agent Working"
    IN_REVIEW = "In Review"
    AWAITING_COMMAND = "Awaiting Command"
    CHANGES_REQUESTED = "Changes Requested"
    NEEDS_DECISION = "Needs Decision"
    AGENT_FAILED = "Agent Failed"
    DONE = "Done"


class Kind(StrEnum):
    CODE = "code"
    RESEARCH = "research"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Ticket:
    key: str
    project: str
    summary: str
    status: Status
    kind: Kind
    tier: int
    description: str = ""
    current_tier: int | None = None
    promotions: int = 0
    attempts: int = 0  # attempts at the current tier
    agent_eligible: bool = False
    risk: Risk = Risk.LOW
    acceptance_criteria: list[str] = field(default_factory=list)
    context_pack: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    pin_endpoint: str | None = None
    last_endpoint: str | None = None
    review_verdict: str | None = None
    handoff_notes: list[str] = field(default_factory=list)
    number: int | None = None  # tracker issue number, when the tracker has one
    pr_number: int | None = None

    def __post_init__(self) -> None:
        if self.current_tier is None:
            self.current_tier = self.tier

    @property
    def lane(self) -> str:
        return f"{self.kind}/{self.current_tier}"


def execution_id(ticket: Ticket, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{ticket.key}-{now.strftime('%Y%m%dT%H%M%SZ')}-a{ticket.attempts + 1}"
