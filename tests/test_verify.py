import subprocess

import pytest

from frontera.verify import added_text, changed_files, conflict_files, has_conflict_markers, path_allowed, verify


@pytest.mark.parametrize("path,patterns,ok", [
    ("tests/test_a.py", ["tests/**"], True),
    ("tests/sub/test_a.py", ["tests/**"], True),
    ("src/a.py", ["tests/**"], False),
    ("scripts/probe_ssh.py", ["scripts/probe_ssh.py"], True),
    ("scripts/other.py", ["scripts/probe_ssh.py"], False),
    ("docs/a.md", ["docs/*.md"], True),
    ("docs/x/a.md", ["docs/*.md"], False),
    ("a/b/c.py", ["**/c.py"], True),
    ("c.py", ["**/c.py"], True),
    (".github/workflows/x.yml", ["./.github/workflows/*.yml"], True),
])
def test_globs(path, patterns, ok):
    assert path_allowed(path, patterns) is ok


def _commit_all(repo):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "init"], check=True)


def test_changed_files_and_added_text(repo):
    (repo / "notes.md").write_text("server 10.1.2.3 already here\n")
    _commit_all(repo)
    (repo / "notes.md").write_text("server 10.1.2.3 already here\nnew line\n")
    (repo / "new.txt").write_text("fresh\n")
    (repo / ".frontera").mkdir()
    (repo / ".frontera" / "result.json").write_text("{}")
    (repo / "src" / "__pycache__").mkdir(parents=True)
    (repo / "src" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"x")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "v").write_text("x")
    assert changed_files(repo) == ["new.txt", "notes.md"]
    text = added_text(repo, ["new.txt", "notes.md"])
    assert text["notes.md"] == "new line" and text["new.txt"] == "fresh\n"


def test_verify_scope_scan_and_check(repo):
    _commit_all(repo)
    (repo / "src").mkdir()
    (repo / "src" / "ok.py").write_text("x = 1\n")
    (repo / "other.py").write_text("key = 'ghp_" + "a" * 36 + "'\n")
    v = verify(repo, ["src/**"], (), "exit 0")
    assert v.outside == ["other.py"] and v.findings and v.check_ok is True and not v.scope_ok
    assert verify(repo, ["**"], (), "exit 3").check_ok is False


def test_check_command_gets_scrubbed_env(repo, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "should-not-leak")
    v = verify(repo, ["**"], (), 'test -z "$GH_TOKEN"')
    assert v.check_ok is True


def test_has_conflict_markers():
    assert has_conflict_markers("<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> main\n")
    assert has_conflict_markers("a line\n=======\n")
    assert has_conflict_markers(">>>>>>> origin/main\n")
    assert not has_conflict_markers("x = 1\n")
    assert not has_conflict_markers("a ====== b\n")   # not at line start
    assert not has_conflict_markers("======\n")       # 6 is not a marker; 7 is
    assert not has_conflict_markers("   =======\n")   # indented is not a marker


def test_conflict_files_flags_only_marked_files(repo):
    (repo / "clean.py").write_text("x = 1\n")
    (repo / "conflicted.py").write_text("<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> main\n")
    assert conflict_files(repo, ["clean.py", "conflicted.py"]) == ["conflicted.py"]


def test_verify_reports_conflict_files(repo):
    (repo / "conflicted.py").write_text("<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> main\n")
    v = verify(repo, ["**"], (), None)
    assert v.conflict_files == ["conflicted.py"]
