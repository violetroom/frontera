"""Thin wrapper over ``gh api`` so GitHub calls share gh's auth and are easy to fake in tests."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol


class GhError(RuntimeError):
    pass


class Gh(Protocol):
    def api(self, method: str, path: str, body: Any = None) -> Any: ...


class GhCli:
    def api(self, method: str, path: str, body: Any = None) -> Any:
        cmd = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            cmd += ["--input", "-"]
        proc = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise GhError(f"gh api {method} {path}: {proc.stderr.strip() or proc.stdout.strip()}")
        out = proc.stdout.strip()
        return json.loads(out) if out else None
