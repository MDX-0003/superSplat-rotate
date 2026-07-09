#!/usr/bin/env python3
"""
SCP 传输速度测试 — 帧图片目录 + PLY 文件

用项目 05 的真实数据，测试主机 ↔ 副机之间的 SCP 往返耗时。

[1/4] SCP 帧目录 → 副机 ...
    耗时: 10.7s  (2.2 MB/s)

[2/4] SCP PLY → 副机 ...
    耗时: 18.6s  (3.8 MB/s)

[3/4] SCP 帧目录 ← 副机 ...
    耗时: 10.6s  (2.2 MB/s)

[4/4] SCP PLY ← 副机 ...
    耗时: 29.5s  (2.4 MB/s) 

"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── config ─────────────────────────────────────────────────────────────────────

REMOTE_IP = "172.28.52.135"
REMOTE_USER = "Administrator"
REMOTE_TARGET = f"{REMOTE_USER}@{REMOTE_IP}"
REMOTE_TMP = "C:/temp/v7_scp_test"         # 副机临时目录
LOCAL_TMP = Path("C:/temp/v7_scp_test")    # 本机临时目录 (收发都在这)

# 测试数据：项目 05 的 1 个帧目录 + 1 个 PLY
PROJ_DIR = Path("E:/work/26.7_SKNJ/supersplat/CameraData/05")
FRAME_DIR = PROJ_DIR / "raw_images/114-2026-06-30-122221"   # 120 张图, ~23 MB
PLY_FILE = PROJ_DIR / "0630-122221.ply"                     # ~70 MB

SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]


# ── helpers ────────────────────────────────────────────────────────────────────

def ssh(cmd: str) -> subprocess.CompletedProcess:
    """Run a command on the remote worker."""
    return subprocess.run(
        ["ssh", *SSH_OPTS, REMOTE_TARGET, cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )


def scp_put(local_path: Path, remote_path: str) -> float:
    """SCP a file or directory to remote.  Returns elapsed seconds."""
    t0 = time.perf_counter()
    result = subprocess.run(
        ["scp", "-r", *SSH_OPTS, str(local_path),
         f"{REMOTE_TARGET}:{remote_path}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    elapsed = time.perf_counter() - t0
    if result.returncode != 0:
        raise RuntimeError(f"SCP put failed: {result.stderr.strip()}")
    return elapsed


def scp_get(remote_path: str, local_path: Path) -> float:
    """SCP a file or directory from remote.  Returns elapsed seconds."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    result = subprocess.run(
        ["scp", "-r", *SSH_OPTS,
         f"{REMOTE_TARGET}:{remote_path}", str(local_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    elapsed = time.perf_counter() - t0
    if result.returncode != 0:
        raise RuntimeError(f"SCP get failed: {result.stderr.strip()}")
    return elapsed


def size_mb(p: Path) -> float:
    """Return total size of a file or directory in MB."""
    if p.is_file():
        return p.stat().st_size / 1024 / 1024
    total = 0
    for root, _, files in p.walk() if hasattr(p, 'walk') else _walk_fallback(p):
        for f in files:
            total += (Path(root) / f).stat().st_size
    return total / 1024 / 1024


def _walk_fallback(p: Path):
    import os
    for root, dirs, files in os.walk(str(p)):
        yield root, dirs, files


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  SCP 传输速度测试")
    print("=" * 60)

    # validate sources exist
    if not FRAME_DIR.is_dir():
        print(f"ERROR: frame dir not found: {FRAME_DIR}")
        sys.exit(1)
    if not PLY_FILE.is_file():
        print(f"ERROR: PLY not found: {PLY_FILE}")
        sys.exit(1)

    frame_mb = size_mb(FRAME_DIR)
    ply_mb = size_mb(PLY_FILE)
    file_count = sum(1 for _ in FRAME_DIR.rglob("*") if _.is_file())
    print(f"\n  帧目录: {FRAME_DIR.name}  ({file_count} files, {frame_mb:.1f} MB)")
    print(f"  PLY:     {PLY_FILE.name}  ({ply_mb:.1f} MB)")
    print(f"  副机:    {REMOTE_TARGET}")

    # clean up from previous runs
    print(f"\n  清理残留临时文件 ...")
    if LOCAL_TMP.exists():
        shutil.rmtree(LOCAL_TMP)
    ssh(f'if exist "{REMOTE_TMP}" rmdir /s /q "{REMOTE_TMP}"')

    results: list[tuple[str, float, float]] = []  # (label, seconds, MB)

    try:
        # ── Test 1: 帧目录 → 副机 ──
        print(f"\n  [1/4] SCP 帧目录 → 副机 ...")
        remote_frame = f"{REMOTE_TMP}/{FRAME_DIR.name}"
        ssh(f'if not exist "{REMOTE_TMP}" mkdir "{REMOTE_TMP}"')
        t = scp_put(FRAME_DIR, remote_frame)
        results.append(("帧 → 副机", t, frame_mb))
        print(f"        耗时: {t:.1f}s  ({frame_mb / t:.1f} MB/s)")

        # ── Test 2: PLY → 副机 ──
        print(f"\n  [2/4] SCP PLY → 副机 ...")
        remote_ply = f"{REMOTE_TMP}/{PLY_FILE.name}"
        t = scp_put(PLY_FILE, remote_ply)
        results.append(("PLY → 副机", t, ply_mb))
        print(f"        耗时: {t:.1f}s  ({ply_mb / t:.1f} MB/s)")

        # ── Test 3: 帧目录 ← 副机 ──
        print(f"\n  [3/4] SCP 帧目录 ← 副机 ...")
        LOCAL_TMP.mkdir(parents=True, exist_ok=True)
        t = scp_get(remote_frame, LOCAL_TMP / FRAME_DIR.name)
        results.append(("帧 ← 副机", t, frame_mb))
        print(f"        耗时: {t:.1f}s  ({frame_mb / t:.1f} MB/s)")

        # ── Test 4: PLY ← 副机 ──
        print(f"\n  [4/4] SCP PLY ← 副机 ...")
        t = scp_get(remote_ply, LOCAL_TMP / PLY_FILE.name)
        results.append(("PLY ← 副机", t, ply_mb))
        print(f"        耗时: {t:.1f}s  ({ply_mb / t:.1f} MB/s)")

    finally:
        # ── clean up temp files ──
        print(f"\n  清理临时文件 ...")
        if LOCAL_TMP.exists():
            shutil.rmtree(LOCAL_TMP)
            print(f"    已删除: {LOCAL_TMP}")
        r = ssh(f'if exist "{REMOTE_TMP}" rmdir /s /q "{REMOTE_TMP}"')
        if r.returncode == 0:
            print(f"    已删除: {REMOTE_TMP} (副机)")
        else:
            print(f"    WARNING: 副机清理失败: {r.stderr.strip()}")

    # ── summary ──
    print(f"\n{'─'*60}")
    print(f"  {'测试':<16} {'耗时':>8} {'大小':>10} {'速率':>10}")
    print(f"  {'─'*16} {'─'*8} {'─'*10} {'─'*10}")
    for label, seconds, mb in results:
        rate = mb / seconds if seconds > 0 else 0
        print(f"  {label:<16} {seconds:>7.1f}s {mb:>9.1f}MB {rate:>9.1f}MB/s")
    print(f"{'─'*60}")
    print(f"  SCP 连接建立开销 ≈ 单文件耗时 - 多文件总耗时/N")
    print(f"  批量传输改进: v7 把同一 Worker 的 N 次 SCP 合并为 1 次")
    print(f"{'─'*60}")


if __name__ == "__main__":
    main()
