import subprocess

import pytest

from frontera.verify import (added_text, changed_files, conflict_files, globs_overlap,
                             has_conflict_markers, path_allowed, paths_overlap, verify)


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


@pytest.mark.parametrize("a,b,overlap", [
    # literal vs literal: only the same file overlaps
    ("src/app.py", "src/app.py", True),
    ("src/app.py", "src/other.py", False),
    ("src/app.py", "docs/a.py", False),
    # literal vs glob: the glob matches the literal path
    ("src/app.py", "src/**", True),
    ("src/app.py", "src/*.py", True),
    ("src/app.py", "docs/**", False),
    ("src/app.py", "src/other/*.py", False),
    # nested directories: one leading directory path contains the other
    ("src/**", "src/sub/x.py", True),
    ("src/**", "src/sub/**", True),
    ("src/sub/**", "src/**", True),
    ("src/sub/**", "src/other/**", False),
    # disjoint paths
    ("src/**", "docs/**", False),
    ("src/**", "src2/**", False),  # no string-prefix false positive
    ("docs/*.md", "docs/a.md", True),
    ("**/c.py", "src/c.py", True),
])
def test_globs_overlap(a, b, overlap):
    assert globs_overlap(a, b) is overlap
    assert globs_overlap(b, a) is overlap


def test_paths_overlap():
    assert paths_overlap(["src/**"], ["docs/**", "src/app.py"])
    assert not paths_overlap(["src/**"], ["docs/**"])
    assert not paths_overlap([], ["src/**"])


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
    assert has_conflict_markers("<<<<<<< HEAD\nx = 1\n")
    assert has_conflict_markers(">>>>>>> origin/main\n")
    assert not has_conflict_markers("x = 1\n")
    assert not has_conflict_markers("a ====== b\n")   # not at line start
    assert not has_conflict_markers("======\n")       # 6 is not a marker; 7 is
    assert not has_conflict_markers("   =======\n")   # indented is not a marker
    assert not has_conflict_markers("Title\n=======\n")  # RST/setext underline, not a conflict


def test_conflict_files_flags_only_marked_files(repo):
    (repo / "clean.py").write_text("x = 1\n")
    (repo / "conflicted.py").write_text("<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> main\n")
    assert conflict_files(repo, ["clean.py", "conflicted.py"]) == ["conflicted.py"]


def test_verify_reports_conflict_files(repo):
    (repo / "conflicted.py").write_text("<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> main\n")
    v = verify(repo, ["**"], (), None)
    assert v.conflict_files == ["conflicted.py"]


def test_changed_files_scoped_to_worker_during_conflicted_merge(repo):
    (repo / "a.txt").write_text("base\n")
    _commit_all(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "side"], check=True, capture_output=True)
    (repo / "a.txt").write_text("side\n")
    (repo / "b.txt").write_text("from side\n")
    _commit_all(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-"], check=True, capture_output=True)
    (repo / "a.txt").write_text("base2\n")
    _commit_all(repo)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e",
                    "merge", "--no-edit", "-m", "m", "side"],
                   check=False, capture_output=True)  # a.txt conflicts; b.txt is staged by the merge
    assert changed_files(repo) == ["a.txt", "b.txt"]
    assert changed_files(repo, conflicts=["a.txt"]) == ["a.txt"]
    # The worker resolves the conflict and adds a file; the base branch's file stays out of scope.
    (repo / "a.txt").write_text("resolved\n")
    (repo / "d.txt").write_text("worker\n")
    assert changed_files(repo, conflicts=["a.txt"]) == ["a.txt", "d.txt"]
