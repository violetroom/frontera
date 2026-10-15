import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from frontera.cli import main
from frontera.harness import LaunchResult
from frontera.usage import (
    UsageError,
    UsageRow,
    aggregate_runs,
    fetch_runs,
    format_rows,
    write_launch_record,
)


def record(base: Path, execution_id: str, endpoint: str, kind: str, **over):
    data = {
        "execution_id": execution_id, "endpoint": endpoint, "harness": "opencode",
        "kind": kind, "lane": "code/1", "wall_s": 10.0, "outcome": "success",
        "returncode": 0, "timed_out": False, "error_class": None, "usage": {},
    }
    data.update(over)
    d = base / ".frontera" / "runs" / execution_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "launch.json").write_text(json.dumps(data))
    return d


def test_aggregate_groups_by_endpoint_and_kind(tmp_path):
    record(tmp_path, "SHI-1-a1", "qwen-27b", "code", wall_s=10.0)
    record(tmp_path, "SHI-1-a2", "qwen-27b", "code", wall_s=30.0, outcome="retry",
           usage={"total_cost_usd": 0.25, "num_turns": 3})
    record(tmp_path, "SHI-1-review-x", "claude-pro", "review", harness="claude-code",
           wall_s=5.0, outcome="approve", usage={"total_cost_usd": 0.75, "num_turns": 1})
    rows = aggregate_runs(tmp_path)
    assert [(r.endpoint, r.kind) for r in rows] == [("claude-pro", "review"), ("qwen-27b", "code")]
    qwen = rows[1]
    assert qwen.runs == 2
    assert qwen.outcomes == {"success": 1, "retry": 1}
    assert qwen.wall_total_s == 40.0 and qwen.wall_median_s == 20.0
    assert qwen.claude_cost_usd == 0.25 and qwen.claude_turns == 3
    assert rows[0].runs == 1 and rows[0].outcomes == {"approve": 1}
    assert rows[0].claude_cost_usd == 0.75 and rows[0].claude_turns == 1


def test_aggregate_skips_malformed_records(tmp_path):
    bad = tmp_path / "bad" / "launch.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ not json")
    (tmp_path / "nokeys").mkdir()
    (tmp_path / "nokeys" / "launch.json").write_text(json.dumps({"wall_s": 1}))
    assert aggregate_runs(tmp_path) == []


def test_write_launch_record_round_trips(tmp_path):
    res = LaunchResult(0, False, "", {"total_cost_usd": 1.5, "num_turns": 4})
    write_launch_record(tmp_path / "runs" / "SHI-1-a1", execution_id="SHI-1-a1",
                        endpoint="claude-pro", harness="claude-code", kind="test",
                        lane="test", wall_s=12.34, outcome="pass", res=res)
    data = json.loads((tmp_path / "runs" / "SHI-1-a1" / "launch.json").read_text())
    assert data == {"execution_id": "SHI-1-a1", "endpoint": "claude-pro", "harness": "claude-code",
                    "kind": "test", "lane": "test", "wall_s": 12.3, "outcome": "pass",
                    "returncode": 0, "timed_out": False, "error_class": None,
                    "usage": {"total_cost_usd": 1.5, "num_turns": 4}}
    rows = aggregate_runs(tmp_path)
    assert rows[0].claude_cost_usd == 1.5 and rows[0].claude_turns == 4


class FakeGh:
    def __init__(self, runs):
        self.runs = runs
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(self.runs), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_fetch_runs_downloads_only_recent_runs(tmp_path):
    now = datetime.now(timezone.utc)
    stamp = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake = FakeGh([
        {"databaseId": 11, "createdAt": stamp(1)},
        {"databaseId": 12, "createdAt": stamp(10)},
        {"databaseId": 13, "createdAt": stamp(30)},
    ])
    dest = tmp_path / "downloads"
    assert fetch_runs("owner/name", 7, dest, runner=fake) == [11]
    listed = fake.calls[0]
    assert listed[:3] == ["gh", "run", "list"]
    assert "-R" in listed and "owner/name" in listed
    assert "--workflow" in listed and "frontera" in listed
    assert "--json" in listed and "databaseId,createdAt" in listed
    downloaded = fake.calls[1]
    assert downloaded[:3] == ["gh", "run", "download"]
    assert "11" in downloaded
    assert "-R" in downloaded and "owner/name" in downloaded
    assert "-D" in downloaded and str(dest) in downloaded
    assert "frontera-runs-11" in downloaded


def test_fetch_runs_raises_when_gh_fails(tmp_path):
    class Failing:
        def __call__(self, cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
    with pytest.raises(UsageError, match="boom"):
        fetch_runs("owner/name", 7, tmp_path / "dl", runner=Failing())


def test_format_rows_one_row_per_endpoint_and_kind():
    rows = [
        UsageRow(endpoint="claude-pro", kind="code", runs=2, outcomes={"success": 2},
                 wall_total_s=100.0, wall_median_s=50.0, claude_cost_usd=1.25, claude_turns=9),
        UsageRow(endpoint="qwen-27b", kind="code", runs=1, outcomes={"retry": 1},
                 wall_total_s=10.0, wall_median_s=10.0),
    ]
    lines = format_rows(rows).splitlines()
    assert len(lines) == 3  # header + one row per endpoint and kind
    assert "claude-pro" in lines[1] and "success:2" in lines[1]
    assert "$1.25" in lines[1] and "9 turns" in lines[1]
    assert "qwen-27b" in lines[2] and "retry:1" in lines[2]
    assert format_rows([]) == "no run records found"


def test_cli_usage_dir_skips_fetch(config_file, tmp_path, capsys):
    record(tmp_path / "records", "SHI-1-a1", "qwen-27b", "code")
    assert main(["--config", str(config_file), "usage", "--dir", str(tmp_path / "records")]) == 0
    out = capsys.readouterr().out
    assert "qwen-27b" in out and "code" in out and "success:1" in out


def test_cli_usage_fetches_then_aggregates(config_file, tmp_path, capsys, monkeypatch):
    import frontera.usage as usage
    calls = []

    def fake_fetch(repo, days, dest, runner=None):
        calls.append((repo, days))
        record(dest, "SHI-1-a1", "qwen-27b", "code")
        return [1]

    monkeypatch.setattr(usage, "fetch_runs", fake_fetch)
    assert main(["--config", str(config_file), "usage", "--repo", "owner/name", "--days", "3"]) == 0
    assert calls == [("owner/name", 3)]
    assert "qwen-27b" in capsys.readouterr().out
