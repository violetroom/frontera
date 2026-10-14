from __future__ import annotations

from typing import Protocol

from ..models import Status, Ticket


class TrackerError(RuntimeError):
    pass


class Tracker(Protocol):
    """Read side of a work tracker. Write methods (transition, comment, fields) come with step 3."""

    def get(self, key: str) -> Ticket: ...

    def list(self, project: str | None = None, statuses: set[Status] | None = None) -> list[Ticket]: ...
