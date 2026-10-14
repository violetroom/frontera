import pytest

from frontera.commands import Command, CommandError, is_owner, parse_command


def test_parse_commands():
    assert parse_command("thanks!") is None
    assert parse_command("/frontera run") == Command("run")
    assert parse_command("looks good\n/frontera run claude-pro\n") == Command("run", "claude-pro")
    assert parse_command("/frontera review") == Command("review")


@pytest.mark.parametrize("body", ["/frontera", "/frontera deploy", "/frontera review claude-pro", "/frontera run a b"])
def test_bad_commands(body):
    with pytest.raises(CommandError):
        parse_command(body)


def test_owner_roles():
    assert is_owner("admin") and is_owner("maintain")
    assert not any(is_owner(r) for r in ("write", "triage", "read", "none"))
