from frontera.models import Kind, Risk, Status
from frontera.preflight import preflight
from frontera.routing import route
from frontera.state import EndpointState
from conftest import make_ticket


def codes(ticket, cfg, owner=None):
    return {p.code for p in preflight(ticket, cfg, route(ticket, cfg, EndpointState(), owner), owner)}


def test_clean_ticket_passes(cfg):
    assert codes(make_ticket(), cfg) == set()


def test_ineligible_and_no_criteria(cfg):
    assert codes(make_ticket(agent_eligible=False, acceptance_criteria=[" "]), cfg) == {"NOT_ELIGIBLE", "NO_CRITERIA"}


def test_not_ready(cfg):
    assert "NOT_READY" in codes(make_ticket(status=Status.BACKLOG), cfg)


def test_awaiting_command(cfg):
    t = make_ticket(status=Status.AWAITING_COMMAND, tier=3)
    assert codes(t, cfg) >= {"NEEDS_COMMAND", "ROUTE_AWAITING_COMMAND"}
    assert codes(t, cfg, owner="run SHI-1") == set()


def test_high_risk_needs_command(cfg):
    assert "HIGH_RISK" in codes(make_ticket(risk=Risk.HIGH), cfg)


def test_attempts_exhausted(cfg):
    assert "ATTEMPTS_EXHAUSTED" in codes(make_ticket(attempts=2), cfg)


def test_unknown_project(cfg):
    assert "PROJECT_NOT_ALLOWED" in codes(make_ticket(project="NOPE"), cfg)


def test_context_paths(cfg):
    got = codes(make_ticket(context_pack=["../outside.md", "/etc/passwd", "missing.md"]), cfg)
    assert got >= {"CONTEXT_OUTSIDE_REPO", "CONTEXT_MISSING"}


def test_code_needs_allowed_paths_research_does_not(cfg):
    assert "NO_ALLOWED_PATHS" in codes(make_ticket(allowed_paths=[]), cfg)
    assert codes(make_ticket(kind=Kind.RESEARCH, allowed_paths=[]), cfg) == set()
