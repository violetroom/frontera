import pytest

from frontera.tracker import LocalTracker, TrackerError
from conftest import write_ticket


def test_get_malformed_json_raises_tracker_error(tmp_path):
    (tmp_path / "SHI-1.json").write_text('{"key": "SHI-1", "project": ')
    tracker = LocalTracker(tmp_path)
    with pytest.raises(TrackerError, match="SHI-1.json"):
        tracker.get("SHI-1")


def test_list_malformed_json_raises_tracker_error(tmp_path):
    write_ticket(tmp_path)
    (tmp_path / "SHI-2.json").write_text("not json at all")
    tracker = LocalTracker(tmp_path)
    with pytest.raises(TrackerError, match="SHI-2.json"):
        tracker.list()


def test_get_and_list_still_work(tmp_path):
    write_ticket(tmp_path)
    tracker = LocalTracker(tmp_path)
    assert [t.key for t in tracker.list()] == ["SHI-1"]
    assert tracker.get("SHI-1").summary == "Fix a thing"
