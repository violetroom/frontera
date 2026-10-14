"""Local endpoint state: usage-limit cooling and active run counts.

Stored as JSON under the configured state dir. Step 3 will update it from real
runs; until then it is only read, so a missing file means "all endpoints idle".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class EndpointState:
    cooling_until: dict[str, datetime] = field(default_factory=dict)
    active: dict[str, int] = field(default_factory=dict)

    def is_cooling(self, endpoint: str, now: datetime | None = None) -> bool:
        until = self.cooling_until.get(endpoint)
        return until is not None and (now or datetime.now(timezone.utc)) < until

    def active_runs(self, endpoint: str) -> int:
        return self.active.get(endpoint, 0)

    @classmethod
    def load(cls, state_dir: Path) -> EndpointState:
        path = state_dir / "endpoints.json"
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        return cls(
            cooling_until={k: datetime.fromisoformat(v) for k, v in raw.get("cooling_until", {}).items()},
            active={k: int(v) for k, v in raw.get("active", {}).items()},
        )
