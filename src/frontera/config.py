"""Load and validate the user config (projects, endpoints, lanes, privacy).

The config lives outside every repo (default ``~/.config/frontera/config.yaml``,
override with ``FRONTERA_CONFIG``) because it names private paths, endpoints and
accounts. ``config.example.yaml`` in the repo shows the shape with placeholders.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .models import Kind

DEFAULT_CONFIG_PATH = Path("~/.config/frontera/config.yaml")
HARNESSES = {"opencode", "claude-code", "codex", "api"}
REVIEW_LANE = "review"
TEST_LANE = "test"


class ConfigError(ValueError):
    pass


class Launch(StrEnum):
    AUTO = "auto"
    ON_COMMAND = "on_command"
    DISABLED = "disabled"


@dataclass(frozen=True)
class Endpoint:
    name: str
    harness: str
    launch: Launch
    model: str | None = None
    max_concurrent: int = 1
    max_runs_per_command: int | None = None
    projects: frozenset[str] | None = None  # None means every project
    provider: dict[str, Any] = field(default_factory=dict)

    def allows_project(self, project: str) -> bool:
        return self.projects is None or project in self.projects


@dataclass(frozen=True)
class Lane:
    name: str
    endpoints: tuple[str, ...]
    wall_clock_s: int | None = None
    attempts: int = 1
    promote_to: str | None = None
    fallback: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return self.name.split("/")[0]

    @property
    def tier(self) -> int | None:
        parts = self.name.split("/")
        return int(parts[1]) if len(parts) == 2 else None


@dataclass(frozen=True)
class Project:
    key: str
    repo: Path
    base_branch: str = "main"
    check_command: str | None = None
    research_dir: str = "docs/research"
    github_repo: str | None = None  # owner/name, for the github tracker


@dataclass(frozen=True)
class Config:
    tracker: dict[str, Any]
    state_dir: Path
    private_terms: tuple[str, ...]
    bot_logins: tuple[str, ...]
    projects: dict[str, Project]
    tiers: tuple[int, ...]
    endpoints: dict[str, Endpoint]
    lanes: dict[str, Lane]


_DURATION = re.compile(r"^(\d+)\s*([smh])$")


def parse_duration(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = _DURATION.match(str(value).strip())
    if not m:
        raise ConfigError(f"bad duration {value!r}; use e.g. 45m, 90s, 2h")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("FRONTERA_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    if not path.exists():
        raise ConfigError(f"no config at {path}; copy config.example.yaml there and edit it")
    raw = yaml.safe_load(path.read_text()) or {}
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    tiers = tuple(raw.get("tiers") or (1, 2, 3))

    projects = {}
    for key, p in (raw.get("projects") or {}).items():
        if "repo" not in p:
            raise ConfigError(f"project {key}: missing repo")
        projects[key] = Project(
            key=key,
            repo=_expand(p["repo"]).resolve(),
            base_branch=p.get("base_branch", "main"),
            check_command=p.get("check_command"),
            research_dir=p.get("research_dir", "docs/research"),
            github_repo=p.get("github_repo"),
        )

    endpoints = {}
    for name, e in (raw.get("endpoints") or {}).items():
        harness = e.get("harness")
        if harness not in HARNESSES:
            raise ConfigError(f"endpoint {name}: harness must be one of {sorted(HARNESSES)}")
        try:
            launch = Launch(e.get("launch", "disabled"))
        except ValueError:
            raise ConfigError(f"endpoint {name}: launch must be auto, on_command or disabled") from None
        projs = e.get("projects")
        endpoints[name] = Endpoint(
            name=name,
            harness=harness,
            launch=launch,
            model=e.get("model"),
            max_concurrent=int(e.get("max_concurrent", 1)),
            max_runs_per_command=e.get("max_runs_per_command"),
            projects=frozenset(projs) if projs is not None else None,
            provider=dict(e.get("provider") or {}),
        )

    lanes = {}
    for name, l in (raw.get("lanes") or {}).items():
        lanes[name] = Lane(
            name=name,
            endpoints=tuple(l.get("endpoints") or ()),
            wall_clock_s=parse_duration(l.get("wall_clock")),
            attempts=int(l.get("attempts", 1)),
            promote_to=l.get("promote_to"),
            fallback=tuple(l.get("fallback") or ()),
        )

    cfg = Config(
        tracker=dict(raw.get("tracker") or {"kind": "local"}),
        state_dir=_expand(raw.get("state_dir", "~/.local/state/frontera")),
        private_terms=_private_terms(raw.get("privacy") or {}),
        bot_logins=tuple((raw.get("github") or {}).get("bot_logins") or ("github-actions[bot]",)),
        projects=projects,
        tiers=tiers,
        endpoints=endpoints,
        lanes=lanes,
    )
    validate(cfg)
    return cfg


def _private_terms(privacy: dict[str, Any]) -> tuple[str, ...]:
    """Literal terms plus, for public config files, newline-separated terms from an env var (a secret)."""
    terms = list(privacy.get("private_terms") or ())
    env_name = privacy.get("private_terms_env")
    if env_name:
        terms += [t.strip() for t in os.environ.get(env_name, "").splitlines() if t.strip()]
    return tuple(terms)


def validate(cfg: Config) -> None:
    kinds = {k.value for k in Kind}
    for lane in cfg.lanes.values():
        if lane.name in (REVIEW_LANE, TEST_LANE):
            pass
        elif lane.kind not in kinds or lane.tier not in cfg.tiers:
            raise ConfigError(
                f"lane {lane.name}: name must be review, test or <kind>/<tier> with kind in "
                f"{sorted(kinds)} and tier in {list(cfg.tiers)}"
            )
        if not lane.endpoints:
            raise ConfigError(f"lane {lane.name}: needs at least one endpoint")
        for ep in lane.endpoints + lane.fallback:
            if ep not in cfg.endpoints:
                raise ConfigError(f"lane {lane.name}: unknown endpoint {ep}")
        if lane.promote_to is not None:
            target = cfg.lanes.get(lane.promote_to)
            if target is None:
                raise ConfigError(f"lane {lane.name}: promote_to unknown lane {lane.promote_to}")
            if target.kind != lane.kind or (target.tier or 0) <= (lane.tier or 0):
                raise ConfigError(
                    f"lane {lane.name}: promote_to must be a higher tier of the same kind"
                )
