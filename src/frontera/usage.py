"""Run records and per-endpoint usage.

Every launch (coding, review, test) writes ``.frontera/runs/<execution>/launch.json``
with the endpoint, harness, kind, lane, wall-clock seconds, outcome and any usage the
harness reports. The ``frontera`` workflow uploads those records as ``frontera-runs-<run>``
artifacts; ``frontera usage`` downloads the recent ones with ``gh`` and prints one row
per endpoint and kind.
"""

from __future__ import annotations

import json
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .harness import LaunchResult

WORKFLOW = "frontera"              # workflow in the managed repo that runs agents
ARTIFACT_PREFIX = "frontera-runs"  # artifact per run: frontera-runs-<run id>


class UsageError(RuntimeError):
    pass


@dataclass
class UsageRow:
    endpoint: str
    kind: str
    runs: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    wall_total_s: float = 0.0
    wall_median_s: float | None = None
    claude_cost_usd: float | None = None
    claude_turns: int | None = None


def write_launch_record(run_dir: Path, *, execution_id: str, endpoint: str, harness: str,
                        kind: str, lane: str, wall_s: float, outcome: str,
                        res: LaunchResult) -> dict:
    """Write ``launch.json`` for one launch and return the record."""
    record = {
        "execution_id": execution_id,
        "endpoint": endpoint,
        "harness": harness,
        "kind": kind,
        "lane": lane,
        "wall_s": round(wall_s, 1),
        "outcome": outcome,
        "returncode": res.returncode,
        "timed_out": res.timed_out,
        "error_class": res.error_class,
        "usage": res.usage,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "launch.json").write_text(json.dumps(record, indent=2))
    return record


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_record(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def aggregate_runs(records: Path) -> list[UsageRow]:
    """Aggregate every ``launch.json`` under ``records`` into one row per endpoint and kind."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for path in sorted(Path(records).rglob("launch.json")):
        data = _read_record(path)
        if data is None:
            continue
        endpoint, kind = data.get("endpoint"), data.get("kind")
        if not isinstance(endpoint, str) or not isinstance(kind, str):
            continue
        groups.setdefault((endpoint, kind), []).append(data)

    rows: list[UsageRow] = []
    for (endpoint, kind) in sorted(groups):
        data = groups[(endpoint, kind)]
        outcomes: dict[str, int] = {}
        for d in data:
            outcome = d.get("outcome")
            key = outcome if isinstance(outcome, str) and outcome else "unknown"
            outcomes[key] = outcomes.get(key, 0) + 1
        walls = [d["wall_s"] for d in data if _is_number(d.get("wall_s"))]
        costs = [d["usage"]["total_cost_usd"] for d in data
                 if _is_number((d.get("usage") or {}).get("total_cost_usd"))]
        turns = [d["usage"]["num_turns"] for d in data
                 if _is_number((d.get("usage") or {}).get("num_turns"))]
        rows.append(UsageRow(
            endpoint=endpoint,
            kind=kind,
            runs=len(data),
            outcomes=outcomes,
            wall_total_s=float(sum(walls)),
            wall_median_s=statistics.median(walls) if walls else None,
            claude_cost_usd=float(sum(costs)) if costs else None,
            claude_turns=sum(turns) if turns else None,
        ))
    return rows


def format_rows(rows: list[UsageRow]) -> str:
    if not rows:
        return "no run records found"
    lines = [f"{'endpoint':<16} {'kind':<10} {'runs':>4}  {'outcomes':<28} {'wall_s total/median':>22}  claude cost/turns"]
    for r in rows:
        outcomes = ", ".join(f"{k}:{v}" for k, v in sorted(r.outcomes.items()))
        wall = f"{r.wall_total_s:.0f} / {r.wall_median_s:.0f}" if r.wall_median_s is not None else "-"
        claude = f"${r.claude_cost_usd:.2f} / {r.claude_turns} turns" if r.claude_cost_usd is not None else "-"
        lines.append(f"{r.endpoint:<16} {r.kind:<10} {r.runs:>4}  {outcomes:<28} {wall:>22}  {claude}")
    return "\n".join(lines)


def _parse_created(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_runs(repo: str, days: int, dest: Path, runner: Callable = subprocess.run) -> list[int]:
    """Download the ``frontera-runs-<run>`` artifacts of the last ``days`` days of ``frontera`` runs.

    Uses ``gh run list`` and ``gh run download``; ``runner`` is injectable so tests can
    exercise the command lines without a network. Returns the downloaded run ids."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    proc = runner(["gh", "run", "list", "-R", repo, "--workflow", WORKFLOW,
                   "--limit", "200", "--json", "databaseId,createdAt"],
                  capture_output=True, text=True)
    if proc.returncode != 0:
        raise UsageError(f"gh run list failed: {(proc.stderr or '').strip()}")
    try:
        runs = json.loads(proc.stdout or "[]")
    except ValueError:
        raise UsageError("gh run list did not return a JSON array") from None
    if not isinstance(runs, list):
        raise UsageError("gh run list did not return a JSON array")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    downloaded: list[int] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        created = _parse_created(run.get("createdAt"))
        if created is None or created < cutoff:
            continue
        run_id = run.get("databaseId")
        if run_id is None:
            continue
        proc = runner(["gh", "run", "download", str(run_id), "-R", repo,
                       "-D", str(dest), "--pattern", f"{ARTIFACT_PREFIX}-{run_id}"],
                      capture_output=True, text=True)
        if proc.returncode != 0:
            raise UsageError(f"gh run download {run_id} failed: {(proc.stderr or '').strip()}")
        downloaded.append(run_id)
    return downloaded
