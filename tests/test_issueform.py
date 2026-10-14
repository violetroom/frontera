from frontera.issueform import parse_list, parse_sections, render_body

FORM = """### Kind

code

### Tier

2

### Risk

_No response_

### Description

Make the probe report timeouts.

Second paragraph.

### Acceptance criteria

- [ ] A test covers the timeout path
- [x] Existing tests pass

### Context files

`AGENTS.md`
scripts/probe_ssh.py

### Allowed paths

_No response_
"""


def test_parse_issue_form_body():
    s = parse_sections(FORM)
    assert s["kind"] == "code" and s["tier"] == "2" and s["risk"] == ""
    assert s["description"] == "Make the probe report timeouts.\n\nSecond paragraph."
    assert parse_list(s["acceptance criteria"]) == ["A test covers the timeout path", "Existing tests pass"]
    assert parse_list(s["context files"]) == ["AGENTS.md", "scripts/probe_ssh.py"]
    assert parse_list(s["allowed paths"]) == []


def test_render_round_trip():
    body = render_body("research", 1, "Look into it", ["Answer X"], ["docs/a.md"], [], "medium")
    s = parse_sections(body)
    assert s["kind"] == "research" and s["tier"] == "1" and s["risk"] == "medium"
    assert parse_list(s["acceptance criteria"]) == ["Answer X"]
    assert parse_list(s["allowed paths"]) == []
