"""Tests for check_frame_ready() — dual-sampling frame directory stability check."""

import tempfile
import threading
import time
from pathlib import Path

from tills._distributed import check_frame_ready


def test_empty_dir_not_ready():
    """An empty directory should never be ready (count=0 != expected)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "120-2026-06-30-120849"
        d.mkdir()
        # expected_count=None, prefix=120 → requires count==120
        assert check_frame_ready(d, expected_count=None) is False


def test_count_mismatch_not_ready():
    """Directory with wrong file count (< prefix N) should not be ready."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "120-2026-06-30-120849"
        d.mkdir()
        for i in range(10):  # only 10, not 120
            (d / f"frame_{i:04d}.jpg").write_bytes(b"x" * 1000)
        assert check_frame_ready(d, expected_count=None) is False


def test_stable_dir_is_ready():
    """Directory with stable files matching prefix count should be ready."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "3-2026-06-30-120849"
        d.mkdir()
        for i in range(3):
            (d / f"frame_{i:04d}.jpg").write_bytes(b"x" * 1000)
        # stable_window=0: both samples taken instantly (files already stable)
        assert check_frame_ready(d, expected_count=None, stable_window=0) is True


def test_explicit_img_num_overrides_prefix():
    """Expected_count takes priority over directory name prefix."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "120-2026-06-30-120849"
        d.mkdir()
        for i in range(5):  # 5 files, prefix says 120
            (d / f"frame_{i:04d}.jpg").write_bytes(b"x" * 1000)
        # img_num=5 matches, even though prefix is 120
        assert check_frame_ready(d, expected_count=5, stable_window=0) is True
        # img_num=6 does NOT match, and prefix 120 also doesn't match
        assert check_frame_ready(d, expected_count=6, stable_window=0) is False


def test_file_growing_not_ready():
    """A file that changes size between samples should block readiness."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "1-2026-06-30-120849"
        d.mkdir()
        f = d / "img_0000.jpg"
        f.write_bytes(b"x" * 100)

        # Inject instability: modify file between samples
        def grow_file():
            time.sleep(0.1)  # wait for first sample
            f.write_bytes(b"x" * 200)  # grow during sleep window

        t = threading.Thread(target=grow_file)
        t.start()
        result = check_frame_ready(d, expected_count=None, stable_window=0.3)
        t.join()
        # File grew → not ready
        assert result is False


def test_no_prefix_fallback():
    """When dirname has no numeric prefix and no img_num, count check is skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "no-prefix-here"
        d.mkdir()
        for i in range(5):
            (d / f"img_{i:04d}.jpg").write_bytes(b"x" * 1000)
        # No prefix → can't determine expected count → only stability matters
        assert check_frame_ready(d, expected_count=None, stable_window=0) is True
