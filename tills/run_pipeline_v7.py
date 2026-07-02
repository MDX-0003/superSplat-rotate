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
from collections import defaultdict
from pathlib import Path

# shared helpers (Playwright, presets, clip, path resolution)
from _shared import (
    ROOT, load_preset, parse_frame_dirname,
)

# distributed utilities (SSH, SCP, WorkerNode, ProgressDisplay)
from _distributed import (
    WorkerNode, load_workers,
    ssh_run, ssh_run_async,
    scp_send, scp_recv,
    validate_workers,
)

# fuse + render — complete reuse from v6
from run_pipeline_v6 import run_v6_fuse_interactive, async_main_v6


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
    raw_dir = proj_dir / "raw_images"
    if not raw_dir.is_dir():
        print(f"ERROR: raw_images directory not found: {raw_dir}")
        sys.exit(1)

    # ── Phase 1: scan & parse ──
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

    training_cfg = cfg.get("distributed", {}).get("training", {})

    # ── Process one sub_dir at a time ──
    for sub_dir, group in by_subdir.items():
        print(f"\n{'─'*60}")
        print(f"  sub_dir={sub_dir}  ({len(group)} total frame(s))")

        # ── Phase 2: differential detection ──
        new_frames: list[tuple[Path, str]] = []
        skipped = 0
        for fd, frame_id in group:
            ply_dst = proj_dir / f"{sub_dir}-{frame_id}.ply"
            if force or not ply_dst.exists():
                new_frames.append((fd, frame_id))
            else:
                skipped += 1

        print(f"  新帧: {len(new_frames)}, 已有 PLY 跳过: {skipped}")

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
        if simulate_local:
            print(f"\n  [Phase 2] simulate-local: 跳过帧分发 "
                  f"(所有 Worker 共享 data/{sub_dir}/)")
        else:
            print(f"\n  [Phase 2] 分发帧数据 → 各 Worker ...")
            for worker, chunk in zip(online, chunks):
                if not chunk:
                    continue
                worker_data = Path(worker.litegs_path) / "data" / sub_dir

                for fd, frame_id in chunk:
                    dst = worker_data / fd.name

                    if worker.is_host:
                        if force and dst.exists():
                            shutil.rmtree(dst)
                            print(f"    [host] force-clean {fd.name}")
                        if force or not dst.exists():
                            shutil.copytree(fd, dst, dirs_exist_ok=True)
                            print(f"    [host] {fd.name} → {dst}")
                        else:
                            print(f"    [host] SKIP {fd.name} (exists)")
                    else:
                        if force:
                            ssh_run(worker,
                                    f'if exist "{dst}" rmdir /s /q "{dst}"')
                        ssh_run(worker,
                                f'if not exist "{worker_data}" mkdir "{worker_data}"')
                        ok = scp_send(worker, fd,
                                      str(dst).replace("\\", "/"))
                        tag = "OK" if ok else "FAILED"
                        print(f"    [{worker.id}] {fd.name} → {tag}")

        # ── Phase 4: parallel training ──
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

            cmd = (
                f'cd /d "{worker.litegs_path}" && '
                f'uv run python batch_run.py '
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

        # ── Phase 6: collect results ──
        print(f"\n  [Phase 4] 回收训练结果 → CameraData ...")
        for worker, chunk, proc in processes:
            if not chunk:
                continue
            worker_results = Path(worker.litegs_path) / "results" / sub_dir

            for fd, frame_id in chunk:
                ply_name = f"{sub_dir}-{frame_id}.ply"
                remote_ply = worker_results / ply_name
                local_ply = proj_dir / ply_name

                if local_ply.exists() and not force:
                    print(f"    SKIP {ply_name} (exists)")
                    continue

                if worker.is_host:
                    if remote_ply.exists():
                        shutil.copy2(str(remote_ply), str(local_ply))
                        size_mb = local_ply.stat().st_size / 1024 ** 2
                        print(f"    [host] {ply_name} → OK ({size_mb:.1f} MB)")
                    else:
                        print(f"    [host] {ply_name} → NOT FOUND (训练可能失败)")
                else:
                    ok = scp_recv(worker,
                                  str(remote_ply).replace("\\", "/"),
                                  local_ply)
                    if ok:
                        size_mb = local_ply.stat().st_size / 1024 ** 2
                        print(f"    [{worker.id}] {ply_name} → OK ({size_mb:.1f} MB)")
                    else:
                        print(f"    [{worker.id}] {ply_name} → FAILED")

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
