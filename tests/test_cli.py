import json

import yaml

from frontera import ticketing
from frontera.cli import EXIT_BLOCKED, main
from frontera.issueform import parse_list, parse_sections
from conftest import write_ticket


def run(config_file, *args):
    return main(["--config", str(config_file), *args])


def new_ticket_args(**over):
    spec = {
        "--title": "Fix a thing", "--kind": "code", "--tier": ["2"], "--risk": "low",
        "--description": "Do the thing.", "--criterion": ["It works"],
        "--context": ["AGENTS.md"], "--allowed": ["src/**"],
    }
    spec.update(over)
    argv = []
    for flag, value in spec.items():
        for v in value if isinstance(value, list) else [value]:
            argv += [flag, str(v)]
    return argv


def test_config_check(config_file, capsys):
    assert run(config_file, "config", "check") == 0
    assert "claude-pro" in capsys.readouterr().out


def test_next_shows_routes(config_file, tmp_path, capsys):
    write_ticket(tmp_path / "tickets")
    write_ticket(tmp_path / "tickets", key="SHI-2", tier=3, status="Awaiting Command")
    write_ticket(tmp_path / "tickets", key="SHI-3", status="Backlog")
    assert run(config_file, "next") == 0
    out = capsys.readouterr().out
    assert "SHI-1" in out and "qwen-27b" in out
    assert "SHI-2" in out and "awaiting_command" in out
    assert "SHI-3" not in out


def test_dry_run_writes_pack(config_file, tmp_path, capsys):
    write_ticket(tmp_path / "tickets", context_pack=["AGENTS.md", "docs"])
    out_dir = tmp_path / "pack"
    assert run(config_file, "run", "SHI-1", "--dry-run", "--out", str(out_dir)) == 0
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["endpoint"] == "qwen-27b" and manifest["branch"].startswith("agent/SHI-1-")
    assert manifest["context_bytes"] > 0
    prompt = (out_dir / "prompt.md").read_text()
    assert "SHI-1" in prompt and "$" not in prompt.replace("$$", "")
    assert "Fix a thing" in (out_dir / "ticket.md").read_text()


def test_dry_run_blocked_without_command(config_file, tmp_path, capsys):
    write_ticket(tmp_path / "tickets", tier=3, status="Awaiting Command")
    assert run(config_file, "run", "SHI-1", "--dry-run", "--out", str(tmp_path / "p")) == EXIT_BLOCKED
    assert "NEEDS_COMMAND" in capsys.readouterr().out
    assert run(config_file, "run", "SHI-1", "--dry-run", "--out", str(tmp_path / "p"),
               "--owner-command", "do the promoted tickets") == 0


def test_privacy_scan_blocks(config_file, tmp_path, capsys):
    write_ticket(tmp_path / "tickets", description="deploy to secret-host.example")
    assert run(config_file, "run", "SHI-1", "--dry-run", "--out", str(tmp_path / "p")) == EXIT_BLOCKED
    assert "private_term" in capsys.readouterr().out


def test_real_run_not_available_yet(config_file, tmp_path):
    write_ticket(tmp_path / "tickets")
    assert run(config_file, "run", "SHI-1") == EXIT_BLOCKED


def test_review_dry_run(config_file, tmp_path, capsys):
    write_ticket(tmp_path / "tickets", status="In Review", last_endpoint="qwen-27b")
    assert run(config_file, "review", "SHI-1", "--dry-run") == EXIT_BLOCKED
    assert run(config_file, "review", "SHI-1", "--dry-run", "--owner-command", "review the finished tickets") == 0
    assert "claude-pro" in capsys.readouterr().out


def test_status_shows_ticket(config_file, tmp_path, capsys):
    write_ticket(tmp_path / "tickets", current_tier=2, attempts=2, promotions=1,
                 last_endpoint="qwen-27b", pr_number=7, review_verdict="approve")
    assert run(config_file, "status", "SHI-1") == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines == [
        "key: SHI-1",
        "summary: Fix a thing",
        "status: Ready",
        "lane: code/2",
        "tier: 1",
        "attempts: 2",
        "promotions: 1",
        "last_endpoint: qwen-27b",
        "pr: 7",
        "review: approve",
    ]
    write_ticket(tmp_path / "tickets", key="SHI-2")
    assert run(config_file, "status", "SHI-2") == 0
    out2 = capsys.readouterr().out
    assert "last_endpoint: none" in out2.splitlines()
    assert "pr: none" in out2.splitlines()
    assert "review: none" in out2.splitlines()


def test_status_unknown_key(config_file, tmp_path, capsys):
    write_ticket(tmp_path / "tickets")
    assert run(config_file, "status", "SHI-9") == 1
    err = capsys.readouterr().err
    assert "SHI-9" in err


def test_missing_config(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "nope.yaml"), "next"]) == 1


def test_ticket_new_dry_run_round_trips(config_file, capsys):
    assert run(config_file, "ticket", "new", "--dry-run", *new_ticket_args()) == 0
    s = parse_sections(capsys.readouterr().out)
    assert s["kind"] == "code" and s["tier"] == "2" and s["risk"] == "low"
    assert s["description"] == "Do the thing."
    assert parse_list(s["acceptance criteria"]) == ["It works"]
    assert parse_list(s["context files"]) == ["AGENTS.md"]
    assert parse_list(s["allowed paths"]) == ["src/**"]


def test_ticket_new_requires_criterion(config_file, capsys):
    assert run(config_file, "ticket", "new", "--dry-run", *new_ticket_args(**{"--criterion": []})) == EXIT_BLOCKED
    assert "criterion" in capsys.readouterr().err


def test_ticket_new_code_needs_allowed_paths(config_file, capsys):
    assert run(config_file, "ticket", "new", "--dry-run", *new_ticket_args(**{"--allowed": []})) == EXIT_BLOCKED
    assert "allowed path" in capsys.readouterr().err


def test_ticket_new_rejects_protected_path(config_file, capsys):
    assert run(config_file, "ticket", "new", "--dry-run", *new_ticket_args(**{"--allowed": [".github/**"]})) == EXIT_BLOCKED
    assert ".github/**" in capsys.readouterr().err


def test_ticket_new_context_must_exist(config_file, capsys):
    assert run(config_file, "ticket", "new", "--dry-run", *new_ticket_args(**{"--context": ["missing.md"]})) == EXIT_BLOCKED
    assert "missing.md" in capsys.readouterr().err


def test_ticket_new_scan_blocks(config_file, capsys):
    assert run(config_file, "ticket", "new", "--dry-run",
               *new_ticket_args(**{"--description": "deploy to secret-host.example"})) == EXIT_BLOCKED
    assert "private_term" in capsys.readouterr().err


def test_ticket_new_unknown_project(config_file, capsys):
    assert run(config_file, "ticket", "new", "--dry-run", "--project", "NOPE", *new_ticket_args()) == EXIT_BLOCKED
    assert "NOPE" in capsys.readouterr().err


def test_ticket_new_creates_issue(raw_config, tmp_path, capsys, monkeypatch):
    raw_config["projects"]["SHI"]["github_repo"] = "owner/name"
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(raw_config))
    calls = []
    monkeypatch.setattr(ticketing, "create_issue",
                        lambda repo, title, body: calls.append((repo, title, body))
                        or "https://github.com/owner/name/issues/12")
    assert run(p, "ticket", "new", *new_ticket_args()) == 0
    assert "https://github.com/owner/name/issues/12" in capsys.readouterr().out
    assert calls and calls[0][0] == "owner/name" and calls[0][1] == "Fix a thing"
    assert "### Kind" in calls[0][2]


def test_ticket_new_validation_creates_nothing(raw_config, tmp_path, monkeypatch):
    raw_config["projects"]["SHI"]["github_repo"] = "owner/name"
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(raw_config))
    calls = []
    monkeypatch.setattr(ticketing, "create_issue",
                        lambda repo, title, body: calls.append(1) or "https://example.invalid")
    assert run(p, "ticket", "new", *new_ticket_args(**{"--context": ["missing.md"]})) == EXIT_BLOCKED
    assert calls == []


def test_ticket_new_without_github_repo(config_file, capsys):
    assert run(config_file, "ticket", "new", *new_ticket_args()) == EXIT_BLOCKED
    assert "github_repo" in capsys.readouterr().err
