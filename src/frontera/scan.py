"""Find secrets and private details in text leaving the machine or entering a PR.

Patterns are deliberately conservative; a finding blocks the run and goes to the
owner. ``private_terms`` from the user config covers what generic patterns
cannot know, such as real hostnames or account names.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "openai_style_key": re.compile(r"\bsk-(?:proj-|ant-|tellico-)?[A-Za-z0-9_\-]{20,}\b"),
    "atlassian_token": re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
}
_HOME_PATH = re.compile(r"(?:/Users|/home)/(?!<|\$|USER\b|you\b|me\b)[A-Za-z0-9._-]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# RFC 5737 documentation ranges and loopback are fine to mention.
_ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8", "0.0.0.0/32")]


@dataclass(frozen=True)
class Finding:
    rule: str
    source: str
    line: int
    excerpt: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.rule}: {self.excerpt}"


def _redact(s: str) -> str:
    s = s.strip()
    return s[:6] + "…" if len(s) > 8 else "…"


def _ip_flagged(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not any(ip in n for n in _ALLOWED_NETS)


def scan_text(text: str, source: str, private_terms: tuple[str, ...] = ()) -> list[Finding]:
    findings: list[Finding] = []
    lowered_terms = [t.lower() for t in private_terms if t]
    for n, line in enumerate(text.splitlines(), 1):
        for rule, pat in _SECRET_PATTERNS.items():
            for m in pat.finditer(line):
                findings.append(Finding(rule, source, n, _redact(m.group())))
        for m in _HOME_PATH.finditer(line):
            findings.append(Finding("home_path", source, n, m.group()))
        for m in _IPV4.finditer(line):
            if _ip_flagged(m.group()):
                findings.append(Finding("ip_address", source, n, m.group()))
        low = line.lower()
        for term in lowered_terms:
            if term in low:
                findings.append(Finding("private_term", source, n, _redact(term)))
    return findings
