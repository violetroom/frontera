import pytest

from frontera.config import ConfigError, Launch, parse_config, parse_duration


def test_example_config_parses(cfg):
    assert cfg.endpoints["claude-pro"].launch == Launch.ON_COMMAND
    assert cfg.endpoints["codex-plus"].launch == Launch.DISABLED
    assert cfg.lanes["code/1"].wall_clock_s == 45 * 60
    assert cfg.lanes["code/1"].promote_to == "code/3"


@pytest.mark.parametrize("value,seconds", [("45m", 2700), ("90s", 90), ("2h", 7200), (30, 30), (None, None)])
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


def test_bad_duration():
    with pytest.raises(ConfigError):
        parse_duration("soon")


def test_unknown_endpoint_in_lane(raw_config):
    raw_config["lanes"]["code/1"]["endpoints"] = ["nope"]
    with pytest.raises(ConfigError, match="unknown endpoint"):
        parse_config(raw_config)


def test_promotion_must_go_up_within_kind(raw_config):
    raw_config["lanes"]["code/3"]["promote_to"] = "code/1"
    with pytest.raises(ConfigError, match="higher tier"):
        parse_config(raw_config)
    raw_config["lanes"]["code/3"].pop("promote_to")
    raw_config["lanes"]["code/1"]["promote_to"] = "research/3"
    with pytest.raises(ConfigError, match="higher tier"):
        parse_config(raw_config)


def test_bad_lane_name(raw_config):
    raw_config["lanes"]["design/1"] = {"endpoints": ["qwen-27b"]}
    with pytest.raises(ConfigError, match="lane design/1"):
        parse_config(raw_config)


def test_bad_launch(raw_config):
    raw_config["endpoints"]["qwen-27b"]["launch"] = "sometimes"
    with pytest.raises(ConfigError, match="launch"):
        parse_config(raw_config)


def test_private_terms_from_env(raw_config, monkeypatch):
    monkeypatch.setenv("FRONTERA_PRIVATE_TERMS", "host-a.example\n\n acct-b \n")
    raw_config["privacy"] = {"private_terms": ["x"], "private_terms_env": "FRONTERA_PRIVATE_TERMS"}
    assert parse_config(raw_config).private_terms == ("x", "host-a.example", "acct-b")
