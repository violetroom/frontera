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


# --- Tellico setup, remote control, env passthrough -------------------------------------------

from pathlib import Path

from frontera.harness import opencode_config, strip_jsonc

SETUP = Path(__file__).parent / "fixtures" / "tellico-setup"
SETUP_ENV = {**SRC, "TELLICO_SETUP_DIR": str(SETUP)}
RELAY = {"TELLICO_REMOTE_CONTROL": "1", "TELLICO_RELAY_URL": "https://gw.example/tellico",
         "TELLICO_RELAY_KEY_FILE": "/k", "TELLICO_RELAY_MACHINE": "actions repo"}


def qwen(**extra):
    p = {**QWEN.provider, "setup_dir_env": "TELLICO_SETUP_DIR", **extra}
    return Endpoint("qwen-27b", "opencode", Launch.AUTO, model=QWEN.model, provider=p)


def cfg_of(ep, env, kind="code", label=None, tmp_path=Path("/tmp")):
    s = LaunchSpec(ep, kind, "do it", tmp_path, 60, tmp_path / "log", label)
    cmd, e = build_command(s, env)
    return json.loads(e["OPENCODE_CONFIG_CONTENT"]), e


def test_setup_plugins_limits_and_model_block():
    cfg = opencode_config(qwen(), "code", SETUP_ENV)
    root = SETUP.resolve()
    assert cfg["plugin"] == [f"{root}/plugins/{n}" for n in ("loop-guard", "compaction-guard", "secret-guard", "disk-guard")]
    assert cfg["tool_output"] == {"max_lines": 400, "max_bytes": 20000}
    assert cfg["compaction"] == {"auto": True, "reserved": 8000}
    m = cfg["provider"]["tellico-0"]["models"]["qwen3.8-27b-gsq-iq3s"]
    assert m["variants"] == {"high": {"reasoningEffort": "high"}} and m["options"] == {"temperature": 0.6}
    assert m["name"] == "qwen3.8-27b-gsq-iq3s" and m["limit"] == {"context": 131072, "output": 32768}
    assert m["reasoning"] is True  # model_config applied on top


@pytest.mark.parametrize("kind", ["code", "review", "research"])
def test_setup_never_changes_provider_options_or_permissions(kind):
    plain = opencode_config(qwen(), kind, SRC)
    with_setup = opencode_config(qwen(), kind, SETUP_ENV)
    for key in ("autoupdate", "share", "permission"):
        assert with_setup[key] == plain[key]
    assert with_setup["provider"]["tellico-0"]["options"] == plain["provider"]["tellico-0"]["options"]


def test_without_setup_config_is_minimal(tmp_path):
    base = opencode_config(QWEN, "code", SRC)
    assert opencode_config(qwen(), "code", SRC) == base
    assert opencode_config(qwen(), "code", {**SRC, "TELLICO_SETUP_DIR": str(tmp_path / "missing")}) == base
    assert "plugin" not in base and "tool_output" not in base


def test_jsonc_comments_but_not_strings():
    text = '{"u": "https://x/y", /* c */ "a": 1, // tail\n "s": "a\\" // b /* c */"}'
    assert json.loads(strip_jsonc(text)) == {"u": "https://x/y", "a": 1, "s": 'a" // b /* c */'}


def test_malformed_setup_config_names_the_file(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "opencode.json").write_text('{"plugin": [')
    with pytest.raises(HarnessError, match="opencode.json"):
        opencode_config(qwen(), "code", {**SRC, "TELLICO_SETUP_DIR": str(tmp_path)})


RELAY_NAMES = {"TELLICO_REMOTE_CONTROL", "TELLICO_RELAY_URL", "TELLICO_RELAY_KEY_FILE", "TELLICO_RELAY_MACHINE"}


def test_remote_control_enabled(tmp_path):
    cfg, env = cfg_of(qwen(), {**SETUP_ENV, **RELAY}, label="SHI-1-x-a1", tmp_path=tmp_path)
    assert cfg["plugin"][-1] == f"{SETUP.resolve()}/plugins/remote-control"
    assert RELAY_NAMES <= set(env)
    assert env["TELLICO_RELAY_MACHINE"] == "actions repo SHI-1-x-a1"
    assert env["TELLICO_RELAY_URL"] == RELAY["TELLICO_RELAY_URL"]


@pytest.mark.parametrize("drop", [None, "TELLICO_REMOTE_CONTROL", "TELLICO_RELAY_URL", "TELLICO_RELAY_KEY_FILE"])
def test_remote_control_off_or_incomplete(drop, tmp_path):
    relay = {k: v for k, v in RELAY.items() if k != drop}
    if drop is None:
        relay["TELLICO_REMOTE_CONTROL"] = "0"
    cfg, env = cfg_of(qwen(), {**SETUP_ENV, **relay}, label="e1", tmp_path=tmp_path)
    assert not any(p.endswith("remote-control") for p in cfg["plugin"])
    assert not RELAY_NAMES & set(env)


def test_remote_control_needs_setup_dir(tmp_path):
    cfg, env = cfg_of(qwen(), {**SRC, **RELAY}, tmp_path=tmp_path)
    assert "plugin" not in cfg and not RELAY_NAMES & set(env)


def test_env_passthrough_only_listed_and_set(tmp_path):
    ep = qwen(env_passthrough=["TELLICO_LOOP_GUARD", "TELLICO_DISK_MIN_FREE"])
    src = {**SETUP_ENV, "TELLICO_LOOP_GUARD": "5", "OTHER_KEY": "x"}
    _, env = cfg_of(ep, src, tmp_path=tmp_path)
    assert set(env) == {"PATH", "HOME", "TELLICO_API_KEY", "OPENCODE_CONFIG_CONTENT", "TELLICO_LOOP_GUARD"}
    assert "GH_TOKEN" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_env_passthrough_refuses_dispatcher_credentials(tmp_path):
    with pytest.raises(HarnessError):
        cfg_of(qwen(env_passthrough=["GH_TOKEN"]), SETUP_ENV, tmp_path=tmp_path)
