"""Check a worker's changes independently of what the worker claims."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .contextpack import PACK_DIR
from .harness import scrubbed_env
from .scan import Finding, scan_text


@lru_cache(maxsize=256)
def _glob_re(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip()
    if pattern.startswith("./"):
        pattern = pattern[2:]
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    body = "".join(out)
    if pattern.endswith("/"):
        body += ".*"
    return re.compile(body + r"\Z")


def path_allowed(path: str, patterns: list[str]) -> bool:
    return any(_glob_re(p).match(path) for p in patterns)


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


# Build and test by-products a worker's own check runs leave behind, even in repos without a .gitignore.
JUNK = ("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**", "**/.mypy_cache/**", "**/.ruff_cache/**",
        "**/node_modules/**", "**/.DS_Store")


def changed_files(repo: Path) -> list[str]:
    """Modified, added, deleted and untracked files, excluding the pack directory and build junk."""
    out = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    files, entries = [], out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        if not e:
            i += 1
            continue
        status, path = e[:2], e[3:]
        if status[0] in "RC":  # rename/copy: next entry is the source path
            i += 1
        if not (path == PACK_DIR or path.startswith(PACK_DIR + "/") or path_allowed(path, list(JUNK))):
            files.append(path)
        i += 1
    return sorted(set(files))


def added_text(repo: Path, files: list[str]) -> dict[str, str]:
    """Lines each change adds: diff additions for tracked files, whole content for new ones."""
    tracked = set(git(repo, "ls-files", "-z").split("\0"))
    out: dict[str, str] = {}
    for f in files:
        if f in tracked:
            diff = git(repo, "diff", "--unified=0", "HEAD", "--", f, check=False)
            out[f] = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        elif (repo / f).is_file():
            out[f] = (repo / f).read_bytes().decode(errors="replace")
    return out


@dataclass
class Verification:
    files: list[str]
    outside: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    check_ok: bool | None = None  # None: no check configured
    check_output: str = ""

    @property
    def scope_ok(self) -> bool:
        return not self.outside and not self.findings


def verify(repo: Path, allowed: list[str], private_terms: tuple[str, ...], check_command: str | None,
           timeout_s: int = 1200) -> Verification:
    files = changed_files(repo)
    v = Verification(files=files, outside=[f for f in files if not path_allowed(f, allowed)])
    for f, text in added_text(repo, files).items():
        v.findings += scan_text(text, f, private_terms)
    if check_command:
        try:
            # Agent-written code runs here, so it gets the same scrubbed environment as the agent.
            proc = subprocess.run(check_command, shell=True, cwd=repo, capture_output=True, text=True,
                                  timeout=timeout_s, env=scrubbed_env([]))
            v.check_ok = proc.returncode == 0
            v.check_output = (proc.stdout + proc.stderr)[-4000:]
        except subprocess.TimeoutExpired:
            v.check_ok, v.check_output = False, f"check timed out after {timeout_s}s"
    return v
