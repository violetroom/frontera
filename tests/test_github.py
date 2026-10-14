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
    forge = FakeForge()
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
    return type("W", (), dict(cfg=cfg, gh=gh, forge=forge, script=script, launches=launches, ctx=ctx, work=work))


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
    world.gh.add_issue(2, body=body(), labels=["state:ready"])
    world.script += [{"files": {"src/app.py": "a\n"}}, {"files": {"src/app.py": "b\n"}}]
    out = go(world, label_event(1))
    assert "SHI-2: pr" in out and len(world.forge.prs) == 2


def test_review_changes_then_fix(world):
    world.gh.add_issue(1, body=body(), labels=["state:ready"])
    world.script.append({"files": {"src/app.py": "x = 2\n"}})
    go(world, label_event(1))
    head = world.forge.prs[0]["head"]
    _git(world.work, "push", "-q", "origin", f"{head}:{head}")  # FakeForge doesn't push; do it for the review fetch

    world.script.append({"result": {"verdict": "changes", "comments": [{"path": "src/app.py", "line": 1, "body": "wrong"}]}})
    assert go(world, comment_event(1, "/frontera review")) == "changes"
    assert world.launches[-1].endpoint.name == "claude-pro" and world.launches[-1].kind == "review"
    assert world.forge.reviews and "wrong" in world.forge.reviews[0][1]
    assert {"state:changes-requested", "review:changes"} <= set(world.gh.labels(1))

    world.script.append({"files": {"src/app.py": "x = 5\n"}})
    assert go(world, comment_event(1, "/frontera fix")) == "pr"
    assert world.forge.closed == [101] and len(world.forge.prs) == 2


def test_bad_command_gets_usage_reply_for_owner_only(world):
    world.gh.add_issue(1, body=body())
    go(world, comment_event(1, "/frontera dance", "stranger"))
    assert world.gh.bot_comments(1) == []
    go(world, comment_event(1, "/frontera dance"))
    assert "usage" in world.gh.bot_comments(1)[0]
