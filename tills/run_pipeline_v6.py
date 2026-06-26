#!/usr/bin/env python3
"""
Pipeline v6 — multi-frame LiteGS training + interactive fuse + SuperSplat render.

Simplified variant of v5:
  - No concat / real-frame extraction (output is just the SuperSplat render MP4).
  - Raw frames are already in LiteGSWin-ready format: one subdirectory per frame
    under ``raw_images/`` (e.g. ``114-2026-06-25-162636/``).
  - All frames under ``raw_images/`` are trained, then result PLYs copied back.
  - Interactive fuse step: user confirms which PLY indices to merge.
  - Single-PLY fuse is a no-op (handled inside fuse_ply.py).

Usage:
  python tills/run_pipeline_v6.py --config CameraData/02/pipeline.json
  python tills/run_pipeline_v6.py --config CameraData/02/pipeline.json --steps train
  python tills/run_pipeline_v6.py --config CameraData/02/pipeline.json --steps fuse
  python tills/run_pipeline_v6.py --config CameraData/02/pipeline.json --steps render
"""
import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# import shared helpers
from _shared import (
    ROOT, TILLS_PLY_DIR,
    step, check_dev_server,
    load_preset, build_clip_args, parse_frame_dirname,
    _select_from_list, select_ply, select_json,
    ensure_browser, upload_ply, upload_json_file,
    verify_timeline, render_video,
)


# ── v6 train ───────────────────────────────────────────────────────────────────

def run_v6_train(cfg, preset, force):
    """Scan raw_images subdirectories → copy to LiteGS → batch_run → copy PLYs."""
    proj_dir = (ROOT / f"CameraData/{cfg['project']}").resolve()
    raw_dir = proj_dir / "raw_images"
    if not raw_dir.is_dir():
        print(f"ERROR: raw_images directory not found: {raw_dir}")
        sys.exit(1)

    litegs_path = Path(cfg["litegs_path"])
    if not litegs_path.is_dir():
        print(f"ERROR: litegs_path not found: {litegs_path}")
        sys.exit(1)

    # discover frame directories
    frame_dirs = sorted(d for d in raw_dir.iterdir() if d.is_dir())
    if not frame_dirs:
        print(f"ERROR: no frame subdirectories found in {raw_dir}")
        sys.exit(1)

    print(f"\n  Found {len(frame_dirs)} frame(s) in raw_images/:")

    # parse and group by sub_dir (e.g. "0625")
    frames = []
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
    from collections import defaultdict
    by_subdir = defaultdict(list)
    for fd, sub_dir, frame_id in frames:
        by_subdir[sub_dir].append((fd, frame_id))

    # T2-T4 per sub_dir group
    for sub_dir, group in by_subdir.items():
        print(f"\n{'─'*60}")
        print(f"  Processing sub_dir={sub_dir}  ({len(group)} frame(s))")

        # T2: copy raw_images frames → LiteGSWin/data/<sub_dir>/
        print(f"\n  T2: 复制帧素材 → LiteGSWin")
        for fd, frame_id in group:
            dst_frame = litegs_path / "data" / sub_dir / fd.name
            if force or not dst_frame.exists():
                print(f"    {fd.name} → {dst_frame}")
                shutil.copytree(fd, dst_frame, dirs_exist_ok=True)
            else:
                print(f"    SKIP: {dst_frame} already exists")

        # T3: batch_run
        any_missing = False
        for _, frame_id in group:
            ply_src = litegs_path / "results" / sub_dir / f"{sub_dir}-{frame_id}.ply"
            if force or not ply_src.exists():
                any_missing = True
                break

        if any_missing or force:
            step(f"T3  LiteGS batch_run --sub_dir {sub_dir}",
                 f"uv run python batch_run.py --sub_dir {sub_dir}",
                 shell=True, cwd=str(litegs_path))
        else:
            print(f"\n  T3: SKIP — all result PLYs already exist")

        # T4: copy cameras.json from LiteGS results → project root
        cameras_src = litegs_path / "results" / sub_dir / "cameras.json"
        cameras_dst = proj_dir / "cameras.json"
        if cameras_src.exists():
            shutil.copy2(cameras_src, cameras_dst)
            print(f"\n  T4: cameras.json → {cameras_dst}")
        else:
            print(f"\n  T4: WARNING — cameras.json not found at {cameras_src}")

        # T5: copy result PLYs → CameraData/<project>/
        print(f"\n  T5: 复制结果 PLY → CameraData")
        for _, frame_id in group:
            ply_src = litegs_path / "results" / sub_dir / f"{sub_dir}-{frame_id}.ply"
            ply_dst = proj_dir / f"{sub_dir}-{frame_id}.ply"
            if force or not ply_dst.exists():
                if ply_src.exists():
                    shutil.copy2(ply_src, ply_dst)
                    print(f"    {ply_src.name} → {ply_dst}")
                else:
                    print(f"    ERROR: source PLY not found: {ply_src}")
            else:
                print(f"    SKIP: {ply_dst} already exists")

    print(f"\n  v6 训练阶段完成 (T1-T5)。")
    print(f"  下一步: python tills/run_pipeline_v6.py --config {cfg.get('_config_path','')} --steps fuse")


# ── v6 interactive fuse ────────────────────────────────────────────────────────

def run_v6_fuse_interactive(cfg, preset, force):
    """List project PLYs, wait for user to pick indices, then fuse+clip."""
    proj_dir = (ROOT / f"CameraData/{cfg['project']}").resolve()

    # list all .ply files in project root
    ply_files = sorted(proj_dir.glob("*.ply"))
    if not ply_files:
        print(f"ERROR: no .ply files found in {proj_dir}")
        sys.exit(1)

    # build display list with 1-based idx + mtime (matches fuse_ply.py convention)
    print(f"\n{'─'*60}")
    print(f"  PLY files in {proj_dir.name}/:")
    for i, f in enumerate(ply_files):
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")
        size_mb = f.stat().st_size / 1024 ** 2
        print(f"    [{i+1}]  {f.name}  ({size_mb:.1f} MB)  {mtime}")
    print(f"{'─'*60}")

    # default indices from preset
    default_indices = preset.get("fuse", {}).get("indices", [1, 2])
    default_str = ", ".join(str(i) for i in default_indices)

    choice = input(f"  输入要合并的 PLY 编号 (逗号或空格分隔, 直接回车={default_str}): ").strip()

    if choice:
        # parse user input (1-based)
        indices = [int(x.strip()) for x in choice.replace(",", " ").split()]
    else:
        indices = default_indices
        print(f"  使用 preset 默认: {indices}")

    if len(indices) == 0:
        print("ERROR: 未指定任何 PLY")
        sys.exit(1)

    for idx in indices:
        if idx < 1 or idx > len(ply_files):
            print(f"ERROR: 编号 {idx} 超出范围 (1-{len(ply_files)})")
            sys.exit(1)

    print(f"\n  将合并以下 PLY (索引 {indices}):")
    for idx in indices:
        print(f"    {ply_files[idx-1].name}")

    input(f"\n  按 ENTER 开始 fuse+clip, Ctrl+C 取消...")

    # build fuse args — path comes from cfg project, not from preset
    fuse_script = TILLS_PLY_DIR / "fuse_ply.py"
    proj_path = f"CameraData/{cfg['project']}"
    max_index = preset.get("max_index", 89)
    f = preset.get("fuse", {})

    # fuse_ply.py expects 1-based indices (same as our display)
    fuse_args = [
        sys.executable, str(fuse_script),
        "--path", proj_path,
        "--max-index", str(max_index),
        "--radius-scale", str(f.get("radius_scale", 1.0)),
        "--height-up", str(f.get("height_up", 2)),
        "--height-down", str(f.get("height_down", 0.5)),
        "--indices", " ".join(str(i) for i in indices),
    ]
    if f.get("bias"):
        fuse_args.append("--bias")
        fuse_args.extend(["--bias-margin", str(f.get("bias_margin", 0.05))])
        fuse_args.extend(["--bias-radius-percentile", str(f.get("bias_radius_percentile", 50))])

    # fuse
    combine_plys = list(proj_dir.glob("*combine*.ply"))
    clean = str(combine_plys[0]) if (force and combine_plys) else None
    if clean:
        for c in combine_plys[1:]:
            c.unlink()
    step("fuse → combine PLYs", fuse_args, force_clean=clean)

    # clip — override preset path to target the current project
    clip_out = proj_dir.parent / f"{proj_dir.name}-clip"
    clean = str(clip_out) if (force and clip_out.is_dir()) else None
    clip_args = build_clip_args(preset, path_override=f"CameraData/{cfg['project']}")
    step("clip → XX-clip/*.ply", clip_args, force_clean=clean)

    print(f"\n  fuse+clip 完成。")
    print(f"  下一步: python tills/run_pipeline_v6.py --config {cfg.get('_config_path','')} --steps render")


# ── v6 render ──────────────────────────────────────────────────────────────────

async def async_main_v6(args, cfg):
    """Playwright steps: select PLY → upload → import JSON → render."""
    proj_name = cfg["project"]
    proj_dir = (ROOT / f"CameraData/{proj_name}").resolve()
    fps = cfg.get("fps", 25)

    # Step 4a: select PLY
    ply_path = select_ply(proj_dir)

    # Step 4b: select JSON (optional)
    json_path = None
    if "jsons_path" in cfg:
        json_path = select_json(cfg["jsons_path"])

    # expected output: project name + extension
    # v6 outputs directly to renders/<project>.mp4 (simple naming)
    expected_filename = f"{proj_name}.mp4"

    pw, browser, page = await ensure_browser()
    try:
        await upload_ply(page, ply_path)

        total_frames = 0
        if json_path:
            total_frames = await upload_json_file(page, json_path)
            if total_frames == 0:
                print("ERROR: JSON 导入失败")
                sys.exit(1)
        else:
            total_frames = await verify_timeline(page)

        renders_dir = proj_dir / "renders"
        renders_dir.mkdir(parents=True, exist_ok=True)
        success = await render_video(page, total_frames, renders_dir,
                                     expected_filename, fps)
        if not success:
            print(f"\n  渲染可能未完成，请检查 SuperSplat 页面")
    finally:
        try:
            await page.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pipeline v6: multi-frame LiteGS training + fuse + SuperSplat render"
    )
    parser.add_argument("--config", required=True,
                        help="Path to pipeline.json (e.g. CameraData/02/pipeline.json)")
    parser.add_argument("--steps", type=str, default=None,
                        help="Comma-separated steps: train,fuse,render (default: all)")
    parser.add_argument("--force", action="store_true",
                        help="Re-generate all intermediate outputs")
    args_p = parser.parse_args()

    # load config
    config_path = Path(args_p.config)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_config_path"] = str(config_path)  # stash for log messages

    if "project" not in cfg:
        print("ERROR: Missing 'project' in config")
        sys.exit(1)
    if "preset" not in cfg:
        print("ERROR: Missing 'preset' in config (v6 requires preset reference)")
        sys.exit(1)

    preset = load_preset(cfg["preset"])
    proj_name = cfg["project"]
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

    print(f"Pipeline v6 — project: {proj_name}")
    print(f"  Steps: {step_filter}")
    print(f"  Preset: {cfg['preset']}")

    # ── train ─────────────────────────────────────────────────────────
    if should("train"):
        if "litegs_path" not in cfg:
            print("ERROR: --steps train requires 'litegs_path' in pipeline.json")
            sys.exit(1)
        run_v6_train(cfg, preset, args_p.force)

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
