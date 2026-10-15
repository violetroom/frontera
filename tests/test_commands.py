import pytest

from frontera.commands import Command, CommandError, is_owner, parse_command


def test_parse_commands():
    assert parse_command("thanks!") is None
    assert parse_command("/frontera run") == Command(("run",))
    assert parse_command("looks good\n/frontera run claude-pro\n") == Command(("run",), "claude-pro")
    assert parse_command("/frontera review") == Command(("review",))
    c = parse_command("/frontera review test approve")
    assert c.steps == ("review", "test", "approve") and c.chain == ("review", "test", "approve")
    c = parse_command("/frontera run qwen-n0 review approve")
    assert c.verb == "run" and c.endpoint == "qwen-n0" and c.chain == ("review", "approve")
    assert parse_command("/frontera approve") == Command(("approve",))
    assert parse_command("/frontera fix") == Command(("fix",))


@pytest.mark.parametrize("body", ["/frontera", "/frontera deploy", "/frontera review claude-pro", "/frontera run a b",
                                  "/frontera approve review", "/frontera review review", "/frontera fix now",
                                  "/frontera promote review"])
def test_bad_commands(body):
    with pytest.raises(CommandError):
        parse_command(body)


def test_owner_roles():
    assert is_owner("admin") and is_owner("maintain")
    assert not any(is_owner(r) for r in ("write", "triage", "read", "none"))
