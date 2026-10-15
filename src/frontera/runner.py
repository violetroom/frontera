"""Run tickets end to end: route, launch, verify, open PRs, promote, gate, fix, merge.

Everything external is injected (tracker, forge, launcher) so the control flow
is testable without GitHub or a model. See docs/GITHUB.md for the lifecycle.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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
from .routing import WaitReason, promotion_target, route, route_lane, route_review
from .state import EndpointState
from .tracker.github import GitHubTracker, Meta
from .verify import Verification, changed_files, git, run_check, verify

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_LOOPS = 6  # attempts + promotions within one job
MAX_GATE_FAILURES = 3  # failed review/test/fix cycles on one tier before promotion


class Forge(Protocol):
    def push(self, branch: str) -> None: ...
    def open_pr(self, title: str, body: str, head: str, base: str) -> int: ...
    def close_pr(self, number: int, comment: str) -> None: ...
    def review_pr(self, number: int, body: str) -> None: ...
    def comment_pr(self, number: int, body: str) -> None: ...
    def merge_pr(self, number: int, branch: str) -> tuple[bool, str]: ...


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


def _save(ctx: Ctx, t: Ticket, summary: str, **over) -> Meta:
    """Merge the ticket's counters into the stored state comment, so fields like branch survive."""
    m = ctx.tracker.meta(t.number)
    m.attempts, m.promotions, m.last_endpoint = t.attempts, t.promotions, t.last_endpoint
    m.pr_number, m.handoff_notes = t.pr_number, list(t.handoff_notes)
    for k, v in over.items():
        setattr(m, k, v)
    ctx.tracker.save_meta(t, m, summary)
    return m


def _reset_worktree(ctx: Ctx, branch: str | None = None, start: str | None = None) -> None:
    """Clean checkout of ``start`` (default: the base branch), optionally as local ``branch``."""
    start = start or f"origin/{ctx.project.base_branch}"
    git(ctx.repo, "reset", "--hard", "-q")
    git(ctx.repo, "clean", "-fdq", "-e", PACK_DIR)
    if branch:
        git(ctx.repo, "checkout", "-q", "-B", branch, start)
    else:
        git(ctx.repo, "checkout", "-q", "--detach", start)
    exclude = ctx.repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if f"/{PACK_DIR}/" not in (exclude.read_text() if exclude.exists() else ""):
        with open(exclude, "a") as f:
            f.write(f"\n/{PACK_DIR}/\n")


def _checkout_pr(ctx: Ctx, branch: str, local: str | None = None) -> None:
    _reset_worktree(ctx)
    git(ctx.repo, "fetch", "-q", "origin", branch)
    _reset_worktree(ctx, local, "FETCH_HEAD")


def _merge_base(ctx: Ctx, branch: str) -> tuple[list[str], str | None]:
    """Merge ``origin/<base>`` into the local PR branch before a fix, so the fix starts from
    the branch up to date with the base branch.

    Returns ``(conflicts, error)``. ``conflicts`` is non-empty when the merge stopped on
    conflicts; it is left in progress with conflict markers in place for the worker to
    resolve, and the merge commit is created with the worker's changes. ``error`` is set when
    the merge failed for another reason, in which case the branch was not updated."""
    base = f"origin/{ctx.project.base_branch}"
    git(ctx.repo, "fetch", "-q", "origin", ctx.project.base_branch)
    proc = subprocess.run(["git", "-C", str(ctx.repo), "-c", f"user.name={BOT_NAME}",
                           "-c", f"user.email={BOT_EMAIL}", "merge", "--no-edit",
                           "-m", f"Merge {base} into {branch}", base],
                          capture_output=True, text=True)
    unmerged = git(ctx.repo, "ls-files", "-u")
    if unmerged.strip():
        # Each line is ``<mode> <object> <stage>\t<filename>``; the path follows the first tab.
        return sorted({line.split("\t", 1)[1] for line in unmerged.splitlines() if "\t" in line}), None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return [], f"git merge: {detail}" if detail else "git merge failed without output"
    return [], None


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


def _template(name: str, **values) -> str:
    return Template(resources.files("frontera").joinpath(f"prompts/{name}.md").read_text()).substitute(**values)


# ---- one coding attempt -----------------------------------------------------------------

@dataclass
class Attempt:
    kind: str  # pr | decision | retry | promote | transient
    detail: str = ""
    pr: int | None = None
    branch: str | None = None
    execution_id: str | None = None


def attempt(ctx: Ctx, t: Ticket, ep: Endpoint, fix_branch: str | None = None) -> Attempt:
    """Run the coder once. With ``fix_branch``, work on top of that PR branch and push to it."""
    lane = ctx.cfg.lanes[t.lane]
    exec_id = execution_id(t)
    branch = fix_branch or f"agent/{exec_id}"
    run_dir = ctx.repo / PACK_DIR / "runs" / exec_id

    ctx.tracker.set_status(t, Status.AGENT_WORKING, ep.name)
    what = f"Fixing PR #{t.pr_number}" if fix_branch else f"Attempt {t.attempts + 1} of {lane.attempts}"
    _save(ctx, t, f"{what} on `{ep.name}` in lane `{lane.name}`. Execution `{exec_id}`{_run_link(ctx)}.",
          last_endpoint=ep.name, execution_id=exec_id, branch=branch)
    conflicts: list[str] = []
    merge_error: str | None = None
    if fix_branch:
        _checkout_pr(ctx, fix_branch, fix_branch)
        conflicts, merge_error = _merge_base(ctx, fix_branch)
    else:
        _reset_worktree(ctx, branch)
    if merge_error:
        return Attempt("decision", f"Merging `origin/{ctx.project.base_branch}` into `{branch}` "
                                   f"failed, so the branch was not updated:\n{merge_error}")
    pack = build_pack(t, ctx.project, ctx.cfg, ep, exec_id, conflicts=conflicts or None)
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
    v = verify(ctx.repo, allowed, ctx.cfg.private_terms, check, protected=ctx.project.protected_paths,
              conflicts=conflicts)
    outcome = (result or {}).get("outcome")

    if outcome in ("needs_decision", "policy_blocked"):
        qs = (result or {}).get("questions") or []
        return Attempt("decision", f"Worker stopped: `{outcome}` ({(result or {}).get('reason_code')}). "
                       f"{(result or {}).get('summary', '')}\n\nQuestions:\n{_fmt_list(qs)}")
    if v.protected:
        return Attempt("decision", "The worker changed protected files, which agents may never change, so nothing "
                       "was pushed:\n" + _fmt_list(f"`{f}`" for f in v.protected))
    if v.outside:
        return Attempt("decision", "The worker changed files outside Allowed Paths, so nothing was pushed:\n"
                       + _fmt_list(f"`{f}`" for f in v.outside))
    if v.findings:
        return Attempt("decision", "The change failed the secret/privacy scan, so nothing was pushed:\n"
                       + _fmt_list(str(f) for f in v.findings))

    if v.conflict_files:
        return Attempt("retry", "Leftover merge conflict markers (<<<<<<< / ======= / >>>>>>>) in:\n"
                        + _fmt_list(f"`{f}`" for f in v.conflict_files))

    note = _handoff_note(exec_id, ep, res, result, v)
    if outcome == "needs_higher_tier":
        return Attempt("promote", note)
    if res.timed_out or result is None or outcome != "success" or not v.files or v.check_ok is False:
        return Attempt("retry", note)

    git(ctx.repo, "add", "-A", "--", *v.files)
    git(ctx.repo, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-q",
        "-m", f"[{t.key}] {'Address review/test findings' if fix_branch else t.summary}\n\n"
              f"Execution: {exec_id}\nEndpoint: {ep.name}")
    ctx.forge.push(branch)
    if fix_branch:
        ctx.forge.comment_pr(t.pr_number, f"Fix pushed by `{ep.name}` (execution `{exec_id}`{_run_link(ctx)}):\n\n"
                                          f"{(result or {}).get('summary', '')}")
        return Attempt("pr", (result or {}).get("summary", ""), t.pr_number, branch, exec_id)
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
            f"Agent-written; a human merges (comment `/frontera approve`). CI does not run automatically on this PR yet.")


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
    _save(ctx, t, "Needs your decision.")
    ctx.tracker.comment(t, f"**Needs your decision**\n\n{why}\n\nEdit the ticket, then add `state:ready` again.")


def _handle_failed_attempt(ctx: Ctx, t: Ticket, a: Attempt) -> str | None:
    """Shared endings for decision/transient attempts; None means the caller continues."""
    if a.kind == "decision":
        _needs_decision(ctx, t, a.detail)
        return "decision"
    if a.kind == "transient":
        ctx.tracker.set_status(t, Status.AGENT_FAILED)
        _save(ctx, t, "Stopped by a transient provider problem; no attempt counted.")
        ctx.tracker.comment(t, f"**Transient failure**, no attempt counted.\n\n{a.detail}\n\nAdd `state:ready` again to retry.")
        return "transient"
    return None


def run_ticket(ctx: Ctx, t: Ticket, owner_command: str | None = None, endpoint: str | None = None) -> str:
    """Drive one ticket until it reaches a PR, a decision, a wait, or a transient failure."""
    for _ in range(MAX_LOOPS):
        _reset_worktree(ctx)  # preflight checks context files against the base branch
        rt = route(t, ctx.cfg, _state(ctx), owner_command, endpoint)
        problems = preflight(t, ctx.cfg, rt, owner_command)
        if rt.wait == WaitReason.AWAITING_COMMAND and {p.code for p in problems} <= {"NEEDS_COMMAND", "ROUTE_AWAITING_COMMAND"}:
            ctx.tracker.set_status(t, Status.AWAITING_COMMAND)
            _save(ctx, t, f"Lane `{t.lane}` needs your command.")
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
            _save(ctx, t, f"Draft PR #{a.pr} is ready for review{_run_link(ctx)}.",
                  branch=a.branch, execution_id=a.execution_id, gate_failures=0)
            ctx.tracker.comment(t, f"Draft PR #{a.pr} from `{rt.endpoint.name}`. Comment on it, for example "
                                   f"`/frontera review test approve`, or review it yourself.")
            return "pr"
        if (end := _handle_failed_attempt(ctx, t, a)) is not None:
            return end

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


def promote_ticket(ctx: Ctx, t: Ticket, owner_command: str) -> str:
    lane = t.lane
    if not _promote(ctx, t, f"Promoted by owner: {owner_command}"):
        ctx.tracker.comment(t, f"No higher tier is configured after `{lane}`.")
        return "ignored"
    if t.pr_number:
        ctx.forge.close_pr(t.pr_number, f"Closed: #{t.number} was promoted to `{t.lane}` and restarts from the base branch.")
        t.pr_number = None
    ctx.tracker.comment(t, f"Promoted from `{lane}` to `{t.lane}`.")
    ctx.tracker.set_status(t, Status.READY)
    _save(ctx, t, f"Promoted to `{t.lane}`.", branch=None, gate_failures=0)
    return run_ticket(ctx, t)  # no owner command: an on_command lane waits for /frontera run


# ---- gates: review and test -----------------------------------------------------------

@dataclass
class Gate:
    verdict: str  # pass | fail | block | unavailable
    detail: str = ""


def _launch_gate(ctx: Ctx, t: Ticket, ep: Endpoint, kind: str, prompt_name: str, lane_name: str,
                 exec_id: str, extra_files: dict[str, str]) -> tuple[dict | None, LaunchResult]:
    run_dir = ctx.repo / PACK_DIR / "runs" / exec_id
    pack_dir = ctx.repo / PACK_DIR
    pack_dir.mkdir(exist_ok=True)
    (pack_dir / "result.json").unlink(missing_ok=True)
    (pack_dir / "ticket.md").write_text(render_ticket(t, default_allowed_paths(t, ctx.project)))
    for name, text in extra_files.items():
        (pack_dir / name).write_text(text)
    prompt = _template(prompt_name, ticket_key=t.key, execution_id=exec_id, pack_dir=PACK_DIR,
                       check_command=ctx.project.check_command or "(none configured)")
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(pack_dir / "ticket.md", run_dir / "ticket.md")
    lane = ctx.cfg.lanes[lane_name]
    res = ctx.launcher(LaunchSpec(ep, kind, prompt, ctx.repo, lane.wall_clock_s or 1800, run_dir / "agent.log"))
    result = _read_result(pack_dir / "result.json", exec_id)
    if result is not None:
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    return result, res


def gate_review(ctx: Ctx, t: Ticket, owner_command: str | None, meta: Meta) -> Gate:
    rt, self_review = route_review(t, ctx.cfg, _state(ctx), owner_command)
    if not rt.ok:
        return Gate("unavailable", f"Review waits: {rt.detail}.")
    ep = rt.endpoint
    exec_id = f"{t.key}-review-{execution_id(t).split('-', 2)[-1]}"
    _checkout_pr(ctx, meta.branch)
    diff = git(ctx.repo, "diff", f"origin/{ctx.project.base_branch}...HEAD")
    result, res = _launch_gate(ctx, t, ep, "review", "review", "review", exec_id, {"diff.patch": diff})
    verdict = (result or {}).get("verdict")
    if verdict not in ("approve", "changes", "block"):
        return Gate("unavailable", f"Review on `{ep.name}` produced no usable verdict"
                                   f"{' (' + res.error_class + ')' if res.error_class else ''}{_run_link(ctx)}.")
    comments = "\n".join(f"- `{c.get('path')}:{c.get('line')}`: {c.get('body')}" for c in result.get("comments", []))
    note = " (self-review: author and reviewer are the same endpoint)" if self_review else ""
    ctx.forge.review_pr(meta.pr_number, f"**Agent review by `{ep.name}`: {verdict}**{note}\n\n"
                                        f"{result.get('summary', '')}\n\n{comments}\n\nA recommendation only; a human merges.")
    ctx.tracker.set_review(t, "self" if self_review and verdict == "approve" else verdict)
    detail = f"Review by {ep.name} ({verdict}): {result.get('summary', '')}\n{comments}"
    return Gate({"approve": "pass", "changes": "fail", "block": "block"}[verdict], detail)


def gate_test(ctx: Ctx, t: Ticket, owner_command: str | None, meta: Meta) -> Gate:
    """Run the project's checks, then have an agent exercise every acceptance criterion."""
    if "test" not in ctx.cfg.lanes:
        return Gate("unavailable", "No `test` lane is configured.")
    _checkout_pr(ctx, meta.branch)
    if ctx.project.check_command:
        ok, out = run_check(ctx.repo, ctx.project.check_command)
        if not ok:
            ctx.forge.comment_pr(meta.pr_number, f"**Test: fail**. The project check failed:\n```\n{out[-1500:]}\n```")
            return Gate("fail", f"The project check `{ctx.project.check_command}` failed:\n{out[-1500:]}")
    rt = route_lane("test", t, ctx.cfg, _state(ctx), owner_command)
    if not rt.ok:
        return Gate("unavailable", f"Test waits: {rt.detail}.")
    ep = rt.endpoint
    exec_id = f"{t.key}-test-{execution_id(t).split('-', 2)[-1]}"
    diff = git(ctx.repo, "diff", f"origin/{ctx.project.base_branch}...HEAD")
    result, res = _launch_gate(ctx, t, ep, "test", "test", "test", exec_id, {"diff.patch": diff})
    touched = changed_files(ctx.repo)
    verdict = (result or {}).get("verdict")
    if touched:
        verdict, extra = "fail", f"\nThe tester modified repository files, which it may not: {', '.join(touched)}"
    else:
        extra = ""
    if verdict not in ("pass", "fail"):
        return Gate("unavailable", f"Test on `{ep.name}` produced no usable verdict"
                                   f"{' (' + res.error_class + ')' if res.error_class else ''}{_run_link(ctx)}.")
    crit = "\n".join(f"- {'✓' if c.get('met') else '✗'} {c.get('text')}: {c.get('evidence')}" for c in result.get("criteria", []))
    problems = _fmt_list(result.get("problems") or [])
    ctx.forge.comment_pr(meta.pr_number, f"**Test by `{ep.name}`: {verdict}**\n\n{result.get('summary', '')}\n\n"
                                         f"{crit}\n\nProblems:\n{problems}{extra}")
    return Gate(verdict, f"Test by {ep.name} ({verdict}): {result.get('summary', '')}\n{crit}\nProblems:\n{problems}{extra}")


GATES = {"review": gate_review, "test": gate_test}


def _merge(ctx: Ctx, t: Ticket, meta: Meta, owner_command: str) -> str:
    ok, msg = ctx.forge.merge_pr(meta.pr_number, meta.branch)
    if not ok:
        ctx.tracker.comment(t, f"Could not merge PR #{meta.pr_number}: {msg}")
        return "merge_failed"
    ctx.tracker.set_status(t, Status.DONE)
    ctx.tracker.close(t)  # workflow-token merges do not auto-close the linked issue
    _save(ctx, t, f"Merged PR #{meta.pr_number}.")
    ctx.tracker.comment(t, f"Merged PR #{meta.pr_number} on your command {owner_command}.")
    return "merged"


def _fix(ctx: Ctx, t: Ticket, meta: Meta, owner_command: str | None, feedback: str) -> Attempt:
    rt = route(t, ctx.cfg, _state(ctx), owner_command)
    if not rt.ok:
        return Attempt("wait", rt.detail)
    t.handoff_notes = (t.handoff_notes + [feedback[:3000]])[-4:]
    a = attempt(ctx, t, rt.endpoint, fix_branch=meta.branch)
    t.last_endpoint = rt.endpoint.name
    if a.kind == "pr":
        ctx.tracker.set_status(t, Status.IN_REVIEW)
    return a


def _promote_after_failures(ctx: Ctx, t: Ticket, meta: Meta, note: str) -> str:
    lane = t.lane
    if not _promote(ctx, t, f"{MAX_GATE_FAILURES} failed review/test/fix cycles at {lane}. Last:\n{note}"[:3000]):
        _needs_decision(ctx, t, f"{MAX_GATE_FAILURES} failed cycles at `{lane}` and no higher tier is configured.\n\n{note}")
        return "exhausted"
    ctx.forge.close_pr(meta.pr_number, f"Closed after {MAX_GATE_FAILURES} failed cycles; #{t.number} moves to `{t.lane}`.")
    t.pr_number = None
    ctx.tracker.comment(t, f"Promoted from `{lane}` to `{t.lane}` after {MAX_GATE_FAILURES} failed review/test/fix cycles.")
    ctx.tracker.set_status(t, Status.READY)
    _save(ctx, t, f"Promoted to `{t.lane}`.", branch=None, gate_failures=0)
    return "promoted:" + run_ticket(ctx, t)  # an on_command lane waits for your /frontera run


def run_chain(ctx: Ctx, t: Ticket, steps: tuple[str, ...], owner_command: str) -> str:
    """Run gates in order on the ticket's PR. A failed gate sends the PR back to the coder with the
    findings and the chain starts again; MAX_GATE_FAILURES failures promote the ticket. ``approve``
    merges once every gate has passed (on its own, it merges straight away)."""
    meta = ctx.tracker.meta(t.number)
    if t.status not in (Status.IN_REVIEW, Status.CHANGES_REQUESTED) or not meta.pr_number or not meta.branch:
        ctx.tracker.comment(t, f"`{' '.join(steps)}` needs a ticket with an open agent PR (status is {t.status}).")
        return "ignored"
    gates = [s for s in steps if s in GATES]
    feedback: str | None = None
    while True:
        if feedback is None:
            for name in gates:
                g = GATES[name](ctx, t, owner_command, meta)
                if g.verdict == "block":
                    _needs_decision(ctx, t, f"`{name}` blocked PR #{meta.pr_number}:\n\n{g.detail}")
                    return "blocked"
                if g.verdict == "unavailable":
                    ctx.tracker.comment(t, f"`{name}` could not run: {g.detail}")
                    return "unavailable"
                if g.verdict == "fail":
                    feedback = f"{name} failed. {g.detail}"
                    break
            else:
                _reset_worktree(ctx)
                if "approve" in steps:
                    return _merge(ctx, t, meta, owner_command)
                ctx.tracker.set_status(t, Status.IN_REVIEW)
                _save(ctx, t, f"Passed {', '.join(gates)} on PR #{meta.pr_number}.", gate_failures=meta.gate_failures)
                ctx.tracker.comment(t, f"PR #{meta.pr_number} passed {', '.join(f'`{g}`' for g in gates)}. "
                                       f"Comment `/frontera approve` to merge.")
                return "passed"

        meta.gate_failures += 1
        _save(ctx, t, f"{meta.gate_failures} of {MAX_GATE_FAILURES} failed cycles on PR #{meta.pr_number}.",
              gate_failures=meta.gate_failures, branch=meta.branch)
        if meta.gate_failures >= MAX_GATE_FAILURES:
            return _promote_after_failures(ctx, t, meta, feedback)
        ctx.tracker.comment(t, f"Sending PR #{meta.pr_number} back to the coder "
                               f"(failure {meta.gate_failures} of {MAX_GATE_FAILURES}).")
        a = _fix(ctx, t, meta, owner_command, feedback)
        if a.kind == "pr":
            feedback = None
            meta = ctx.tracker.meta(t.number)
            continue
        if a.kind == "wait":
            ctx.tracker.set_status(t, Status.CHANGES_REQUESTED)
            ctx.tracker.comment(t, f"The coder can't run now ({a.detail}). Reply `/frontera fix` later.")
            return "waiting"
        if (end := _handle_failed_attempt(ctx, t, a)) is not None:
            return end
        if a.kind == "promote":
            return _promote_after_failures(ctx, t, meta, a.detail)
        feedback = f"{feedback}\n\nThe fix attempt also failed: {a.detail}"  # retry: counts as another failure


def fix_ticket(ctx: Ctx, t: Ticket, owner_command: str) -> str:
    """Rerun the coder on the PR branch with the handoff notes (latest review/test findings)."""
    meta = ctx.tracker.meta(t.number)
    if not meta.branch or not meta.pr_number:
        ctx.tracker.comment(t, "`/frontera fix` needs a ticket with an open agent PR.")
        return "ignored"
    a = _fix(ctx, t, meta, owner_command, "Owner asked for a fix: " + owner_command)
    if a.kind == "pr":
        _save(ctx, t, f"Fix pushed to PR #{meta.pr_number}.", branch=meta.branch)
        return "pr"
    if a.kind == "wait":
        ctx.tracker.comment(t, f"The coder can't run now: {a.detail}.")
        return "waiting"
    if (end := _handle_failed_attempt(ctx, t, a)) is not None:
        return end
    ctx.tracker.set_status(t, Status.CHANGES_REQUESTED)
    ctx.tracker.comment(t, f"The fix attempt failed:\n```\n{a.detail}\n```")
    return "failed"


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
