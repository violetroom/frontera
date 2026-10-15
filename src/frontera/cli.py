"""Command-line entry point. Step 2 provides config checks, routing views and dry runs."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import ticketing
from .config import ConfigError, load_config
from .contextpack import build_pack, write_pack
from .models import Status, execution_id
from .preflight import preflight
from .routing import route, route_review
from .state import EndpointState
from .tracker import GitHubTracker, TrackerError, make_tracker

EXIT_BLOCKED = 2


def _owner(cmd: str | None) -> str | None:
    return cmd.strip() if cmd and cmd.strip() else None


def _endpoint_state(cfg, tracker) -> EndpointState:
    if isinstance(tracker, GitHubTracker):
        return EndpointState(active=tracker.active_counts())
    return EndpointState.load(cfg.state_dir)


def cmd_config_check(args, cfg) -> int:
    print("endpoints:")
    for e in cfg.endpoints.values():
        scope = ",".join(sorted(e.projects)) if e.projects is not None else "all projects"
        print(f"  {e.name:<18} {e.harness:<12} {e.launch:<11} {scope}")
    print("lanes:")
    for l in cfg.lanes.values():
        extra = f" -> {l.promote_to}" if l.promote_to else ""
        fb = f" (fallback {', '.join(l.fallback)})" if l.fallback else ""
        print(f"  {l.name:<12} {', '.join(l.endpoints)}{fb}{extra}")
    print("projects:", ", ".join(cfg.projects) or "(none)")
    return 0


def cmd_next(args, cfg) -> int:
    tracker = make_tracker(cfg)
    state = _endpoint_state(cfg, tracker)
    tickets = tracker.list(args.project, {Status.READY, Status.AWAITING_COMMAND})
    if not tickets:
        print("nothing ready")
        return 0
    for t in tickets:
        rt = route(t, cfg, state)
        where = rt.endpoint.name if rt.ok else f"waits: {rt.wait} ({rt.detail})"
        print(f"{t.key:<10} {t.lane:<11} {t.status:<17} {where}  {t.summary}")
    return 0


def cmd_status(args, cfg) -> int:
    tracker = make_tracker(cfg)
    ticket = tracker.get(args.key)
    print(f"key: {ticket.key}")
    print(f"summary: {ticket.summary}")
    print(f"status: {ticket.status}")
    print(f"lane: {ticket.lane}")
    print(f"tier: {ticket.tier}")
    print(f"attempts: {ticket.attempts}")
    print(f"promotions: {ticket.promotions}")
    print(f"last_endpoint: {ticket.last_endpoint or 'none'}")
    print(f"pr: {ticket.pr_number if ticket.pr_number is not None else 'none'}")
    print(f"review: {ticket.review_verdict or 'none'}")
    return 0


def cmd_run(args, cfg) -> int:
    if not args.dry_run:
        print("real runs arrive in step 3; use --dry-run", file=sys.stderr)
        return EXIT_BLOCKED
    owner = _owner(args.owner_command)
    tracker = make_tracker(cfg)
    ticket = tracker.get(args.key)
    state = _endpoint_state(cfg, tracker)
    rt = route(ticket, cfg, state, owner, args.endpoint)
    problems = preflight(ticket, cfg, rt, owner)

    print(f"{ticket.key}: lane {rt.lane}, endpoint {rt.endpoint.name if rt.ok else '-'}")
    if problems:
        print("blocked:")
        for p in problems:
            print(f"  {p}")
        return EXIT_BLOCKED

    exec_id = execution_id(ticket)
    pack = build_pack(ticket, cfg.projects[ticket.project], cfg, rt.endpoint, exec_id)
    out = Path(args.out).expanduser() if args.out else cfg.state_dir / "runs" / exec_id
    write_pack(pack, out)
    print(f"context pack: {out} ({pack.context_bytes:,} bytes incl. context files)")
    if pack.findings:
        print("blocked by privacy/secret scan:")
        for f in pack.findings:
            print(f"  {f}")
        return EXIT_BLOCKED
    if owner:
        print(f'owner command: "{owner}"')
    print("dry run: would launch", rt.endpoint.harness, "on", rt.endpoint.name)
    return 0


def cmd_review(args, cfg) -> int:
    if not args.dry_run:
        print("reviews arrive in step 4; use --dry-run", file=sys.stderr)
        return EXIT_BLOCKED
    tracker = make_tracker(cfg)
    ticket = tracker.get(args.key)
    if ticket.status != Status.IN_REVIEW:
        print(f"{ticket.key} is {ticket.status}, not In Review")
        return EXIT_BLOCKED
    rt, self_review = route_review(ticket, cfg, _endpoint_state(cfg, tracker), _owner(args.owner_command))
    if not rt.ok:
        print(f"{ticket.key}: review waits: {rt.wait} ({rt.detail})")
        return EXIT_BLOCKED
    note = " (self-review: flag for closer human review)" if self_review else ""
    print(f"{ticket.key}: would review with {rt.endpoint.name}{note}")
    return 0


def _resolve_project(cfg, key: str | None):
    if key is None:
        if len(cfg.projects) == 1:
            return next(iter(cfg.projects.values()))
        print(f"error: --project is required when the config has {len(cfg.projects)} projects", file=sys.stderr)
        return None
    project = cfg.projects.get(key)
    if project is None:
        print(f"error: unknown project {key}", file=sys.stderr)
    return project


def cmd_ticket_new(args, cfg) -> int:
    project = _resolve_project(cfg, args.project)
    if project is None:
        return EXIT_BLOCKED
    draft = ticketing.TicketDraft(
        title=args.title, kind=args.kind, tier=args.tier, risk=args.risk,
        description=args.description, criteria=list(args.criterion),
        context=list(args.context), allowed=list(args.allowed),
    )
    problems = ticketing.validate_draft(draft, cfg, project)
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return EXIT_BLOCKED
    body = ticketing.render(draft)
    if args.dry_run:
        print(body)
        return 0
    if not project.github_repo:
        print(f"error: project {project.key} has no github_repo configured", file=sys.stderr)
        return EXIT_BLOCKED
    try:
        url = ticketing.create_issue(project.github_repo, draft.title, body)
    except ticketing.TicketError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(url)
    return 0


LABELS = {
    "state:ready": ("0e8a16", "Approved for an agent; adding it starts a run"),
    "state:working": ("fbca04", "An agent run is in progress"),
    "state:awaiting-command": ("d93f0b", "Next endpoint runs only on /frontera run"),
    "state:in-review": ("1d76db", "Draft PR open"),
    "state:changes-requested": ("e99695", "Review asked for changes; /frontera fix to rerun"),
    "state:needs-decision": ("b60205", "Needs the owner: ambiguous, blocked, or out of tiers"),
    "state:failed": ("5a5a5a", "Transient failure; add state:ready to retry"),
    "kind:code": ("c5def5", "Code task"),
    "kind:research": ("c5def5", "Research task"),
    "tier:1": ("bfdadc", "Current tier: mechanical"),
    "tier:2": ("bfdadc", "Current tier: moderate"),
    "tier:3": ("bfdadc", "Current tier: hard"),
    "risk:high": ("b60205", "Runs only on an explicit /frontera run"),
    "human-only": ("000000", "Never dispatched to an agent"),
    "review:approve": ("0e8a16", "Agent review recommends merging"),
    "review:changes": ("e99695", "Agent review requested changes"),
    "review:block": ("b60205", "Agent review blocked the PR"),
    "review:self": ("fbca04", "Approved by the authoring endpoint; review closely"),
}


def cmd_labels(args, cfg) -> int:
    import subprocess

    names = dict(LABELS)
    for ep in cfg.endpoints:
        names[f"on:{ep}"] = ("ededed", f"Being worked on by {ep}")
        names[f"pin:{ep}"] = ("ededed", f"Force endpoint {ep}")
    for name, (color, desc) in names.items():
        cmd = ["gh", "label", "create", name, "--color", color, "--description", desc, "--force", "-R", args.repo]
        if args.dry_run:
            print(" ".join(cmd))
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True)
        print(f"{'ok' if proc.returncode == 0 else 'FAILED'}  {name}" + (f": {proc.stderr.strip()}" if proc.returncode else ""))
    return 0


def cmd_action(args, cfg) -> int:
    from .action import main_action

    name = args.event_name or os.environ.get("GITHUB_EVENT_NAME")
    path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not name or not path:
        print("frontera action runs inside GitHub Actions (needs GITHUB_EVENT_NAME/PATH)", file=sys.stderr)
        return 1
    return main_action(cfg, name, path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="frontera")
    p.add_argument("--config", help="config file (default $FRONTERA_CONFIG or ~/.config/frontera/config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="validate and show the config")
    c.add_argument("action", choices=["check"])
    c.set_defaults(func=cmd_config_check)

    n = sub.add_parser("next", help="list Ready and Awaiting Command tickets with their routes")
    n.add_argument("--project")
    n.set_defaults(func=cmd_next)

    s = sub.add_parser("status", help="show one ticket's state")
    s.add_argument("key")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("run", help="preflight, route and build the context pack for a ticket")
    r.add_argument("key")
    r.add_argument("--endpoint", help="force an endpoint (still subject to its launch policy)")
    r.add_argument("--owner-command", help="your instruction, quoted; required for on_command endpoints")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--out", help="write the pack here instead of the state dir")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("review", help="choose a reviewer for a ticket in In Review")
    v.add_argument("key")
    v.add_argument("--owner-command")
    v.add_argument("--dry-run", action="store_true")
    v.set_defaults(func=cmd_review)
    lb = sub.add_parser("labels", help="create or update Frontera's labels on a GitHub repo")
    lb.add_argument("--repo", required=True, help="owner/name")
    lb.add_argument("--dry-run", action="store_true")
    lb.set_defaults(func=cmd_labels)

    t = sub.add_parser("ticket", help="create tickets as GitHub issues")
    t_sub = t.add_subparsers(dest="ticket_cmd", required=True)
    tn = t_sub.add_parser("new", help="validate a ticket and create it as an unlabeled issue")
    tn.add_argument("--project", help="project key (default: the only configured project)")
    tn.add_argument("--title", required=True)
    tn.add_argument("--kind", required=True, help="code or research")
    tn.add_argument("--tier", required=True, type=int)
    tn.add_argument("--risk", default="low", help="low, medium or high")
    tn.add_argument("--description", default="")
    tn.add_argument("--criterion", action="append", default=[], help="acceptance criterion (repeatable)")
    tn.add_argument("--context", action="append", default=[], help="context file in the local checkout (repeatable)")
    tn.add_argument("--allowed", action="append", default=[], help="allowed path pattern (repeatable)")
    tn.add_argument("--dry-run", action="store_true", help="print the issue body without creating anything")
    tn.set_defaults(func=cmd_ticket_new)

    a = sub.add_parser("action", help="handle a GitHub Actions event (issues, issue_comment, workflow_dispatch)")
    a.add_argument("--event-name")
    a.add_argument("--event-path")
    a.set_defaults(func=cmd_action)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        return args.func(args, cfg)
    except (ConfigError, TrackerError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
