"""Run tickets end to end: route, launch, verify, open PRs, promote, review.

Everything external is injected (tracker, forge, launcher) so the control flow
is testable without GitHub or a model. See docs/GITHUB.md for the lifecycle.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from string import Template
from typing import Callable, Protocol

from .config import Config, Endpoint, Project
from .contextpack import PACK_DIR, build_pack, render_ticket, write_pack
from .harness import LaunchResult, LaunchSpec, launch
from .models import Status, Ticket, execution_id
from .preflight import default_allowed_paths, preflight
from .routing import WaitReason, promotion_target, route, route_review
from .state import EndpointState
from .tracker.github import GitHubTracker, Meta
from .verify import Verification, git, verify

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_LOOPS = 6  # attempts + promotions within one job


class Forge(Protocol):
    def push(self, branch: str) -> None: ...
    def open_pr(self, title: str, body: str, head: str, base: str) -> int: ...
    def close_pr(self, number: int, comment: str) -> None: ...
    def review_pr(self, number: int, body: str) -> None: ...


@dataclass
class Ctx:
    cfg: Config
    project: Project
    tracker: GitHubTracker
    forge: Forge
    run_url: str = ""
    launcher: Callable[[LaunchSpec], LaunchResult] = launch
    log: list[str] = field(default_factory=list)

    @property
    def repo(self) -> Path:
        return self.project.repo

    def say(self, msg: str) -> None:
        self.log.append(msg)
        print(msg, flush=True)


def _state(ctx: Ctx) -> EndpointState:
    return EndpointState(active=ctx.tracker.active_counts())


def _run_link(ctx: Ctx) -> str:
    return f" ([run]({ctx.run_url}))" if ctx.run_url else ""


def _meta(t: Ticket, **over) -> Meta:
    m = Meta(attempts=t.attempts, promotions=t.promotions, last_endpoint=t.last_endpoint,
             pr_number=t.pr_number, handoff_notes=list(t.handoff_notes))
    for k, v in over.items():
        setattr(m, k, v)
    return m


def _reset_worktree(ctx: Ctx, branch: str | None = None) -> None:
    base = ctx.project.base_branch
    git(ctx.repo, "reset", "--hard", "-q")
    git(ctx.repo, "clean", "-fdq", "-e", PACK_DIR)
    if branch:
        git(ctx.repo, "checkout", "-q", "-B", branch, f"origin/{base}")
    else:
        git(ctx.repo, "checkout", "-q", "--detach", f"origin/{base}")
    exclude = ctx.repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if f"/{PACK_DIR}/" not in (exclude.read_text() if exclude.exists() else ""):
        with open(exclude, "a") as f:
            f.write(f"\n/{PACK_DIR}/\n")


def _read_result(path: Path, exec_id: str) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("execution_id") != exec_id:
        return None
    return data


def _fmt_list(items) -> str:
    return "\n".join(f"- {i}" for i in items) or "- (none)"


# ---- single attempt ---------------------------------------------------------------

@dataclass
class Attempt:
    kind: str  # pr | decision | retry | promote | transient
    detail: str = ""
    pr: int | None = None
    branch: str | None = None
    execution_id: str | None = None


def attempt(ctx: Ctx, t: Ticket, ep: Endpoint) -> Attempt:
    lane = ctx.cfg.lanes[t.lane]
    exec_id = execution_id(t)
    branch = f"agent/{exec_id}"
    run_dir = ctx.repo / PACK_DIR / "runs" / exec_id

    ctx.tracker.set_status(t, Status.AGENT_WORKING, ep.name)
    ctx.tracker.save_meta(t, _meta(t, last_endpoint=ep.name, execution_id=exec_id, branch=branch),
                          f"Working on `{ep.name}` in lane `{lane.name}`, attempt {t.attempts + 1} of {lane.attempts}. "
                          f"Execution `{exec_id}`{_run_link(ctx)}.")
    _reset_worktree(ctx, branch)
    pack = build_pack(t, ctx.project, ctx.cfg, ep, exec_id)
    if pack.findings:
        return Attempt("decision", "The context sent to the model failed the privacy/secret scan:\n"
                       + _fmt_list(str(f) for f in pack.findings))
    write_pack(pack, ctx.repo / PACK_DIR)
    write_pack(pack, run_dir)

    res = ctx.launcher(LaunchSpec(ep, t.kind.value, pack.prompt, ctx.repo, lane.wall_clock_s, run_dir / "agent.log"))
    result = _read_result(ctx.repo / PACK_DIR / "result.json", exec_id)
    if result is not None:
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    (run_dir / "launch.json").write_text(json.dumps(
        {"returncode": res.returncode, "timed_out": res.timed_out, "error_class": res.error_class, "usage": res.usage}, indent=2))

    if res.error_class in ("rate_limit", "outage"):
        return Attempt("transient", f"{ep.name} reported {res.error_class.replace('_', ' ')}:\n```\n{res.output_tail[-1500:]}\n```")

    allowed = default_allowed_paths(t, ctx.project)
    check = ctx.project.check_command if t.kind.value == "code" else None
    v = verify(ctx.repo, allowed, ctx.cfg.private_terms, check)
    outcome = (result or {}).get("outcome")

    if outcome in ("needs_decision", "policy_blocked"):
        qs = (result or {}).get("questions") or []
        return Attempt("decision", f"Worker stopped: `{outcome}` ({(result or {}).get('reason_code')}). "
                       f"{(result or {}).get('summary', '')}\n\nQuestions:\n{_fmt_list(qs)}")
    if v.outside:
        return Attempt("decision", "The worker changed files outside Allowed Paths, so nothing was pushed:\n"
                       + _fmt_list(f"`{f}`" for f in v.outside))
    if v.findings:
        return Attempt("decision", "The change failed the secret/privacy scan, so nothing was pushed:\n"
                       + _fmt_list(str(f) for f in v.findings))

    note = _handoff_note(exec_id, ep, res, result, v)
    if outcome == "needs_higher_tier":
        return Attempt("promote", note)
    if res.timed_out or result is None or outcome != "success" or not v.files or v.check_ok is False:
        return Attempt("retry", note)

    git(ctx.repo, "add", "-A", "--", *v.files)
    git(ctx.repo, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-q",
        "-m", f"[{t.key}] {t.summary}\n\nExecution: {exec_id}\nEndpoint: {ep.name}")
    ctx.forge.push(branch)
    pr = ctx.forge.open_pr(f"[{t.key}] {t.summary}", _pr_body(ctx, t, ep, exec_id, result, v), branch,
                           ctx.project.base_branch)
    return Attempt("pr", (result or {}).get("summary", ""), pr, branch, exec_id)


def _handoff_note(exec_id: str, ep: Endpoint, res: LaunchResult, result: dict | None, v: Verification) -> str:
    why = ("timed out" if res.timed_out else "wrote no valid result.json" if result is None
           else f"outcome {result.get('outcome')}" if result.get("outcome") != "success"
           else "changed no files" if not v.files else "checks failed")
    parts = [f"Attempt {exec_id} on {ep.name}: {why}."]
    if result and result.get("summary"):
        parts.append(f"Worker summary: {result['summary']}")
    if v.files:
        parts.append(f"Files it changed: {', '.join(v.files)}")
    if v.check_ok is False:
        parts.append(f"Check output (tail):\n{v.check_output[-1200:]}")
    return "\n".join(parts)[:3000]


def _pr_body(ctx: Ctx, t: Ticket, ep: Endpoint, exec_id: str, result: dict | None, v: Verification) -> str:
    crit = "\n".join(f"- {c.get('text')}: {c.get('evidence')}" for c in (result or {}).get("criteria", [])) or "- (not reported)"
    check = ("passed" if v.check_ok else "failed" if v.check_ok is False else "not configured")
    risks = _fmt_list((result or {}).get("unresolved_risks") or [])
    return (f"Closes #{t.number}\n\n{(result or {}).get('summary', '')}\n\n"
            f"**Acceptance criteria (worker's evidence)**\n{crit}\n\n"
            f"**Dispatcher verification**\n- Files: {', '.join(f'`{f}`' for f in v.files)}\n"
            f"- All within Allowed Paths; secret/privacy scan clean\n- Check command: {check}\n\n"
            f"**Unresolved risks**\n{risks}\n\n"
            f"Endpoint `{ep.name}`, lane `{t.lane}`, execution `{exec_id}`{_run_link(ctx)}. "
            f"Agent-written; a human merges. CI does not run automatically on this PR yet.")


# ---- ticket lifecycle -----------------------------------------------------------------

def _promote(ctx: Ctx, t: Ticket, note: str) -> bool:
    """Move to the next lane. False when there is none (ticket goes to Needs Decision)."""
    target = promotion_target(t, ctx.cfg)
    if target is None:
        return False
    tier = ctx.cfg.lanes[target].tier
    ctx.tracker.set_tier(t, tier)
    t.attempts, t.promotions = 0, t.promotions + 1
    t.handoff_notes = (t.handoff_notes + [note])[-4:]
    return True


def _needs_decision(ctx: Ctx, t: Ticket, why: str) -> None:
    ctx.tracker.set_status(t, Status.NEEDS_DECISION)
    ctx.tracker.save_meta(t, _meta(t), "Needs your decision.")
    ctx.tracker.comment(t, f"**Needs your decision**\n\n{why}\n\nEdit the ticket, then add `state:ready` again.")


def run_ticket(ctx: Ctx, t: Ticket, owner_command: str | None = None, endpoint: str | None = None) -> str:
    """Drive one ticket until it reaches a PR, a decision, a wait, or a transient failure."""
    for _ in range(MAX_LOOPS):
        _reset_worktree(ctx)  # preflight checks context files against the base branch
        rt = route(t, ctx.cfg, _state(ctx), owner_command, endpoint)
        problems = preflight(t, ctx.cfg, rt, owner_command)
        if rt.wait == WaitReason.AWAITING_COMMAND and {p.code for p in problems} <= {"NEEDS_COMMAND", "ROUTE_AWAITING_COMMAND"}:
            ctx.tracker.set_status(t, Status.AWAITING_COMMAND)
            ctx.tracker.save_meta(t, _meta(t), f"Lane `{t.lane}` needs your command.")
            ctx.tracker.comment(t, f"Lane `{t.lane}`: {rt.detail}. Reply `/frontera run` to start it.")
            return "awaiting_command"
        if rt.wait == WaitReason.BUSY and len(problems) == 1:
            ctx.say(f"{t.key}: {rt.detail}; left in Ready for the next drain")
            return "busy"
        if problems:
            _needs_decision(ctx, t, "Preflight blocked this ticket:\n" + _fmt_list(str(p) for p in problems))
            return "blocked"

        a = attempt(ctx, t, rt.endpoint)
        t.last_endpoint = rt.endpoint.name
        if a.kind == "pr":
            t.pr_number = a.pr
            ctx.tracker.set_status(t, Status.IN_REVIEW)
            ctx.tracker.save_meta(t, _meta(t, pr_number=a.pr, branch=a.branch, execution_id=a.execution_id), f"Draft PR #{a.pr} is ready for review{_run_link(ctx)}.")
            ctx.tracker.comment(t, f"Draft PR #{a.pr} from `{rt.endpoint.name}`. "
                                   f"Reply `/frontera review` for an agent review, or review it yourself.")
            return "pr"
        if a.kind == "decision":
            _needs_decision(ctx, t, a.detail)
            return "decision"
        if a.kind == "transient":
            ctx.tracker.set_status(t, Status.AGENT_FAILED)
            ctx.tracker.save_meta(t, _meta(t), "Stopped by a transient provider problem; no attempt counted.")
            ctx.tracker.comment(t, f"**Transient failure**, no attempt counted.\n\n{a.detail}\n\nAdd `state:ready` again to retry.")
            return "transient"

        t.attempts += 1
        lane = ctx.cfg.lanes[t.lane]
        if a.kind == "retry" and t.attempts < lane.attempts:
            t.handoff_notes = (t.handoff_notes + [a.detail])[-4:]
            ctx.tracker.set_status(t, Status.READY)
            continue
        if not _promote(ctx, t, a.detail):
            _needs_decision(ctx, t, f"No higher tier is configured after `{lane.name}`.\n\nLast attempt:\n```\n{a.detail}\n```")
            return "exhausted"
        ctx.tracker.comment(t, f"Promoted to `{t.lane}` after `{lane.name}` could not finish it.")
        ctx.tracker.set_status(t, Status.READY)
        owner_command, endpoint = None, None  # a command covers the lane it was given for, not promotions
    _needs_decision(ctx, t, "Stopped after too many attempts in one job.")
    return "exhausted"


def fix_ticket(ctx: Ctx, t: Ticket, owner_command: str) -> str:
    if t.status != Status.CHANGES_REQUESTED:
        ctx.tracker.comment(t, f"`/frontera fix` applies to tickets in Changes Requested; this one is {t.status}.")
        return "ignored"
    if t.pr_number:
        ctx.forge.close_pr(t.pr_number, f"Superseded by a fix run for #{t.number}.")
    t.attempts, t.pr_number = 0, None
    ctx.tracker.set_status(t, Status.READY)
    return run_ticket(ctx, t, owner_command)


def promote_ticket(ctx: Ctx, t: Ticket, owner_command: str) -> str:
    lane = t.lane
    if not _promote(ctx, t, f"Promoted by owner: {owner_command}"):
        ctx.tracker.comment(t, f"No higher tier is configured after `{lane}`.")
        return "ignored"
    ctx.tracker.comment(t, f"Promoted from `{lane}` to `{t.lane}`.")
    ctx.tracker.set_status(t, Status.READY)
    return run_ticket(ctx, t)  # no owner command: an on_command lane waits for /frontera run


def review_ticket(ctx: Ctx, t: Ticket, owner_command: str | None) -> str:
    meta = ctx.tracker.meta(t.number)
    if t.status != Status.IN_REVIEW or not meta.pr_number or not meta.branch:
        ctx.tracker.comment(t, "`/frontera review` needs a ticket in In Review with an agent PR.")
        return "ignored"
    rt, self_review = route_review(t, ctx.cfg, _state(ctx), owner_command)
    if not rt.ok:
        ctx.tracker.comment(t, f"Review waits: {rt.detail}. Reply `/frontera review` to start it.")
        return "waiting"
    ep = rt.endpoint
    exec_id = f"{t.key}-review-{execution_id(t).split('-', 2)[-1]}"
    run_dir = ctx.repo / PACK_DIR / "runs" / exec_id
    _reset_worktree(ctx)
    git(ctx.repo, "fetch", "-q", "origin", meta.branch)
    git(ctx.repo, "checkout", "-q", "--detach", "FETCH_HEAD")
    diff = git(ctx.repo, "diff", f"origin/{ctx.project.base_branch}...HEAD")
    pack_dir = ctx.repo / PACK_DIR
    pack_dir.mkdir(exist_ok=True)
    (pack_dir / "result.json").unlink(missing_ok=True)
    (pack_dir / "ticket.md").write_text(render_ticket(t, default_allowed_paths(t, ctx.project)))
    (pack_dir / "diff.patch").write_text(diff)
    prompt = Template(resources.files("frontera").joinpath("prompts/review.md").read_text()).substitute(
        ticket_key=t.key, execution_id=exec_id, pack_dir=PACK_DIR)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(pack_dir / "ticket.md", run_dir / "ticket.md")
    lane = ctx.cfg.lanes["review"]
    res = ctx.launcher(LaunchSpec(ep, "review", prompt, ctx.repo, lane.wall_clock_s or 1800, run_dir / "agent.log"))
    result = _read_result(pack_dir / "result.json", exec_id)
    verdict = (result or {}).get("verdict")
    if verdict not in ("approve", "changes", "block"):
        ctx.tracker.comment(t, f"Review on `{ep.name}` produced no usable verdict"
                               f"{' (' + res.error_class + ')' if res.error_class else ''}{_run_link(ctx)}.")
        return "failed"
    comments = "\n".join(f"- `{c.get('path')}:{c.get('line')}`: {c.get('body')}" for c in result.get("comments", []))
    body = (f"**Agent review by `{ep.name}`: {verdict}**"
            f"{' (self-review: author and reviewer are the same endpoint)' if self_review else ''}\n\n"
            f"{result.get('summary', '')}\n\n{comments}\n\nA recommendation only; a human merges.")
    ctx.forge.review_pr(meta.pr_number, body)
    ctx.tracker.set_review(t, "self" if self_review and verdict == "approve" else verdict)
    if verdict == "changes":
        t.handoff_notes = (t.handoff_notes + [f"Review by {ep.name} requested changes:\n{result.get('summary', '')}\n{comments}"])[-4:]
        ctx.tracker.set_status(t, Status.CHANGES_REQUESTED)
        ctx.tracker.save_meta(t, _meta(t, branch=meta.branch, pr_number=meta.pr_number),
                              f"Review requested changes on #{meta.pr_number}. Reply `/frontera fix` to rerun.")
    elif verdict == "block":
        _needs_decision(ctx, t, f"Review by `{ep.name}` blocked PR #{meta.pr_number}:\n\n{result.get('summary', '')}")
    _reset_worktree(ctx)
    return verdict


def drain(ctx: Ctx, limit: int, skip: set[int] = frozenset()) -> list[str]:
    """Run other Ready tickets whose auto endpoint has room; workflow-made label changes don't trigger jobs."""
    done = []
    for t in ctx.tracker.list(statuses={Status.READY}):
        if len(done) >= limit:
            break
        if t.number in skip:
            continue
        rt = route(t, ctx.cfg, _state(ctx))
        if rt.ok:
            done.append(f"{t.key}: {run_ticket(ctx, t)}")
    return done
