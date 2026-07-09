"""Tests for cleanup_frame() — soft/hard multi-worker training artifact cleanup."""

import tempfile
from pathlib import Path

from tills._distributed import cleanup_frame, WorkerNode


def test_cleanup_paths_soft_level():
    """Verify soft cleanup deletes PLY but NOT raw_images."""
    with tempfile.TemporaryDirectory() as tmp:
        proj_dir = Path(tmp) / "CameraData" / "05"
        proj_dir.mkdir(parents=True)

        # Create file that should be deleted
        ply = proj_dir / "0703-120849.ply"
        ply.write_bytes(b"fake ply data")

        # Create raw_images that should NOT be deleted in soft mode
        raw_dir = proj_dir / "raw_images" / "120-2026-06-30-120849"
        raw_dir.mkdir(parents=True)
        (raw_dir / "img_0000.jpg").write_bytes(b"raw image")

        worker = WorkerNode(
            id="test-worker", hostname="localhost", ip="127.0.0.1",
            is_host=True,
            litegs_path=str(Path(tmp) / "LiteGSWin"),
            supersplat_path=str(Path(tmp) / "supersplat"),
        )

        # Soft cleanup
        results = cleanup_frame(worker, proj_dir, "0703", "120849",
                                level="soft", dry_run=True)

        # PLY should be in deletion list
        assert any("0703-120849.ply" in str(r) for r in results["deleted"]), \
            f"PLY not in deletion list: {results}"

        # raw_images should NOT be in deletion list
        assert not any("raw_images" in str(r) for r in results["deleted"]), \
            f"raw_images in soft deletion list: {results}"

        # raw_images should still exist (dry_run + soft)
        assert raw_dir.exists(), "raw_images should survive soft cleanup"


def test_cleanup_paths_hard_level():
    """Verify hard cleanup includes raw_images."""
    with tempfile.TemporaryDirectory() as tmp:
        proj_dir = Path(tmp) / "CameraData" / "05"
        proj_dir.mkdir(parents=True)
        raw_dir = proj_dir / "raw_images" / "120-2026-06-30-120849"
        raw_dir.mkdir(parents=True)
        (raw_dir / "img_0000.jpg").write_bytes(b"x")

        worker = WorkerNode(
            id="test-worker", hostname="localhost", ip="127.0.0.1",
            is_host=True,
            litegs_path=str(Path(tmp) / "LiteGSWin"),
            supersplat_path=str(Path(tmp) / "supersplat"),
        )

        results = cleanup_frame(worker, proj_dir, "0703", "120849",
                                level="hard", frame_dirname="120-2026-06-30-120849",
                                dry_run=True)

        # raw_images should be in deletion list
        assert any("raw_images" in str(r) for r in results["deleted"]), \
            f"raw_images NOT in hard deletion list: {results}"


def test_cleanup_nonexistent_files_no_error():
    """Deleting files that don't exist should not raise."""
    with tempfile.TemporaryDirectory() as tmp:
        proj_dir = Path(tmp) / "CameraData" / "05"
        proj_dir.mkdir(parents=True)
        # Don't create any PLY or raw_images — everything is missing

        worker = WorkerNode(
            id="test-worker", hostname="localhost", ip="127.0.0.1",
            is_host=True,
            litegs_path=str(Path(tmp) / "LiteGSWin"),
            supersplat_path=str(Path(tmp) / "supersplat"),
        )

        # Should not raise
        results = cleanup_frame(worker, proj_dir, "0703", "120849",
                                level="hard", dry_run=False)
        assert results["status"] == "ok"


def test_cleanup_actual_deletion():
    """Non-dry-run soft cleanup should actually delete files."""
    with tempfile.TemporaryDirectory() as tmp:
        proj_dir = Path(tmp) / "CameraData" / "05"
        proj_dir.mkdir(parents=True)

        ply = proj_dir / "0703-120849.ply"
        ply.write_bytes(b"fake ply data")
        assert ply.exists()

        # Also create worker results dir
        worker = WorkerNode(
            id="test-worker", hostname="localhost", ip="127.0.0.1",
            is_host=True,
            litegs_path=str(Path(tmp) / "LiteGSWin"),
            supersplat_path=str(Path(tmp) / "supersplat"),
        )
        worker_results = Path(worker.litegs_path) / "results" / "0703"
        worker_results.mkdir(parents=True)
        (worker_results / "0703-120849.ply").write_bytes(b"worker ply")

        results = cleanup_frame(worker, proj_dir, "0703", "120849",
                                level="soft", dry_run=False)

        assert results["status"] == "ok"
        # PLY should be gone
        assert not ply.exists(), f"PLY still exists: {ply}"
        # Worker results should be gone
        assert not worker_results.exists(), f"worker_results still exists: {worker_results}"
