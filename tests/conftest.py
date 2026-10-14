import json
import subprocess
from pathlib import Path

import pytest
import yaml

from frontera.config import parse_config
from frontera.models import Kind, Status, Ticket

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "docs").mkdir(parents=True)
    (r / "AGENTS.md").write_text("# Agents\nRead the ticket.\n")
    (r / "docs" / "spec.md").write_text("Spec text.\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


@pytest.fixture
def raw_config(tmp_path, repo):
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["tracker"] = {"kind": "local", "path": str(tmp_path / "tickets")}
    raw["state_dir"] = str(tmp_path / "state")
    raw["projects"]["SHI"]["repo"] = str(repo)
    raw["privacy"] = {"private_terms": ["secret-host.example"]}
    return raw


@pytest.fixture
def cfg(raw_config):
    return parse_config(raw_config)


@pytest.fixture
def config_file(tmp_path, raw_config):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw_config))
    (tmp_path / "tickets").mkdir()
    return p


def make_ticket(**over) -> Ticket:
    base = dict(
        key="SHI-1", project="SHI", summary="Fix a thing", status=Status.READY, kind=Kind.CODE,
        tier=1, agent_eligible=True, acceptance_criteria=["It works"],
        context_pack=["AGENTS.md"], allowed_paths=["src/**"],
    )
    base.update(over)
    return Ticket(**base)


def write_ticket(dir_: Path, **fields):
    data = {
        "key": "SHI-1", "project": "SHI", "summary": "Fix a thing", "status": "Ready", "kind": "code",
        "tier": 1, "agent_eligible": True, "acceptance_criteria": ["It works"],
        "context_pack": ["AGENTS.md"], "allowed_paths": ["src/**"],
    }
    data.update(fields)
    (dir_ / f"{data['key']}.json").write_text(json.dumps(data))
