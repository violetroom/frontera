"""Command-line entry point. Step 2 provides config checks, routing views and dry runs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .contextpack import build_pack, write_pack
from .models import Status, execution_id
from .preflight import preflight
from .routing import route, route_review
from .state import EndpointState
from .tracker import TrackerError, make_tracker

EXIT_BLOCKED = 2


def _owner(cmd: str | None) -> str | None:
    return cmd.strip() if cmd and cmd.strip() else None


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
    tracker = make_tracker(cfg.tracker)
    state = EndpointState.load(cfg.state_dir)
    tickets = tracker.list(args.project, {Status.READY, Status.AWAITING_COMMAND})
    if not tickets:
        print("nothing ready")
        return 0
    for t in tickets:
        rt = route(t, cfg, state)
        where = rt.endpoint.name if rt.ok else f"waits: {rt.wait} ({rt.detail})"
        print(f"{t.key:<10} {t.lane:<11} {t.status:<17} {where}  {t.summary}")
    return 0


def cmd_run(args, cfg) -> int:
    if not args.dry_run:
        print("real runs arrive in step 3; use --dry-run", file=sys.stderr)
        return EXIT_BLOCKED
    owner = _owner(args.owner_command)
    tracker = make_tracker(cfg.tracker)
    ticket = tracker.get(args.key)
    state = EndpointState.load(cfg.state_dir)
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
    tracker = make_tracker(cfg.tracker)
    ticket = tracker.get(args.key)
    if ticket.status != Status.IN_REVIEW:
        print(f"{ticket.key} is {ticket.status}, not In Review")
        return EXIT_BLOCKED
    rt, self_review = route_review(ticket, cfg, EndpointState.load(cfg.state_dir), _owner(args.owner_command))
    if not rt.ok:
        print(f"{ticket.key}: review waits: {rt.wait} ({rt.detail})")
        return EXIT_BLOCKED
    note = " (self-review: flag for closer human review)" if self_review else ""
    print(f"{ticket.key}: would review with {rt.endpoint.name}{note}")
    return 0


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
