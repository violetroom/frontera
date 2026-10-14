"""Parse and render the Agent task issue body (GitHub issue-form Markdown)."""

from __future__ import annotations

import re

NO_RESPONSE = "_No response_"
SECTIONS = ["Kind", "Tier", "Risk", "Description", "Acceptance criteria", "Context files", "Allowed paths"]
_HEADING = re.compile(r"^###\s+(.+?)\s*$", re.M)


def parse_sections(body: str) -> dict[str, str]:
    """Map lowercased ``### Heading`` names to their trimmed text ('' for no response)."""
    out: dict[str, str] = {}
    matches = list(_HEADING.finditer(body or ""))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[m.end():end].strip()
        out[m.group(1).strip().lower()] = "" if text == NO_RESPONSE else text
    return out


def parse_list(text: str) -> list[str]:
    items = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+(?:\[[ xX]\]\s+)?", "", line).strip().strip("`").strip()
        if line and line != NO_RESPONSE:
            items.append(line)
    return items


def render_body(kind: str, tier: int, description: str, criteria: list[str], context: list[str],
                allowed: list[str], risk: str = "low") -> str:
    """Body in the same shape the issue form produces, for issues created by tools (e.g. Hermes)."""
    def lst(xs):
        return "\n".join(f"- {x}" for x in xs) or NO_RESPONSE
    parts = {
        "Kind": kind, "Tier": str(tier), "Risk": risk, "Description": description or NO_RESPONSE,
        "Acceptance criteria": lst(criteria), "Context files": lst(context), "Allowed paths": lst(allowed),
    }
    return "\n\n".join(f"### {k}\n\n{v}" for k, v in parts.items()) + "\n"
