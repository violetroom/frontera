"""Draft, validate and create tickets as GitHub issues.

One validated path for every ticket (``frontera ticket new``, Hermes): check the
fields against the config, render the body in the issue form's shape, and create
an unlabeled issue with ``gh issue create``.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field

from .config import Config, Project
from .issueform import render_body
from .models import Kind, Risk
from .scan import scan_text

KINDS = tuple(k.value for k in Kind)
RISKS = tuple(r.value for r in Risk)


class TicketError(ValueError):
    pass


@dataclass
class TicketDraft:
    title: str
    kind: str
    tier: int
    risk: str
    description: str = ""
    criteria: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)


def render(draft: TicketDraft) -> str:
    """The issue body, in the same shape the issue form produces."""
    return render_body(draft.kind, draft.tier, draft.description, draft.criteria,
                       draft.context, draft.allowed, draft.risk)


def _allowed_matches_protected(allowed: str, protected: str) -> bool:
    """True when the allowed path could touch files the protected path covers."""
    a = allowed.strip().strip("/")
    p = protected.strip().strip("/")
    if not a or a in (".", "*", "**"):
        return True
    if a == p:
        return True
    # fnmatch's * crosses /, so this covers "allowed under protected" and "protected under allowed" globs.
    if fnmatch.fnmatch(a, p) or fnmatch.fnmatch(p, a):
        return True
    return p.startswith(a + "/") or a.startswith(p + "/")


def validate_draft(draft: TicketDraft, cfg: Config, project: Project) -> list[str]:
    """All problems with the draft, in a stable order; an empty list means well-formed."""
    problems: list[str] = []
    if not draft.title.strip():
        problems.append("title must not be empty")
    if draft.kind not in KINDS:
        problems.append(f"kind must be one of {', '.join(KINDS)}")
    if draft.tier not in cfg.tiers:
        problems.append(f"tier must be one of {list(cfg.tiers)}")
    if draft.risk not in RISKS:
        problems.append(f"risk must be one of {', '.join(RISKS)}")
    if not draft.criteria:
        problems.append("needs at least one acceptance criterion")
    if draft.kind == Kind.CODE.value and not draft.allowed:
        problems.append("code tickets need at least one allowed path")
    for path in draft.allowed:
        for protected in project.protected_paths:
            if _allowed_matches_protected(path, protected):
                problems.append(f"allowed path {path} matches protected path {protected}")
                break
    root = project.repo.resolve()
    for name in draft.context:
        p = (root / name).resolve()
        if not p.exists():
            problems.append(f"context file {name} does not exist in the local checkout")
        elif not p.is_relative_to(root):
            problems.append(f"context file {name} is not inside the local checkout")
    for finding in scan_text(draft.title + "\n" + render(draft), "ticket", cfg.private_terms):
        problems.append(f"privacy/secret scan: {finding}")
    return problems


def create_issue(repo: str, title: str, body: str, runner=subprocess.run) -> str:
    """Create an unlabeled issue with ``gh issue create``; return the issue URL."""
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    proc = runner(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TicketError(f"gh issue create: {(proc.stderr or proc.stdout).strip()}")
    lines = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    if not lines:
        raise TicketError("gh issue create printed no issue URL")
    return lines[-1]
