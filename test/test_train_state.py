"""Tests for TrainState and FrameState data classes."""

from tills.server.train_daemon import TrainState, FrameState


def test_frame_state_defaults():
    """New FrameState should have sensible defaults."""
    fs = FrameState(frame_id="120849", sub_dir="0703",
                    dirname="120-2026-06-30-120849")
    assert fs.frame_id == "120849"
    assert fs.sub_dir == "0703"
    assert fs.dirname == "120-2026-06-30-120849"
    assert fs.status == "new"
    assert fs.worker_id == ""
    assert fs.iteration == 0
    assert fs.total_iterations == 0
    assert fs.error_message == ""
    assert fs.retry_count == 0


def test_train_state_add_frame():
    """Adding a frame should store it keyed by sub_dir-frame_id."""
    ts = TrainState(project="05", poll_interval=5)
    ts.add_frame("120849", "0703", "120-2026-06-30-120849")
    assert "0703-120849" in ts.frames
    assert ts.frames["0703-120849"].status == "new"


def test_train_state_add_frame_idempotent():
    """Adding the same frame twice should not create duplicates."""
    ts = TrainState(project="05", poll_interval=5)
    ts.add_frame("120849", "0703", "120-2026-06-30-120849")
    ts.add_frame("120849", "0703", "120-2026-06-30-120849")
    assert len(ts.frames) == 1


def test_train_state_update_frame():
    """update_frame should change only specified fields."""
    ts = TrainState(project="05", poll_interval=5)
    ts.add_frame("120849", "0703", "120-2026-06-30-120849")
    key = "0703-120849"

    ts.update_frame(key, status="ready")
    assert ts.frames[key].status == "ready"
    # Other fields unchanged
    assert ts.frames[key].worker_id == ""

    ts.update_frame(key, status="training", worker_id="worker1",
                    iteration=5000, total_iterations=30000)
    assert ts.frames[key].status == "training"
    assert ts.frames[key].worker_id == "worker1"
    assert ts.frames[key].iteration == 5000
    assert ts.frames[key].total_iterations == 30000


def test_train_state_update_nonexistent_frame():
    """Updating a nonexistent frame should not raise."""
    ts = TrainState(project="05", poll_interval=5)
    ts.update_frame("nonexistent", status="done")  # should not raise


def test_train_state_get_frame():
    """get_frame should return None for missing keys."""
    ts = TrainState(project="05", poll_interval=5)
    ts.add_frame("120849", "0703", "120-2026-06-30-120849")
    assert ts.get_frame("0703-120849") is not None
    assert ts.get_frame("nonexistent") is None


def test_train_state_to_dict():
    """to_dict should produce a JSON-serializable structure."""
    ts = TrainState(project="05", poll_interval=5)
    ts.add_frame("120849", "0703", "120-2026-06-30-120849")
    ts.update_frame("0703-120849", status="training", worker_id="host",
                    iteration=5000, total_iterations=30000)

    d = ts.to_dict()
    assert d["project"] == "05"
    assert d["poll_interval"] == 5
    assert len(d["frames"]) == 1
    assert d["frames"][0]["frame_id"] == "120849"
    assert d["frames"][0]["status"] == "training"
    assert d["frames"][0]["worker_id"] == "host"

    # Verify JSON serializable
    import json
    json_str = json.dumps(d)
    assert len(json_str) > 0


def test_train_state_multiple_frames_sorted():
    """Frames in to_dict should be sorted by key."""
    ts = TrainState(project="05", poll_interval=5)
    ts.add_frame("120851", "0703", "dir3")  # newest frame_id, last
    ts.add_frame("120849", "0703", "dir1")  # oldest frame_id, first
    ts.add_frame("120850", "0704", "dir2")  # different sub_dir

    d = ts.to_dict()
    keys = [f["key"] for f in d["frames"]]
    assert keys == sorted(keys), f"frames not sorted: {keys}"
