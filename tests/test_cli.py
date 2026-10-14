import json

from frontera.cli import EXIT_BLOCKED, main
from conftest import write_ticket


def run(config_file, *args):
    return main(["--config", str(config_file), *args])


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


def test_missing_config(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "nope.yaml"), "next"]) == 1
