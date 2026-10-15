import json
import re
import subprocess

import pytest
import yaml

from fakes import REPO, FakeForge, FakeGh
from frontera.action import handle
from frontera.config import parse_config
from frontera.harness import LaunchResult
from frontera.issueform import render_body
from frontera.models import Status
from frontera.runner import Ctx
from frontera.tracker.github import GitHubTracker, Meta, ticket_from_issue
from frontera.tracker import TrackerError

EXAMPLE = __import__("conftest").EXAMPLE


def body(kind="code", tier=1, criteria=("It works",), context=("AGENTS.md",), allowed=("src/**",)):
    return render_body(kind, tier, "desc", list(criteria), list(context), list(allowed))


# ---- tracker -------------------------------------------------------------------------

def test_ticket_from_issue_labels_override_body():
    issue = {"number": 7, "title": "T", "body": body(tier=1), "state": "open",
             "labels": [{"name": "tier:3"}, {"name": "state:ready"}, {"name": "pin:qwen-27b"}]}
    t = ticket_from_issue(issue, "SHI", Meta(attempts=1, last_endpoint="qwen-27b"))
    assert (t.key, t.tier, t.current_tier, t.status, t.pin_endpoint, t.attempts) == ("SHI-7", 1, 3, Status.READY, "qwen-27b", 1)
    assert t.acceptance_criteria == ["It works"] and t.allowed_paths == ["src/**"]


def test_ticket_errors():
    with pytest.raises(TrackerError, match="kind"):
        ticket_from_issue({"number": 1, "title": "", "body": "", "labels": []}, "SHI", Meta())
    with pytest.raises(TrackerError, match="more than one"):
        ticket_from_issue({"number": 1, "title": "", "body": body(), "labels": [{"name": "state:ready"}, {"name": "state:working"}]}, "SHI", Meta())
    closed = ticket_from_issue({"number": 1, "title": "", "body": body(), "state": "closed", "labels": [{"name": "state:in-review"}]}, "SHI", Meta())
    assert closed.status == Status.DONE


def test_state_comment_only_trusted_from_bot():
    gh = FakeGh()
    gh.add_issue(1, body=body(), labels=["state:ready"])
    gh.add_comment(1, '<!-- frontera-state {"attempts": 9} -->', login="stranger")
    tr = GitHubTracker(gh, REPO, "SHI")
    assert tr.get(1).attempts == 0
    t = tr.get("SHI-1")
    tr.save_meta(t, Meta(attempts=2, pr_number=5), "hello")
    tr.save_meta(t, Meta(attempts=3, pr_number=5), "again")  # edits, doesn't add
    assert len(gh.bot_comments(1)) == 1 and tr.get(1).attempts == 3


def test_labels_and_counts():
    gh = FakeGh()
    gh.add_issue(1, body=body(), labels=["state:ready", "tier:1"])
    gh.add_issue(2, body=body(), labels=["state:working", "on:qwen-27b"])
    tr = GitHubTracker(gh, REPO, "SHI")
    t = tr.get(1)
    tr.set_status(t, Status.AGENT_WORKING, "qwen-27b")
    tr.set_tier(t, 3)
    assert sorted(gh.labels(1)) == ["on:qwen-27b", "state:working", "tier:3"]
    assert tr.active_counts() == {"qwen-27b": 2}
    tr.set_status(t, Status.IN_REVIEW)
    assert sorted(gh.labels(1)) == ["state:in-review", "tier:3"]
    assert [x.number for x in tr.list(statuses={Status.AGENT_WORKING})] == [2]
    assert tr.permission("owner") == "admin" and tr.permission("nobody") == "none"


# ---- runner via action events ---------------------------------------------------------

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "AGENTS.md").write_text("# Agents\n")
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("x = 1\n")
    _git(work, "checkout", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "main")

    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["tracker"] = {"kind": "github", "project": "SHI"}
    raw["projects"] = {"SHI": {"repo": str(work), "github_repo": REPO, "check_command": "exit 0"}}
    raw["state_dir"] = str(tmp_path / "state")
    raw["privacy"] = {"private_terms": []}
    raw["endpoints"]["qwen-27b"]["provider"] = {"base_url_env": "X", "api_key_env": "Y"}
    cfg = parse_config(raw)
    gh = FakeGh()
    forge = FakeForge(work)
    script = []
    launches = []

    def launcher(spec):
        launches.append(spec)
        step = script.pop(0) if script else {}
        exec_id = re.search(r"Execution ID: (\S+)", spec.prompt).group(1)
        for path, content in step.get("files", {}).items():
            (spec.cwd / path).parent.mkdir(parents=True, exist_ok=True)
            (spec.cwd / path).write_text(content)
        result = {"execution_id": exec_id, "outcome": step.get("outcome", "success"), "summary": "did it",
                  "questions": step.get("questions", []), **step.get("result", {})}
        if step.get("write_result", True):
            (spec.cwd / ".frontera" / "result.json").write_text(json.dumps(result))
        return LaunchResult(step.get("rc", 0), step.get("timed_out", False), "", {}, step.get("error_class"))

    tracker = GitHubTracker(gh, REPO, "SHI")
    ctx = Ctx(cfg, cfg.projects["SHI"], tracker, forge, "https://run", launcher)
    return type("W", (), dict(cfg=cfg, gh=gh, forge=forge, script=script, launches=launches, ctx=ctx,
                              work=work, origin=origin))


def label_event(number, actor="owner"):
    return "issues", {"action": "labeled", "label": {"name": "state:ready"}, "sender": {"login": actor},
                      "issue": {"number": number}}


def comment_event(number, text, actor="owner"):
    return "issue_comment", {"action": "created", "sender": {"login": actor}, "issue": {"number": number},
                             "comment": {"body": text, "user": {"login": actor}, "html_url": "https://c"}}


def go(w, event):
    return handle(w.cfg, event[0], event[1], w.ctx)


def test_stranger_is_ignored(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    assert go(world, label_event(1, "stranger")).startswith("ignored")
    assert go(world, comment_event(1, "/frontera run claude-pro", "stranger")).startswith("ignored")
    assert world.launches == [] and world.gh.bot_comments(1) == []


def test_t1_success_opens_draft_pr(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 2\n"}})
    assert "pr" in go(world, label_event(1))
    assert world.launches[0].endpoint.name == "qwen-27b"
    pr = world.forge.prs[0]
    assert pr["head"].startswith("agent/SHI-1-") and "Closes #1" in pr["body"]
    assert world.forge.pushed == [pr["head"]]
    assert "state:in-review" in world.gh.labels(1)
    meta = world.ctx.tracker.meta(1)
    assert meta.pr_number == 101 and meta.branch == pr["head"] and pr["head"] == f"agent/{meta.execution_id}"


def test_coding_launch_writes_full_launch_record(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 2\n"}})
    assert "pr" in go(world, label_event(1))
    exec_id = world.ctx.tracker.meta(1).execution_id
    data = json.loads((world.work / ".frontera" / "runs" / exec_id / "launch.json").read_text())
    assert data["endpoint"] == "qwen-27b" and data["harness"] == "opencode"
    assert data["kind"] == "code" and data["lane"] == "code/1"
    assert data["outcome"] == "success" and data["usage"] == {}
    assert isinstance(data["wall_s"], (int, float)) and data["wall_s"] >= 0


def test_failures_promote_to_awaiting_command(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.script += [{"outcome": "failed"}, {"files": {"src/app.py": "x = 3\n"}, "result": {}, "outcome": "failed"}]
    assert "awaiting_command" in go(world, label_event(1))
    assert len(world.launches) == 2  # code/1 allows two attempts
    labels = world.gh.labels(1)
    assert "tier:3" in labels and "state:awaiting-command" in labels
    t = world.ctx.tracker.get(1)
    assert t.promotions == 1 and t.attempts == 0 and len(t.handoff_notes) == 2

    # Your command runs it on Claude, with the handoff notes in the pack
    world.script.append({"files": {"src/app.py": "x = 4\n"}})
    assert go(world, comment_event(1, "/frontera run")) == "pr"
    assert world.launches[-1].endpoint.name == "claude-pro"
    ticket_md = (world.work / ".frontera" / "ticket.md").read_text()
    assert "Earlier attempts (handoff)" in ticket_md


def test_needs_higher_tier_promotes_immediately(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.script.append({"outcome": "needs_higher_tier"})
    assert "awaiting_command" in go(world, label_event(1))
    assert len(world.launches) == 1


def test_needs_decision_and_scope_violation(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.script.append({"outcome": "needs_decision", "questions": ["Which format?"]})
    assert "decision" in go(world, label_event(1))
    assert "state:needs-decision" in world.gh.labels(1)
    assert any("Which format?" in c for c in world.gh.bot_comments(1))

    world.gh.add_issue(2, body=body(), labels=["state:ready"])
    world.script.append({"files": {"README.md": "sneaky\n"}})
    assert "decision" in go(world, label_event(2))
    assert world.forge.pushed == [] and any("outside Allowed Paths" in c for c in world.gh.bot_comments(2))


def test_transient_failure_counts_no_attempt(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.script.append({"rc": 1, "error_class": "rate_limit", "write_result": False})
    assert "transient" in go(world, label_event(1))
    assert "state:failed" in world.gh.labels(1) and world.ctx.tracker.get(1).attempts == 0


def test_t3_label_waits_then_runs_on_command(world):
    world.gh.add_issue(1, body=body(tier=3), labels=["state:ready"])
    assert "awaiting_command" in go(world, label_event(1))
    assert world.launches == []


def test_preflight_block(world):
    world.gh.add_issue(1, body=body(criteria=()), labels=["state:ready"])
    assert "blocked" in go(world, label_event(1))
    assert "state:needs-decision" in world.gh.labels(1) and world.launches == []


def test_drain_runs_other_ready_tickets(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.gh.add_issue(2, body=body(allowed=("docs/**",)), labels=["state:ready"])  # disjoint from #1's src/**
    world.script += [{"files": {"src/app.py": "a\n"}}, {"files": {"docs/x.md": "b\n"}}]
    out = go(world, label_event(1))
    assert "SHI-2: pr" in out and len(world.forge.prs) == 2


def test_held_when_paths_overlap_open_pr(world):
    _open_pr(world)  # #1 -> draft PR, In Review, Allowed Paths src/**
    world.gh.add_issue(2, body=body(allowed=("src/app.py",)), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 9\n"}})
    out = go(world, label_event(2))
    assert "held" in out and len(world.launches) == 1  # not run
    assert "state:ready" in world.gh.labels(2)
    held = [c for c in world.gh.bot_comments(2) if c.startswith("**Held**")]
    assert len(held) == 1 and "#1" in held[0]


def test_held_by_working_ticket(world):
    world.gh.add_issue(1, body=body(), labels=["state:working"])  # stuck in Agent Working, allowed src/**
    world.gh.add_issue(2, body=body(allowed=("src/app.py",)), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 9\n"}})
    out = go(world, label_event(2))
    assert "held" in out and world.launches == []
    assert any("#1" in c for c in world.gh.bot_comments(2))


def test_held_comments_once_per_held_set(world):
    _open_pr(world)
    world.gh.add_issue(2, body=body(allowed=("src/app.py",)), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 9\n"}})
    assert "held" in go(world, label_event(2))
    assert "held" in go(world, label_event(2))  # re-labeled while still held
    assert sum(c.startswith("**Held**") for c in world.gh.bot_comments(2)) == 1


def test_drain_skips_held_and_runs_others(world):
    _open_pr(world)  # #1 In Review, allowed src/**
    world.gh.add_issue(2, body=body(allowed=("src/app.py",)), labels=["state:ready"])
    world.gh.add_issue(3, body=body(allowed=("docs/**",)), labels=["state:ready"])
    world.script.append({"files": {"docs/x.md": "d\n"}})
    out = go(world, label_event(2))
    assert "#2: held" in out and "SHI-3: pr" in out
    assert len(world.forge.prs) == 2
    assert "state:ready" in world.gh.labels(2) and "state:in-review" in world.gh.labels(3)
    # A later drain skips #2 again without commenting a second time.
    world.gh.add_issue(4, body=body(allowed=("notes/**",)), labels=["state:ready"])
    world.script.append({"files": {"notes/4.md": "n\n"}})
    out = go(world, label_event(4))
    assert "#4: pr" in out and "SHI-2" not in out
    assert sum(c.startswith("**Held**") for c in world.gh.bot_comments(2)) == 1


def test_owner_run_overrides_hold(world):
    _open_pr(world)  # #1 In Review
    world.gh.add_issue(2, body=body(allowed=("src/app.py",)), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 9\n"}})
    assert "held" in go(world, label_event(2))
    world.script.append({"files": {"src/app.py": "x = 10\n"}})
    assert go(world, comment_event(2, "/frontera run")) == "pr"
    assert world.launches[-1].kind == "code"
    assert "state:in-review" in world.gh.labels(2)


def _open_pr(world, number=1, tier=1):
    """Ticket -> Qwen run -> draft PR on a branch that really exists on origin."""
    world.gh.add_issue(number, body=body(tier=tier), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 2\n"}})
    go(world, label_event(number))
    return world.forge.prs[-1]["head"]


def _git_out(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout


def _advance_main(world, files):
    """Commit ``files`` to main on the bare origin (via a side clone); return the new main SHA."""
    url = _git_out(world.work, "remote", "get-url", "origin").strip()
    ahead = world.work.parent / "ahead"
    subprocess.run(["git", "clone", "-q", "--branch", "main", url, str(ahead)],
                   check=True, capture_output=True)
    for rel, content in files.items():
        p = ahead / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(ahead, "add", "-A")
    _git(ahead, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "main advances")
    sha = _git_out(ahead, "rev-parse", "HEAD").strip()
    _git(ahead, "push", "-q", "origin", "main")
    return sha


def _origin_contains(world, ancestor, ref):
    """True when ``ancestor`` is an ancestor of (or equal to) ``ref`` on the bare origin."""
    r = subprocess.run(["git", "-C", str(world.origin), "merge-base", "--is-ancestor",
                        ancestor, ref], capture_output=True)
    return r.returncode == 0


def _origin_file(world, ref, rel):
    r = subprocess.run(["git", "-C", str(world.origin), "show", f"{ref}:{rel}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def test_review_test_approve_all_pass_merges(world):
    _open_pr(world)
    world.script += [{"result": {"verdict": "approve"}}, {"result": {"verdict": "pass"}}]
    assert go(world, comment_event(1, "/frontera review test approve")) == "merged"
    assert [l.kind for l in world.launches[1:]] == ["review", "test"]
    assert world.launches[1].endpoint.name == "claude-pro" and world.launches[2].endpoint.name.startswith("qwen")
    assert world.forge.merged == [101]
    assert not [l for l in world.gh.labels(1) if l.startswith("state:")]
    assert world.gh.issues[1]["state"] == "closed" and world.gh.issues[1]["state_reason"] == "completed"


def test_review_and_test_launches_write_launch_records(world):
    _open_pr(world)
    world.script += [{"result": {"verdict": "approve"}}, {"result": {"verdict": "pass"}}]
    assert go(world, comment_event(1, "/frontera review test approve")) == "merged"
    review = json.loads((world.launches[1].log_path.parent / "launch.json").read_text())
    assert review["kind"] == "review" and review["lane"] == "review"
    assert review["endpoint"] == "claude-pro" and review["harness"] == "claude-code"
    assert review["outcome"] == "approve"
    test = json.loads((world.launches[2].log_path.parent / "launch.json").read_text())
    assert test["kind"] == "test" and test["lane"] == "test"
    assert test["endpoint"].startswith("qwen") and test["outcome"] == "pass"


def test_failed_review_sends_back_fixes_same_pr_then_merges(world):
    head = _open_pr(world)
    world.script += [
        {"result": {"verdict": "changes", "comments": [{"path": "src/app.py", "line": 1, "body": "use 3"}]}},
        {"files": {"src/app.py": "x = 3\n"}},          # coder fix on the same branch
        {"result": {"verdict": "approve"}},             # re-review
    ]
    assert go(world, comment_event(1, "/frontera review approve")) == "merged"
    fix = world.launches[2]
    assert fix.kind == "code" and fix.endpoint.name.startswith("qwen")
    fix_pack = (fix.log_path.parent / "ticket.md").read_text()
    assert "Earlier attempts (handoff)" in fix_pack and "use 3" in fix_pack
    assert world.forge.pushed.count(head) == 2 and len(world.forge.prs) == 1  # fix pushed to the same PR
    assert world.ctx.tracker.meta(1).gate_failures == 1
    assert any("Fix pushed" in b for _, b in world.forge.pr_comments)


def test_fix_merges_behind_branch_clean(world):
    head = _open_pr(world)
    main_sha = _advance_main(world, {"src/other.py": "y = 1\n"})  # main moves ahead, no overlap
    world.script.append({"files": {"src/app.py": "x = 3\n"}})
    assert go(world, comment_event(1, "/frontera fix")) == "pr"
    # The fix started from the branch merged with origin/main; the push is a fast-forward.
    assert _origin_contains(world, main_sha, head)
    assert _origin_file(world, head, "src/other.py") == "y = 1\n"
    assert _origin_file(world, head, "src/app.py") == "x = 3\n"


def test_fix_resolves_conflict(world):
    head = _open_pr(world)
    _advance_main(world, {"src/app.py": "x = 100\n"})  # overlaps the PR branch's x = 2 -> conflict
    world.script.append({"files": {"src/app.py": "x = 42\n"}})  # the worker resolves it
    assert go(world, comment_event(1, "/frontera fix")) == "pr"
    assert _origin_file(world, head, "src/app.py") == "x = 42\n"
    # The ticket names the conflicted file; the worker prompt says resolving it is required.
    fix = world.launches[-1]
    run_dir = fix.log_path.parent
    ticket_md = (run_dir / "ticket.md").read_text()
    prompt_md = (run_dir / "prompt.md").read_text()
    assert "Merge conflicts to resolve" in ticket_md and "src/app.py" in ticket_md
    assert "conflict" in prompt_md and "required" in prompt_md


def test_fix_leftover_markers_rejected(world):
    head = _open_pr(world)
    _advance_main(world, {"src/app.py": "x = 100\n"})  # overlaps the PR branch's x = 2 -> conflict
    world.script.append({"outcome": "success"})  # the worker leaves the markers in place
    assert "failed" in go(world, comment_event(1, "/frontera fix"))
    # Nothing is pushed: the PR branch still has its original content.
    assert _origin_file(world, head, "src/app.py") == "x = 2\n"


def test_fix_conflict_with_main_change_outside_allowed_paths(world):
    head = _open_pr(world)
    _advance_main(world, {"src/app.py": "x = 100\n", "notes.md": "main's note\n"})
    world.script.append({"files": {"src/app.py": "x = 42\n"}})  # the worker resolves the conflict
    assert go(world, comment_event(1, "/frontera fix")) == "pr"
    # notes.md is outside Allowed Paths but came in through the merge, not from the worker,
    # so it is not scope-checked; it still lands on the PR branch.
    assert _origin_file(world, head, "src/app.py") == "x = 42\n"
    assert _origin_file(world, head, "notes.md") == "main's note\n"
    assert world.forge.pushed.count(head) == 2


def test_fix_merge_failure_needs_decision(world):
    head = _open_pr(world)
    # Force the PR branch to unrelated history, so the base merge cannot run.
    url = _git_out(world.work, "remote", "get-url", "origin").strip()
    stray = world.work.parent / "stray"
    subprocess.run(["git", "init", "-q", "-b", head, str(stray)], check=True, capture_output=True)
    (stray / "src").mkdir()
    (stray / "src" / "app.py").write_text("x = 2\n")
    _git(stray, "add", "-A")
    _git(stray, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "unrelated")
    _git(stray, "push", "-q", "-f", url, head)
    world.script.append({"files": {"src/app.py": "x = 3\n"}})
    assert "decision" in go(world, comment_event(1, "/frontera fix"))
    assert any("unrelated histories" in c for c in world.gh.bot_comments(1))
    assert len(world.forge.pushed) == 1  # the fix was not started, nothing was pushed


def test_three_failures_promote_and_close_pr(world):
    _open_pr(world)
    world.script += [
        {"result": {"verdict": "fail", "problems": ["boom"]}},   # test fails (1)
        {"files": {"src/app.py": "x = 4\n"}},                    # fix
        {"result": {"verdict": "fail", "problems": ["boom"]}},   # test fails (2)
        {"files": {"src/app.py": "x = 5\n"}},                    # fix
        {"result": {"verdict": "fail", "problems": ["boom"]}},   # test fails (3) -> promote
    ]
    out = go(world, comment_event(1, "/frontera test approve"))
    assert out == "promoted:awaiting_command"
    assert world.forge.closed == [101] and world.forge.merged == []
    labels = world.gh.labels(1)
    assert "tier:3" in labels and "state:awaiting-command" in labels
    assert world.ctx.tracker.meta(1).gate_failures == 0


def test_failed_check_fails_test_without_launching_tester(world):
    _open_pr(world)
    world.cfg.projects["SHI"].__dict__["check_command"] = "exit 1"  # frozen dataclass; test-only override
    world.script.append({"outcome": "failed"})  # the fix attempt fails too
    n = len(world.launches)
    go(world, comment_event(1, "/frontera test"))
    assert all(l.kind != "test" for l in world.launches[n:])
    assert any("project check" in b for _, b in world.forge.pr_comments)


def test_tester_that_edits_files_fails(world):
    _open_pr(world)
    world.script += [{"files": {"src/app.py": "hacked\n"}, "result": {"verdict": "pass"}},
                     {"files": {"src/app.py": "x = 9\n"}}, {"result": {"verdict": "pass"}}]
    assert go(world, comment_event(1, "/frontera test approve")) == "merged"
    assert any("modified repository files" in b for _, b in world.forge.pr_comments)
    assert world.ctx.tracker.meta(1).gate_failures == 1


def test_block_needs_decision(world):
    _open_pr(world)
    world.script.append({"result": {"verdict": "block"}})
    assert go(world, comment_event(1, "/frontera review approve")) == "blocked"
    assert "state:needs-decision" in world.gh.labels(1) and world.forge.merged == []


def test_approve_alone_merges_and_reports_conflicts(world):
    _open_pr(world)
    world.forge.merge_result = (False, "it has merge conflicts")
    assert go(world, comment_event(1, "/frontera approve")) == "merge_failed"
    assert any("merge conflicts" in c for c in world.gh.bot_comments(1))
    assert world.gh.issues[1]["state"] == "open"  # a failed merge leaves the issue open
    world.forge.merge_result = (True, "merged")
    assert go(world, comment_event(1, "/frontera approve")) == "merged"
    assert world.gh.issues[1]["state"] == "closed" and world.gh.issues[1]["state_reason"] == "completed"


def test_run_then_chain_in_one_command(world):
    world.gh.add_issue(1, body=body(), labels=["state:awaiting-command"])
    world.script += [{"files": {"src/app.py": "x = 2\n"}}, {"result": {"verdict": "approve"}}]
    assert go(world, comment_event(1, "/frontera run review approve")) == "merged"


def test_chain_needs_open_pr(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    assert go(world, comment_event(1, "/frontera review")) == "ignored"


def test_bad_command_gets_usage_reply_for_owner_only(world):
    world.gh.add_issue(1, body=body())
    go(world, comment_event(1, "/frontera dance", "stranger"))
    assert world.gh.bot_comments(1) == []
    go(world, comment_event(1, "/frontera dance"))
    assert "usage" in world.gh.bot_comments(1)[0]


def pr_comment_event(pr, text, actor="owner"):
    name, ev = comment_event(pr, text, actor)
    ev["issue"]["pull_request"] = {"url": "x"}
    return name, ev


def test_review_command_on_agent_pr_maps_to_ticket(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 2\n"}})
    go(world, label_event(1))
    head = world.forge.prs[0]["head"]
    world.gh.add_issue(101, title="PR")  # PRs are issues too
    world.gh.pulls[101] = {"head": {"ref": head, "repo": {"full_name": REPO}}}
    world.script.append({"result": {"verdict": "approve"}})
    assert go(world, pr_comment_event(101, "/frontera review")) == "passed"
    assert "review:approve" in world.gh.labels(1)


def test_pr_commands_ignore_forks_and_non_agent_branches(world):
    world.gh.add_issue(5, title="PR")
    world.gh.pulls[5] = {"head": {"ref": "agent/SHI-1-x", "repo": {"full_name": "someone/fork"}}}
    assert go(world, pr_comment_event(5, "/frontera review")) == "ignored: not an agent PR"
    world.gh.pulls[5] = {"head": {"ref": "feature/x", "repo": {"full_name": REPO}}}
    assert go(world, pr_comment_event(5, "/frontera review")) == "ignored: not an agent PR"
    assert world.launches == []


def test_protected_paths_refused_even_when_allowed(world):
    world.gh.add_issue(1, body=body(allowed=("**",)), labels=["state:ready"])
    world.script.append({"files": {".github/workflows/x.yml": "on: push\n"}})
    assert "decision" in go(world, label_event(1))
    assert world.forge.pushed == [] and any("protected files" in c for c in world.gh.bot_comments(1))
