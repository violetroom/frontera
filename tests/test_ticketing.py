import pytest

from frontera.issueform import parse_list, parse_sections
from frontera.ticketing import TicketDraft, TicketError, create_issue, render, validate_draft


def draft(**over):
    base = dict(
        title="Fix a thing", kind="code", tier=2, risk="low",
        description="Do the thing.", criteria=["It works"],
        context=["AGENTS.md"], allowed=["src/**"],
    )
    base.update(over)
    return TicketDraft(**base)


def test_render_round_trips(cfg):
    d = draft(kind="research", risk="medium", criteria=["Answer X", "Cite sources"],
              context=["AGENTS.md", "docs/spec.md"], allowed=[])
    s = parse_sections(render(d))
    assert s["kind"] == "research" and s["tier"] == "2" and s["risk"] == "medium"
    assert s["description"] == "Do the thing."
    assert parse_list(s["acceptance criteria"]) == ["Answer X", "Cite sources"]
    assert parse_list(s["context files"]) == ["AGENTS.md", "docs/spec.md"]
    assert parse_list(s["allowed paths"]) == []


def test_valid_draft_has_no_problems(cfg):
    assert validate_draft(draft(), cfg, cfg.projects["SHI"]) == []


def test_requires_at_least_one_criterion(cfg):
    problems = validate_draft(draft(criteria=[]), cfg, cfg.projects["SHI"])
    assert any("criterion" in p for p in problems)


def test_code_tickets_need_allowed_paths(cfg):
    problems = validate_draft(draft(allowed=[]), cfg, cfg.projects["SHI"])
    assert any("allowed path" in p for p in problems)


def test_research_tickets_may_have_no_allowed_paths(cfg):
    assert validate_draft(draft(kind="research", allowed=[]), cfg, cfg.projects["SHI"]) == []


@pytest.mark.parametrize("allowed", [".github/**", ".github", ".github/workflows/**", "**"])
def test_allowed_path_must_not_match_protected(cfg, allowed):
    problems = validate_draft(draft(allowed=[allowed]), cfg, cfg.projects["SHI"])
    assert any("protected" in p for p in problems)


def test_non_protected_allowed_path_is_fine(cfg):
    assert validate_draft(draft(allowed=["src/**", "tests/**"]), cfg, cfg.projects["SHI"]) == []


def test_context_file_must_exist(cfg):
    problems = validate_draft(draft(context=["missing.md"]), cfg, cfg.projects["SHI"])
    assert any("missing.md" in p for p in problems)


def test_scan_blocks_private_terms(cfg):
    problems = validate_draft(draft(description="deploy to secret-host.example"), cfg, cfg.projects["SHI"])
    assert any("private_term" in p for p in problems)


def test_scan_blocks_secrets(cfg):
    problems = validate_draft(draft(description="token ghp_" + "a" * 36 + " here"), cfg, cfg.projects["SHI"])
    assert any("github_token" in p for p in problems)


def test_bad_kind_tier_risk_rejected(cfg):
    assert any("kind" in p for p in validate_draft(draft(kind="docs"), cfg, cfg.projects["SHI"]))
    assert any("tier" in p for p in validate_draft(draft(tier=9), cfg, cfg.projects["SHI"]))
    assert any("risk" in p for p in validate_draft(draft(risk="urgent"), cfg, cfg.projects["SHI"]))


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_create_issue_calls_gh_without_labels():
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc(stdout="https://github.com/owner/name/issues/12\n")

    url = create_issue("owner/name", "Fix a thing", "body", runner=runner)
    assert url == "https://github.com/owner/name/issues/12"
    cmd = seen["cmd"]
    assert cmd[:3] == ["gh", "issue", "create"]
    assert cmd[cmd.index("--repo") + 1] == "owner/name"
    assert cmd[cmd.index("--title") + 1] == "Fix a thing"
    assert cmd[cmd.index("--body") + 1] == "body"
    assert "--label" not in cmd


def test_create_issue_error_raises():
    def runner(cmd, **kw):
        return FakeProc(returncode=1, stderr="boom")

    with pytest.raises(TicketError):
        create_issue("owner/name", "t", "b", runner=runner)
