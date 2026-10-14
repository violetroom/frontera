from datetime import datetime, timedelta, timezone

from frontera.config import parse_config
from frontera.routing import WaitReason, promotion_target, route, route_review
from frontera.state import EndpointState
from conftest import make_ticket

IDLE = EndpointState()


def test_t1_goes_to_qwen_automatically(cfg):
    r = route(make_ticket(), cfg, IDLE)
    assert r.ok and r.endpoint.name == "qwen-27b"


def test_t3_waits_for_command(cfg):
    r = route(make_ticket(tier=3), cfg, IDLE)
    assert not r.ok and r.wait == WaitReason.AWAITING_COMMAND


def test_t3_runs_on_owner_command(cfg):
    r = route(make_ticket(tier=3), cfg, IDLE, owner_command="do the promoted tickets")
    assert r.ok and r.endpoint.name == "claude-pro"


def test_promoted_ticket_uses_current_tier(cfg):
    t = make_ticket(tier=1, current_tier=3, promotions=1)
    assert route(t, cfg, IDLE).wait == WaitReason.AWAITING_COMMAND


def test_disabled_endpoint_cannot_be_forced(cfg):
    r = route(make_ticket(), cfg, IDLE, owner_command="use codex", endpoint="codex-plus")
    assert r.wait == WaitReason.NO_ENDPOINT


def test_pin_still_needs_command(cfg):
    r = route(make_ticket(pin_endpoint="claude-pro"), cfg, IDLE)
    assert r.wait == WaitReason.AWAITING_COMMAND


def test_cooling_and_capacity(cfg):
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert route(make_ticket(), cfg, EndpointState(cooling_until={"qwen-27b": later})).wait == WaitReason.BUSY
    assert route(make_ticket(), cfg, EndpointState(active={"qwen-27b": 1})).wait == WaitReason.BUSY
    earlier = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert route(make_ticket(), cfg, EndpointState(cooling_until={"qwen-27b": earlier})).ok


def test_falls_through_to_next_endpoint(raw_config):
    raw_config["endpoints"]["qwen-b"] = {"harness": "opencode", "launch": "auto"}
    raw_config["lanes"]["code/1"]["endpoints"] = ["qwen-27b", "qwen-b"]
    cfg = parse_config(raw_config)
    r = route(make_ticket(), cfg, EndpointState(active={"qwen-27b": 1}))
    assert r.endpoint.name == "qwen-b"


def test_project_allowlist(raw_config):
    raw_config["endpoints"]["qwen-27b"]["projects"] = ["OTHER"]
    cfg = parse_config(raw_config)
    assert route(make_ticket(), cfg, IDLE).wait == WaitReason.NO_ENDPOINT


def test_missing_lane(cfg):
    assert route(make_ticket(tier=2, kind="research"), cfg, IDLE).wait == WaitReason.NO_LANE


def test_promotion_targets(cfg):
    assert promotion_target(make_ticket(), cfg) == "code/3"
    assert promotion_target(make_ticket(tier=3), cfg) is None


def test_review_prefers_other_endpoint(cfg):
    t = make_ticket(last_endpoint="qwen-27b")
    r, self_review = route_review(t, cfg, IDLE)
    assert r.wait == WaitReason.AWAITING_COMMAND  # claude needs your command
    r, self_review = route_review(t, cfg, IDLE, owner_command="review the finished tickets")
    assert r.endpoint.name == "claude-pro" and not self_review


def test_claude_authored_pr_reviewed_by_fallback(cfg):
    r, self_review = route_review(make_ticket(last_endpoint="claude-pro"), cfg, IDLE)
    assert r.endpoint.name == "qwen-27b" and not self_review


def test_self_review_only_when_no_other_endpoint(raw_config):
    raw_config["lanes"]["review"] = {"endpoints": ["claude-pro"]}
    cfg = parse_config(raw_config)
    r, self_review = route_review(make_ticket(last_endpoint="claude-pro"), cfg, IDLE, owner_command="review")
    assert r.endpoint.name == "claude-pro" and self_review
