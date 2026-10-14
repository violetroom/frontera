"""Tracker adapters. Jira arrives in a later step; ``local`` reads JSON files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tracker, TrackerError
from .local import LocalTracker


def make_tracker(spec: dict[str, Any]) -> Tracker:
    kind = spec.get("kind", "local")
    if kind == "local":
        path = spec.get("path")
        if not path:
            raise TrackerError("tracker.kind local needs tracker.path")
        return LocalTracker(Path(path).expanduser())
    raise TrackerError(f"tracker kind {kind!r} is not implemented yet")


__all__ = ["Tracker", "TrackerError", "LocalTracker", "make_tracker"]
