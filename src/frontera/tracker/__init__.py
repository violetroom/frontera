"""Tracker adapters: ``github`` (Issues) for real use, ``local`` (JSON files) for tests and offline dry runs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .base import Tracker, TrackerError
from .github import GitHubTracker
from .local import LocalTracker

if TYPE_CHECKING:
    from ..config import Config


def make_tracker(cfg: "Config", gh=None) -> Tracker:
    spec = cfg.tracker
    kind = spec.get("kind", "local")
    if kind == "local":
        path = spec.get("path")
        if not path:
            raise TrackerError("tracker.kind local needs tracker.path")
        return LocalTracker(Path(path).expanduser())
    if kind == "github":
        project = spec.get("project") or (next(iter(cfg.projects)) if len(cfg.projects) == 1 else None)
        if project not in cfg.projects or not cfg.projects[project].github_repo:
            raise TrackerError("tracker.kind github needs tracker.project naming a project with github_repo")
        if gh is None:
            from ..gh import GhCli
            gh = GhCli()
        return GitHubTracker(gh, cfg.projects[project].github_repo, project, cfg.bot_logins)
    raise TrackerError(f"tracker kind {kind!r} is not implemented")


__all__ = ["Tracker", "TrackerError", "LocalTracker", "GitHubTracker", "make_tracker"]
