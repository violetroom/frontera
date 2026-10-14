"""Deterministic checks before any model is invoked. A run proceeds only with no problems."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import Config, Project
from .models import Kind, Risk, Status, Ticket
from .routing import Route


@dataclass(frozen=True)
class Problem:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def default_allowed_paths(ticket: Ticket, project: Project) -> list[str]:
    if ticket.allowed_paths:
        return list(ticket.allowed_paths)
    if ticket.kind == Kind.RESEARCH:
        return [f"{project.research_dir}/{ticket.key}.md"]
    return []


def safe_repo_path(repo: Path, rel: str) -> Path | None:
    """Resolve rel inside repo, or None if it is absolute or escapes the repo."""
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:
        return None
    full = (repo / rel).resolve()
    return full if full.is_relative_to(repo.resolve()) else None


def preflight(ticket: Ticket, cfg: Config, rt: Route, owner_command: str | None) -> list[Problem]:
    problems: list[Problem] = []
    add = lambda code, msg: problems.append(Problem(code, msg))  # noqa: E731

    project = cfg.projects.get(ticket.project)
    if project is None:
        add("PROJECT_NOT_ALLOWED", f"project {ticket.project} is not in the config allowlist")
    elif not (project.repo / ".git").exists():
        add("REPO_MISSING", f"{ticket.project} repo is not a git checkout at the configured path")

    if ticket.status == Status.AWAITING_COMMAND:
        if not owner_command:
            add("NEEDS_COMMAND", "ticket is Awaiting Command; it runs only when you ask for it")
    elif ticket.status != Status.READY:
        add("NOT_READY", f"status is {ticket.status}, not Ready")

    if not ticket.agent_eligible:
        add("NOT_ELIGIBLE", "Agent Eligible is not set")
    if not [c for c in ticket.acceptance_criteria if c.strip()]:
        add("NO_CRITERIA", "no acceptance criteria")
    if ticket.risk == Risk.HIGH and not owner_command:
        add("HIGH_RISK", "high-risk tickets run only on your explicit command")

    lane = cfg.lanes.get(ticket.lane)
    if lane is not None and ticket.attempts >= lane.attempts:
        add("ATTEMPTS_EXHAUSTED", f"{ticket.attempts} attempts used at {lane.name} (limit {lane.attempts})")

    if not rt.ok:
        add(f"ROUTE_{rt.wait.upper()}", rt.detail or str(rt.wait))

    if project is not None:
        if ticket.kind == Kind.CODE and not ticket.allowed_paths:
            add("NO_ALLOWED_PATHS", "code tickets need Allowed Paths")
        for rel in ticket.context_pack:
            full = safe_repo_path(project.repo, rel)
            if full is None:
                add("CONTEXT_OUTSIDE_REPO", f"context path {rel!r} is absolute or leaves the repo")
            elif not full.exists():
                add("CONTEXT_MISSING", f"context path {rel!r} does not exist on {project.base_branch} checkout")
        for glob in default_allowed_paths(ticket, project):
            if PurePosixPath(glob).is_absolute() or ".." in PurePosixPath(glob).parts:
                add("ALLOWED_PATH_OUTSIDE_REPO", f"allowed path {glob!r} is absolute or leaves the repo")
    return problems
