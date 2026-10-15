"""Owner commands posted as comments, e.g. ``/frontera run claude-pro`` or ``/frontera review test approve``.

Grammar:
  /frontera run [endpoint] [review] [test] [approve]
  /frontera review|test|approve ...   (a chain of gates, run in the order written; approve last)
  /frontera fix
  /frontera promote
"""

from __future__ import annotations

from dataclasses import dataclass

CHAIN = ("review", "test", "approve")
SINGLE = ("fix", "promote")
OWNER_ROLES = {"admin", "maintain"}
USAGE = "usage: /frontera run [endpoint] [review] [test] [approve] | /frontera review|test|approve ... | /frontera fix | /frontera promote"


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    steps: tuple[str, ...]  # e.g. ("run", "review", "approve") or ("fix",)
    endpoint: str | None = None

    @property
    def verb(self) -> str:
        return self.steps[0]

    @property
    def chain(self) -> tuple[str, ...]:
        return tuple(s for s in self.steps if s in CHAIN)


def _parse(words: list[str]) -> Command:
    if not words:
        raise CommandError(USAGE)
    if words[0] in SINGLE:
        if len(words) != 1:
            raise CommandError(f"/frontera {words[0]} takes no arguments")
        return Command((words[0],))
    endpoint = None
    steps: list[str] = []
    rest = words
    if words[0] == "run":
        steps.append("run")
        rest = words[1:]
        if rest and rest[0] not in CHAIN:
            endpoint, rest = rest[0], rest[1:]
    for w in rest:
        if w not in CHAIN:
            raise CommandError(f"unknown step {w!r}. {USAGE}")
        if w in steps:
            raise CommandError(f"step {w!r} appears twice")
        if "approve" in steps:
            raise CommandError("approve must be the last step")
        steps.append(w)
    if not steps:
        raise CommandError(USAGE)
    return Command(tuple(steps), endpoint)


def parse_command(body: str) -> Command | None:
    """The first line starting with /frontera, or None if the comment has none."""
    for line in (body or "").splitlines():
        parts = line.strip().split()
        if parts and parts[0] == "/frontera":
            return _parse(parts[1:])
    return None


def is_owner(role: str) -> bool:
    return role in OWNER_ROLES
