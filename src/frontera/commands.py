"""Owner commands posted as issue comments, e.g. ``/frontera run claude-pro``."""

from __future__ import annotations

from dataclasses import dataclass

VERBS = {"run", "review", "fix", "promote"}
OWNER_ROLES = {"admin", "maintain"}


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    verb: str
    endpoint: str | None = None


def parse_command(body: str) -> Command | None:
    """The first line starting with /frontera, or None if the comment has none."""
    for line in (body or "").splitlines():
        parts = line.strip().split()
        if not parts or parts[0] != "/frontera":
            continue
        if len(parts) < 2 or parts[1] not in VERBS:
            raise CommandError(f"usage: /frontera {{{'|'.join(sorted(VERBS))}}} [endpoint]")
        if len(parts) > 3 or (len(parts) == 3 and parts[1] != "run"):
            raise CommandError("only /frontera run takes an argument (an endpoint name)")
        return Command(parts[1], parts[2] if len(parts) == 3 else None)
    return None


def is_owner(role: str) -> bool:
    return role in OWNER_ROLES
