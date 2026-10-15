"""Thin wrapper over ``gh api`` so GitHub calls share gh's auth and are easy to fake in tests."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Protocol


class GhError(RuntimeError):
    pass


class Gh(Protocol):
    def api(self, method: str, path: str, body: Any = None) -> Any: ...


class GhCli:
    def __init__(self, token: str | None = None):
        self.token = token  # None: use gh's ambient auth (GH_TOKEN / GITHUB_TOKEN)

    def api(self, method: str, path: str, body: Any = None) -> Any:
        cmd = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            cmd += ["--input", "-"]
        env = {**os.environ, "GH_TOKEN": self.token} if self.token else None
        proc = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                              capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise GhError(f"gh api {method} {path}: {proc.stderr.strip() or proc.stdout.strip()}")
        out = proc.stdout.strip()
        return json.loads(out) if out else None
