#!/usr/bin/env python3
"""
Pipeline v7 — distributed multi-machine training + fuse + render.

Distributes frames from ``raw_images/`` across multiple workers (host + remote
machines via SSH), runs LiteGS ``batch_run.py`` in parallel, collects results,
then reuses the existing v6 fuse / clip / render logic.

Usage:
  python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json
  python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --steps train
  python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --steps fuse
  python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --steps render
  python tills/run_pipeline_v7.py --config CameraData/03/pipeline.json --force

无参数	扫描全部 → PLY 存在跳过 / 不存在训
--frames A B	只考虑 A B 两帧 → PLY 存在跳过 / 不存在训
--frames A B --force	只考虑 A B 两帧 → 无论 PLY 是否存在都重训
--force 无 --frames	全部帧 → 无论 PLY 是否存在都重训
"""

import argparse
import asyncio
import json
import shutil
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# shared helpers (Playwright, presets, clip, path resolution)
from _shared import (
    ROOT, load_preset, parse_frame_dirname,
)

# distributed utilities (SSH, SCP, WorkerNode, ProgressDisplay)
from _distributed import (
    WorkerNode, load_workers, auto_detect_host,
    ssh_run, ssh_run_async,
    scp_send, scp_recv,
    validate_workers,
)

# fuse + render — complete reuse from v6
from run_pipeline_v6 import run_v6_fuse_interactive, async_main_v6


# ── helpers ────────────────────────────────────────────────────────────────────

def _copy_frames_to_worker(worker: WorkerNode,
                           chunk: list[tuple[Path, str]],
                           sub_dir: str, force: bool) -> tuple[str, list[tuple[str, bool]]]:
    """Copy a worker's assigned frames to its LiteGSWin data directory.

    Runs in a thread — one per worker — so host and remote copies happen
    concurrently instead of serially.  For remote workers, all frames are
    sent in a single SCP call (instead of one SCP per frame).

    Returns:
        (worker_id, [(frame_name, success), ...])
    """
    results: list[tuple[str, bool]] = []
    worker_data = Path(worker.litegs_path) / "data" / sub_dir
    frame_names = [fd.name for fd, _ in chunk]

    if worker.is_host:
        # Host: each frame is a local copytree; still serial within thread
        # but multiple workers' threads run concurrently.
        for fd, frame_id in chunk:
            dst = worker_data / fd.name
            try:
                if force and dst.exists():
                    shutil.rmtree(dst)
                if force or not dst.exists():
                    shutil.copytree(fd, dst, dirs_exist_ok=True)
                results.append((fd.name, True))
            except Exception as e:
                print(f"    [host] {fd.name} → ERROR: {e}")
                results.append((fd.name, False))
    else:
        # Remote: one batch SCP for ALL frames — single SSH handshake
        try:
            # Step 1: clean + ensure parent dir
            if force:
                for name in frame_names:
                    d = worker_data / name
                    ssh_run(worker,
                            f'if exist "{d}" rmdir /s /q "{d}"')
            ssh_run(worker,
                    f'if not exist "{worker_data}" mkdir "{worker_data}"')

            # Step 2: single SCP with all frame dirs as sources
            src_paths = [str(fd) for fd, _ in chunk]
            dst_base = str(worker_data).replace("\\", "/")
            ok = _scp_send_multi(worker, src_paths, dst_base)
            for name in frame_names:
                results.append((name, ok))
        except Exception as e:
            print(f"    [{worker.id}] batch SCP → ERROR: {e}")
            for name in frame_names:
                results.append((name, False))

    return worker.id, results


def _scp_send_multi(worker: WorkerNode, local_paths: list[str],
                    remote_dst: str) -> bool:
    """Send multiple files/dirs to a worker in a single SCP call.

    One SSH handshake instead of N, avoiding per-file connection overhead.
    ``remote_dst`` must already exist on the worker.
    """
    import subprocess as _sp
    remote_target = f"{worker.ssh_target}:{remote_dst}"
    scp_args = ["scp", "-r"]
    if worker.ssh_key_path:
        scp_args.extend(["-i", worker.ssh_key_path])
    if worker.ssh_port != 22:
        scp_args.extend(["-P", str(worker.ssh_port)])
    scp_args.extend(["-o", "StrictHostKeyChecking=accept-new"])
    scp_args.extend(["-o", "ConnectTimeout=10"])
    scp_args.extend(local_paths)
    scp_args.append(remote_target)

    try:
        result = _sp.run(scp_args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=600)
        return result.returncode == 0
    except Exception:
        return False


def _scp_recv_multi(worker: WorkerNode, remote_paths: list[str],
                    local_dst: Path) -> bool:
    """Pull multiple files from a worker in a single SCP call.

    The reverse of ``_scp_send_multi`` — one handshake for N files.
    """
    import subprocess as _sp
    remote_src = f"{worker.ssh_target}:"
    scp_args = ["scp", "-r"]
    if worker.ssh_key_path:
        scp_args.extend(["-i", worker.ssh_key_path])
    if worker.ssh_port != 22:
        scp_args.extend(["-P", str(worker.ssh_port)])
    scp_args.extend(["-o", "StrictHostKeyChecking=accept-new"])
    scp_args.extend(["-o", "ConnectTimeout=10"])
    # remote paths need the user@host: prefix
    scp_args.extend([f"{remote_src}{rp}" for rp in remote_paths])
    scp_args.append(str(local_dst))

    try:
        result = _sp.run(scp_args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=600)
        return result.returncode == 0
    except Exception:
        return False


# ── v7 distributed train ───────────────────────────────────────────────────────

def run_v7_train(cfg: dict, workers: list[WorkerNode], force: bool,
                 simulate_local: bool = False,
                 frames: list[str] | None = None):
    """Distributed training across multiple workers.

    Phases:
      1. Scan raw_images/ → parse frame dirnames → group by sub_dir (MMDD)
         If *frames* is given, only directories whose names are in the list
         are included (the rest are silently dropped).
      2. Differential detection: skip frames whose PLY already exists
      3. Distribute new frames to online workers via SCP / local copy
         (skipped in simulate-local mode — all frames share one data dir)
      4. Start batch_run.py on each worker in parallel
      5. Monitor progress via status files + terminal dashboard
      6. Collect results (PLYs + cameras.json) back to CameraData/<project>/

    When *simulate_local* is True, all workers are synthetic local nodes
    sharing the same ``litegs_path``.  Frame distribution (Phase 3) is
    skipped — the data already lives under ``data/<sub_dir>/``.  Each
    worker writes a unique status file so concurrent processes don't
    collide.
    """
    proj_dir = (ROOT / f"CameraData/{cfg['project']}").resolve()
    timing: dict[str, float] = {}  # phase_name → seconds
    _t0 = time.time()

    def _phase_begin(name: str) -> float:
        t = time.time()
        timing.setdefault(name, 0.0)
        return t

    def _phase_end(name: str, start: float) -> None:
        elapsed = time.time() - start
        timing[name] += elapsed
        print(f"  [{name}] {elapsed:.1f}s")

    raw_dir = proj_dir / "raw_images"
    if not raw_dir.is_dir():
        print(f"ERROR: raw_images directory not found: {raw_dir}")
        sys.exit(1)

    # ── Phase 1: scan & parse ──
    _p1 = _phase_begin("scan")
    frame_dirs = sorted(d for d in raw_dir.iterdir() if d.is_dir())
    if not frame_dirs:
        print(f"ERROR: no frame subdirectories found in {raw_dir}")
        sys.exit(1)

    # --frames whitelist: accepts full dirname OR frame_id (HHMMSS)
    if frames:
        frame_set = set(frames)
        matched: list[Path] = []
        for d in frame_dirs:
            if d.name in frame_set:
                matched.append(d)
            else:
                try:
                    _, fid = parse_frame_dirname(d.name)
                    if fid in frame_set:
                        matched.append(d)
                except ValueError:
                    pass
        if not matched:
            print(f"ERROR: --frames 指定的值未匹配到 raw_images/ 中任何帧目录")
            print(f"  支持完整目录名或 frame_id (HHMMSS)，例如: --frames 122221")
            sys.exit(1)
        # report which --frames values had no match (warning only)
        matched_names = {d.name for d in matched}
        matched_ids: set[str] = set()
        for d in matched:
            try:
                _, fid = parse_frame_dirname(d.name)
                matched_ids.add(fid)
            except ValueError:
                pass
        unmatched = frame_set - matched_names - matched_ids
        if unmatched:
            print(f"  WARNING: 以下 --frames 值未匹配到帧: {', '.join(sorted(unmatched))}")
        frame_dirs = sorted(set(matched))
        print(f"  --frames 过滤: {len(frame_dirs)} 帧 "
              f"({', '.join(d.name for d in frame_dirs)})")

    frames: list[tuple[Path, str, str]] = []  # (path, sub_dir, frame_id)
    for fd in frame_dirs:
        try:
            sub_dir, frame_id = parse_frame_dirname(fd.name)
        except ValueError as e:
            print(f"  WARNING: skipping '{fd.name}' — {e}")
            continue
        frames.append((fd, sub_dir, frame_id))
        print(f"    {fd.name}  →  sub_dir={sub_dir}  frame_id={frame_id}")

    if not frames:
        print("ERROR: no valid frame directories found")
        sys.exit(1)

    # group by sub_dir
    by_subdir: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    for fd, sub_dir, frame_id in frames:
        by_subdir[sub_dir].append((fd, frame_id))

    _phase_end("scan", _p1)
    training_cfg = cfg.get("distributed", {}).get("training", {})

    # ── Process one sub_dir at a time ──
    for sub_dir, group in by_subdir.items():
        print(f"\n{'─'*60}")
        print(f"  sub_dir={sub_dir}  ({len(group)} total frame(s))")

        # ── Phase 2: differential detection ──
        _p2 = _phase_begin("diff")
        new_frames: list[tuple[Path, str]] = []
        skipped = 0
        for fd, frame_id in group:
            ply_dst = proj_dir / f"{sub_dir}-{frame_id}.ply"
            if force or not ply_dst.exists():
                new_frames.append((fd, frame_id))
            else:
                skipped += 1

        print(f"  新帧: {len(new_frames)}, 已有 PLY 跳过: {skipped}")
        _phase_end("diff", _p2)

        if not new_frames:
            print(f"  sub_dir={sub_dir}: 所有帧均已训练，跳过")
            continue

        # ── Phase 3: distribute to online workers ──
        online = [w for w in workers if w.is_online]
        offline = [w for w in workers if not w.is_online and not w.is_host]
        if offline:
            print(f"  WARNING: {len(offline)} worker(s) offline: "
                  f"{', '.join(w.id for w in offline)}")
        if not online:
            print("ERROR: 没有可用的 Worker（包括主机）")
            sys.exit(1)

        print(f"  在线 Worker: {len(online)} ({', '.join(w.id for w in online)})")

        # round-robin distribution
        chunks: list[list[tuple[Path, str]]] = [[] for _ in online]
        for i, item in enumerate(new_frames):
            chunks[i % len(online)].append(item)

        for w, chunk in zip(online, chunks):
            if chunk:
                names = ", ".join(fd.name for fd, _ in chunk)
                print(f"    {w.id}: {len(chunk)} 帧 — {names}")
            else:
                print(f"    {w.id}: 0 帧 (闲置)")

        # copy frame data to each worker (skipped in simulate-local mode:
        # all workers share the same data/ directory on the host)
        _p3 = _phase_begin("distribute")
        if simulate_local:
            print(f"\n  [Phase 2] simulate-local: 跳过帧分发 "
                  f"(所有 Worker 共享 data/{sub_dir}/)")
        else:
            print(f"\n  [Phase 2] 分发帧数据 → 各 Worker (并行) ...")
            # ThreadPoolExecutor: host copy + all remote SCPs run concurrently
            with ThreadPoolExecutor(max_workers=len(online)) as executor:
                futures = {}
                for worker, chunk in zip(online, chunks):
                    if not chunk:
                        continue
                    fut = executor.submit(
                        _copy_frames_to_worker, worker, chunk, sub_dir, force,
                    )
                    futures[fut] = worker.id

                for fut in as_completed(futures):
                    wid = futures[fut]
                    try:
                        _wid, results = fut.result()
                        for fname, ok in results:
                            tag = "OK" if ok else "FAILED"
                            print(f"    [{_wid}] {fname} → {tag}")
                    except Exception as e:
                        print(f"    [{wid}] → ERROR: {e}")
        _phase_end("distribute", _p3)

        # ── Phase 4: parallel training ──
        _p4 = _phase_begin("train")
        print(f"\n  [Phase 3] 启动并行训练 ...")
        processes: list[tuple[WorkerNode, list, object]] = []

        # build extra args for run_LiteGS_pipeline.py (forwarded through batch_run)
        extra_parts: list[str] = []
        if training_cfg.get("iterations"):
            extra_parts.extend(["--iterations", str(training_cfg["iterations"])])
        if training_cfg.get("target_primitives"):
            extra_parts.extend(["--target_primitives", str(training_cfg["target_primitives"])])
        if training_cfg.get("frame_stride"):
            extra_parts.extend(["--frame_stride", str(training_cfg["frame_stride"])])
        extra_str = " ".join(extra_parts)
        force_flag = " --force" if force else ""

        for worker, chunk in zip(online, chunks):
            if not chunk:
                continue

            frame_names = [fd.name for fd, _ in chunk]
            # unique status file per worker to avoid concurrent write collisions
            if simulate_local:
                status_rel = f"results/{sub_dir}/_worker_status_{worker.id}.json"
            else:
                status_rel = f"results/{sub_dir}/_worker_status.json"

            # Use LiteGSWin's own venv python directly — NOT uv run.
            # uv run inherits VIRTUAL_ENV from the parent (supersplat's venv)
            # when the host uses shell=True, which breaks LiteGS imports.
            py = f'"{worker.litegs_path}\\.venv\\Scripts\\python.exe"'
            cmd = (
                f'cd /d "{worker.litegs_path}" && '
                f'{py} batch_run.py '
                f'--sub_dir {sub_dir} '
                f'--frames {" ".join(frame_names)} '
                f'--worker-status {status_rel}'
                f'{force_flag}'
            )
            if extra_str:
                cmd += f" {extra_str}"

            print(f"    [{worker.id}] 启动 ({len(chunk)} 帧)")
            proc = ssh_run_async(worker, cmd)
            processes.append((worker, chunk, proc))

        # ── Phase 5: stream raw stdout from every worker ──
        print(f"\n{'─'*60}")
        print(f"  [Phase 3] 训练进行中 (实时输出)...")
        print(f"{'─'*60}")

        def _stream_worker_output(wid: str, proc_obj):
            """Read lines from a worker's Popen stdout, print with prefix."""
            prefix = f"[{wid}]"
            try:
                for line in proc_obj.stdout:
                    line = line.rstrip("\n\r")
                    if line:
                        print(f"  {prefix} {line}", flush=True)
            except Exception:
                pass

        # start one daemon thread per worker to stream its output
        threads = []
        for worker, _chunk, proc in processes:
            t = threading.Thread(
                target=_stream_worker_output,
                args=(worker.id, proc),
                daemon=True,
            )
            t.start()
            threads.append(t)

        # block until every worker's training process exits
        interrupted = False
        try:
            for worker, _chunk, proc in processes:
                rc = proc.wait()
                if rc != 0:
                    print(f"  [{worker.id}] batch_run exited with code {rc}")
        except KeyboardInterrupt:
            interrupted = True
            print("\n  用户中断，等待 Worker 退出 ...")
            for _, _chunk, proc in processes:
                try:
                    proc.terminate()
                except Exception:
                    pass
            for _, _chunk, proc in processes:
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass

        _phase_end("train", _p4)

        # ── Phase 6: collect results ──
        _p5 = _phase_begin("collect")
        print(f"\n  [Phase 4] 回收训练结果 → CameraData ...")
        for worker, chunk, proc in processes:
            if not chunk:
                continue
            worker_results = Path(worker.litegs_path) / "results" / sub_dir

            # filter out PLYs that already exist on host (unless --force)
            to_collect: list[tuple[str, Path, Path]] = []  # (label, remote, local)
            for fd, frame_id in chunk:
                ply_name = f"{sub_dir}-{frame_id}.ply"
                remote_ply = worker_results / ply_name
                local_ply = proj_dir / ply_name
                if local_ply.exists() and not force:
                    print(f"    SKIP {ply_name} (exists)")
                    continue
                to_collect.append((ply_name, remote_ply, local_ply))

            if not to_collect:
                continue

            if worker.is_host:
                # Host: local copy (already fast)
                for label, remote_ply, local_ply in to_collect:
                    if remote_ply.exists():
                        shutil.copy2(str(remote_ply), str(local_ply))
                        size_mb = local_ply.stat().st_size / 1024 ** 2
                        print(f"    [host] {label} → OK ({size_mb:.1f} MB)")
                    else:
                        print(f"    [host] {label} → NOT FOUND (训练可能失败)")
            else:
                # Remote: batch all PLYs in one SCP call
                src_paths = [str(rp).replace("\\", "/") for _, rp, _ in to_collect]
                dst_base = f"{worker.ssh_target}:{str(proj_dir).replace(chr(92), '/')}/"
                ok = _scp_recv_multi(worker, src_paths, proj_dir)
                for label, _, local_ply in to_collect:
                    if ok and local_ply.exists():
                        size_mb = local_ply.stat().st_size / 1024 ** 2
                        print(f"    [{worker.id}] {label} → OK ({size_mb:.1f} MB)")
                    else:
                        print(f"    [{worker.id}] {label} → FAILED")

        # collect cameras.json from best available worker
        for worker in online:
            remote_cam = Path(worker.litegs_path) / "results" / sub_dir / "cameras.json"
            local_cam = proj_dir / "cameras.json"

            if worker.is_host:
                if remote_cam.exists():
                    shutil.copy2(str(remote_cam), str(local_cam))
                    print(f"  cameras.json → {local_cam}")
                    break
            else:
                ok = scp_recv(worker,
                              str(remote_cam).replace("\\", "/"),
                              local_cam)
                if ok:
                    print(f"  cameras.json → {local_cam}")
                    break
        else:
            print(f"  WARNING: cameras.json not found on any worker")

        if interrupted:
            print(f"\n  训练被中断（已完成帧的 PLY 已回收）。")
            print(f"  重新运行将自动跳过已完成帧。")
            sys.exit(1)

        _phase_end("collect", _p5)

    # ── timing summary ──
    timing["total"] = time.time() - _t0
    print(f"\n{'─'*60}")
    print(f"  各阶段耗时:")
    for name in ["scan", "diff", "distribute", "train", "collect"]:
        if name in timing:
            print(f"    {name:<14} {timing[name]:.1f}s")
    print(f"    {'total':<14} {timing['total']:.1f}s")
    print(f"{'─'*60}")

    # write timing JSON alongside pipeline.json
    timing_path = proj_dir / "v7_timing.json"
    timing_out = {
        "project": cfg["project"],
        "phases": {k: round(v, 3) for k, v in timing.items()},
    }
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump(timing_out, f, ensure_ascii=False, indent=2)
    print(f"  耗时记录 → {timing_path}")

    print(f"\n  v7 训练阶段完成。")
    print(f"  下一步: python tills/run_pipeline_v7.py "
          f"--config {cfg.get('_config_path', '')} --steps fuse")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pipeline v7: distributed multi-machine training + fuse + render"
    )
    parser.add_argument("--config", required=True,
                        help="Path to pipeline.json (e.g. CameraData/03/pipeline.json)")
    parser.add_argument("--steps", type=str, default=None,
                        help="Comma-separated steps: train,fuse,render (default: all)")
    parser.add_argument("--force", action="store_true",
                        help="Re-generate all intermediate outputs")
    parser.add_argument("--frames", nargs="*", default=None,
                        help="Only train these frames (space-separated). "
                             "Accepts full dirname (114-2026-06-30-122221) or "
                             "frame_id / HHMMSS (122221). "
                             "Filters raw_images/ before distribution.")
    parser.add_argument("--simulate-local", dest="simulate_local",
                        type=int, default=None, const=5, nargs="?",
                        metavar="N",
                        help="Simulate N local workers (default 5) — "
                             "no SSH, all processes run on the host. "
                             "Useful for testing distribution logic "
                             "without remote machines.")
    args_p = parser.parse_args()

    # ── load config ──
    config_path = Path(args_p.config)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_config_path"] = str(config_path)

    if "project" not in cfg:
        print("ERROR: Missing 'project' in config")
        sys.exit(1)
    if "preset" not in cfg:
        print("ERROR: Missing 'preset' in config (v7 requires preset reference)")
        sys.exit(1)

    proj_dir = (ROOT / f"CameraData/{cfg['project']}").resolve()
    preset = load_preset(cfg["preset"])
    valid_steps = {"train", "fuse", "render"}

    if args_p.steps:
        step_filter = set(s.strip() for s in args_p.steps.split(","))
        unknown = step_filter - valid_steps
        if unknown:
            print(f"ERROR: unknown steps: {unknown}")
            sys.exit(1)
    else:
        step_filter = valid_steps

    should = lambda name: name in step_filter

    print(f"Pipeline v7 — project: {cfg['project']}")
    print(f"  Steps: {step_filter}")
    print(f"  Preset: {cfg['preset']}")

    # ── train ─────────────────────────────────────────────────────────
    if should("train"):
        dist_cfg = cfg.get("distributed", {})
        if not dist_cfg.get("enabled", False):
            print("\nERROR: distributed.enabled is not true in pipeline.json")
            print("  v7 requires 'distributed': {'enabled': true, ...} in the config.")
            print("  For local training, use: python tills/run_pipeline_v6.py --config ...")
            sys.exit(1)

        if "litegs_path" not in cfg:
            print("ERROR: --steps train requires 'litegs_path' in pipeline.json")
            sys.exit(1)

        # ── simulate-local mode: create synthetic workers ──
        if args_p.simulate_local is not None:
            n = args_p.simulate_local
            print(f"\n  simulate-local: 创建 {n} 个本地 Worker (无 SSH, 无 SCP)")
            from _distributed import WorkerNode
            workers = []
            for i in range(n):
                workers.append(WorkerNode(
                    id=f"sim-{i}",
                    hostname="localhost",
                    ip="127.0.0.1",
                    is_host=True,
                    litegs_path=cfg["litegs_path"],
                    supersplat_path=str(ROOT),
                ))
            ids_str = " ".join(w.id for w in workers)
            print(f"  {ids_str}")
            run_v7_train(cfg, workers, args_p.force,
                         simulate_local=True, frames=args_p.frames)

        # ── real distributed mode ──
        else:
            workers_config = dist_cfg.get("workers_config", "workers.json")
            workers_config_path = (proj_dir / workers_config).resolve()
            if not workers_config_path.exists():
                print(f"ERROR: workers config not found: {workers_config_path}")
                sys.exit(1)

            workers = load_workers(workers_config_path)
            # auto-detect which worker is THIS machine → sets is_host = True
            auto_detect_host(workers)
            print(f"\n  加载了 {len(workers)} 个 Worker:")
            for w in workers:
                tag = " [HOST]" if w.is_host else ""
                print(f"    {w.id}: {w.ip} ({w.ssh_target}){tag}")

            # validate connectivity
            print(f"\n  验证 Worker 连通性 ...")
            results = validate_workers(workers)
            all_ok = True
            for wid, (ok, msg) in results.items():
                status = "OK" if ok else "FAIL"
                print(f"    {wid}: {status} — {msg}")
                if not ok:
                    all_ok = False

            if not all_ok:
                print("\nERROR: 部分 Worker 不可达，请检查网络和 SSH 配置。")
                print("  参考: Docs/PLAN/ssh-setup-guide.md")
                sys.exit(1)

            run_v7_train(cfg, workers, args_p.force,
                         frames=args_p.frames)

    # ── fuse ──────────────────────────────────────────────────────────
    if should("fuse"):
        run_v6_fuse_interactive(cfg, preset, args_p.force)

    # ── render ───────────────────────────────────────────────────────
    if should("render"):
        asyncio.run(async_main_v6(args_p, cfg))

    print(f"\n{'='*60}")
    print(f"  DONE.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
