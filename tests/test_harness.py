import json

import pytest

from frontera.config import Endpoint, Launch
from frontera.harness import HarnessError, LaunchSpec, build_command, classify

SRC = {"PATH": "/bin", "HOME": "/h", "GH_TOKEN": "secret-gh", "TELLICO_API_KEY": "k",
       "TELLICO_GATEWAY_URL": "https://gw.example/v1/", "CLAUDE_CODE_OAUTH_TOKEN": "c", "ACTIONS_RUNTIME_TOKEN": "r"}

QWEN = Endpoint("qwen-27b", "opencode", Launch.AUTO, model="tellico-0/qwen3.8-27b-gsq-iq3s",
                provider={"base_url_env": "TELLICO_GATEWAY_URL", "base_url_suffix": "/node0",
                          "api_key_env": "TELLICO_API_KEY", "model_config": {"reasoning": True}})
CLAUDE = Endpoint("claude-pro", "claude-code", Launch.ON_COMMAND)


def spec(ep, kind="code", tmp_path=None):
    return LaunchSpec(ep, kind, "do it", tmp_path, 60, tmp_path / "log")


def test_opencode_command_and_env(tmp_path):
    cmd, env = build_command(spec(QWEN, tmp_path=tmp_path), SRC)
    assert cmd[:2] == ["opencode", "run"] and "--auto" in cmd and cmd[-1] == "do it"
    assert set(env) == {"PATH", "HOME", "TELLICO_API_KEY", "OPENCODE_CONFIG_CONTENT"}
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    prov = cfg["provider"]["tellico-0"]
    assert prov["options"]["baseURL"] == "https://gw.example/v1/node0"
    assert prov["options"]["apiKey"] == "{env:TELLICO_API_KEY}"  # key itself never written into config
    assert prov["models"]["qwen3.8-27b-gsq-iq3s"]["reasoning"] is True
    assert cfg["permission"]["webfetch"] == "deny"


def test_research_gets_web_review_gets_no_edits(tmp_path):
    cfg = json.loads(build_command(spec(QWEN, "research", tmp_path), SRC)[1]["OPENCODE_CONFIG_CONTENT"])
    assert cfg["permission"]["webfetch"] == "allow"
    cfg = json.loads(build_command(spec(QWEN, "review", tmp_path), SRC)[1]["OPENCODE_CONFIG_CONTENT"])
    assert cfg["permission"]["edit"] == "deny"


def test_claude_command_and_env(tmp_path):
    cmd, env = build_command(spec(CLAUDE, tmp_path=tmp_path), SRC)
    assert cmd[:3] == ["claude", "-p", "do it"]
    assert "WebSearch,WebFetch" in cmd
    assert set(env) == {"PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN"}


def test_missing_secret(tmp_path):
    with pytest.raises(HarnessError):
        build_command(spec(CLAUDE, tmp_path=tmp_path), {"PATH": "/bin"})


def test_classify():
    assert classify(0, False, "429") is None
    assert classify(1, False, "Error: rate limit reached") == "rate_limit"
    assert classify(1, False, "HTTP 503 Service Unavailable") == "outage"
    assert classify(1, False, "Traceback") == "crash"
    assert classify(None, True, "") is None
