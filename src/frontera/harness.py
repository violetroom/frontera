"""Launch a worker CLI for one endpoint, with a scrubbed environment and a hard timeout.

Each adapter turns (endpoint, kind, prompt) into a command line plus the only
environment variables the worker may see. Tokens for the tracker, other
providers and the runner are never passed through.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Endpoint

BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL", "CI")
_RATE = re.compile(r"rate.?limit|usage limit|\b429\b|quota exceeded|too many requests", re.I)
_OUTAGE = re.compile(r"\b50[234]\b|service unavailable|overloaded|ECONNREFUSED|ETIMEDOUT|connection reset", re.I)


class HarnessError(RuntimeError):
    pass


@dataclass
class LaunchSpec:
    endpoint: Endpoint
    kind: str  # code | research | review
    prompt: str
    cwd: Path
    timeout_s: int | None
    log_path: Path


@dataclass
class LaunchResult:
    returncode: int | None
    timed_out: bool
    output_tail: str
    usage: dict = field(default_factory=dict)
    error_class: str | None = None  # rate_limit | outage | crash


def scrubbed_env(extra: list[str], source: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    env = {k: source[k] for k in BASE_ENV if k in source}
    for name in extra:
        if name not in source:
            raise HarnessError(f"environment variable {name} is not set")
        env[name] = source[name]
    return env


def opencode_config(ep: Endpoint, kind: str, source: dict[str, str] | None = None) -> dict:
    """OpenCode config for an OpenAI-compatible endpoint described in ``endpoint.provider``."""
    source = os.environ if source is None else source
    p = ep.provider
    if not ep.model or "/" not in ep.model:
        raise HarnessError(f"{ep.name}: opencode model must be provider/model")
    pid, mid = ep.model.split("/", 1)
    base_env = p.get("base_url_env")
    if not base_env or base_env not in source:
        raise HarnessError(f"{ep.name}: base_url_env {base_env!r} is not set")
    base = source[base_env].rstrip("/") + p.get("base_url_suffix", "")
    model = {"name": mid, "limit": {"context": p.get("context", 131072), "output": p.get("output", 32768)}}
    model.update(p.get("model_config") or {})
    web = "allow" if kind == "research" else "deny"
    edit = "deny" if kind == "review" else "allow"
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "provider": {pid: {
            "npm": "@ai-sdk/openai-compatible",
            "name": pid,
            "options": {"baseURL": base, "apiKey": "{env:%s}" % p["api_key_env"],
                        "headerTimeout": 3600000, "chunkTimeout": 3600000},
            "models": {mid: model},
        }},
        "permission": {"edit": edit, "bash": "allow", "webfetch": web, "websearch": web},
    }


def build_command(spec: LaunchSpec, source: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    ep = spec.endpoint
    if ep.harness == "opencode":
        cfg = opencode_config(ep, spec.kind, source)
        env = scrubbed_env([ep.provider["api_key_env"]], source)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
        return ["opencode", "run", "--standalone", "--auto", "--model", ep.model, spec.prompt], env
    if ep.harness == "claude-code":
        tools = {
            "code": ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
            "research": ["Read", "Write", "Glob", "Grep", "WebSearch", "WebFetch"],
            "review": ["Read", "Glob", "Grep", "Write", "Bash(git diff:*)", "Bash(git log:*)"],
        }[spec.kind]
        cmd = ["claude", "-p", spec.prompt, "--output-format", "json",
               "--permission-mode", "acceptEdits", "--allowedTools", ",".join(tools)]
        if spec.kind != "research":
            cmd += ["--disallowedTools", "WebSearch,WebFetch"]
        if ep.model and ep.model != "default":
            cmd += ["--model", ep.model]
        return cmd, scrubbed_env([ep.provider.get("token_env", "CLAUDE_CODE_OAUTH_TOKEN")], source)
    raise HarnessError(f"{ep.name}: harness {ep.harness!r} has no adapter yet")


def classify(returncode: int | None, timed_out: bool, output: str) -> str | None:
    if timed_out or returncode == 0:
        return None
    if _RATE.search(output):
        return "rate_limit"
    if _OUTAGE.search(output):
        return "outage"
    return "crash"


def _claude_usage(output: str) -> dict:
    try:
        data = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return {k: data[k] for k in ("total_cost_usd", "num_turns", "duration_ms", "usage", "is_error") if k in data}


def launch(spec: LaunchSpec) -> LaunchResult:
    cmd, env = build_command(spec)
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with open(spec.log_path, "w") as log:
        proc = subprocess.Popen(cmd, cwd=spec.cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            proc.wait(timeout=spec.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                    proc.wait(timeout=20)
                    break
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue
    output = spec.log_path.read_text(errors="replace")
    usage = _claude_usage(output) if spec.endpoint.harness == "claude-code" else {}
    return LaunchResult(proc.returncode, timed_out, output[-4000:], usage,
                        classify(proc.returncode, timed_out, output[-4000:]))
