#!/usr/bin/env python3
"""
Pipeline v2 — UE trajectory + arc bridge, MP4 concat output.

Steps:
  1. colmap bin → cameras.json
  2. extract multi-camera segments
  3. bridge interpolation + UE seq → cameras_align.json
  4. [MANUAL] SuperSplat render video → wait for render.mp4
  5. head JPGs → head.ts, tail JPGs → tail.ts
  6. TS concat → output.mp4

Usage:
  python tills/run_pipeline_v2.py --project 02 \
      --gt-camera 006 --ue-seq SequenceData/01/cameras.json \
      --tail-gt-camera 032 \
      --head-segments "cam006:1-64" \
      --tail-segments "cam032:161-168"
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(SCRIPT_DIR))
from bridge_interpolate import (
    compute_center, detect_rotation_direction, generate_bridge,
)
from merge_trajectory import merge_trajectories
from paths import project as proj_dir


def step(name, cmd, shell=False):
    """Run a step, print header, abort on failure."""
    print(f"\n{'='*60}")
    print(f"  STEP: {name}")
    if shell:
        print(f"  CMD : {cmd}")
    else:
        print(f"  CMD : {' '.join(str(c) for c in cmd)}")
    print(f"{'='*60}")
    if shell:
        result = subprocess.run(cmd, shell=True)
    else:
        result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0:
        print(f"\n  FAILED at: {name}")
        sys.exit(1)


def wait_for_file(directory, filename, step_desc):
    """Pause until user has placed the required file."""
    target = directory / filename
    print(f"\n{'─'*60}")
    print(f"  MANUAL STEP: {step_desc}")
    print(f"  Expected: {target}")
    print(f"\n  After completing this step, press ENTER to continue...")
    print(f"{'─'*60}")
    input()

    if target.exists():
        print(f"  OK: {target} found")
    else:
        print(f"  WARNING: {target} not found, continuing anyway")
    return target


def parse_segments(spec):
    """Parse 'cam006:1-64' → {camera, start, end}."""
    import re
    m = re.match(r"^cam(\d+):(\d+)-(\d+)$", spec.strip())
    if not m:
        raise ValueError(f"Invalid segment: {spec}")
    return {
        "camera": int(m.group(1)),
        "start": int(m.group(2)),
        "end": int(m.group(3)),
    }


def extract_segment_frames(source_dir, out_dir, seg, start_output_idx):
    """Extract one segment's frames. Returns (output_start, output_end)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    camera_file = f"{seg['camera']:03d}.jpg"
    idx = start_output_idx

    for frame_num in range(seg["start"], seg["end"] + 1):
        frame_dir = source_dir / f"{frame_num:04d}"
        src = frame_dir / camera_file
        if not src.exists():
            print(f"  WARNING: {src} missing, skipping")
            continue
        dst = out_dir / f"{idx:04d}.jpg"
        shutil.copy2(src, dst)
        idx += 1

    count = idx - start_output_idx
    print(f"  cam{seg['camera']:03d} frames {seg['start']}-{seg['end']}: "
          f"{count} files → {start_output_idx:04d}-{idx - 1:04d}")
    return start_output_idx, idx - 1


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline v2: bin→json→extract→bridge+seq→mp4"
    )
    parser.add_argument("--project", required=True)

    # trajectory
    parser.add_argument("--gt-camera", required=True,
                        help="GT anchor camera img_name (e.g. '006')")
    parser.add_argument("--ue-seq", required=True,
                        help="Path to UE camera sequence JSON")
    parser.add_argument("--tail-gt-camera", type=str, default=None,
                        help="GT tail camera for bridge end (optional)")

    # real segments
    parser.add_argument("--head-segments", type=str, default=None,
                        help="Head real segments (e.g. 'cam006:1-64')")
    parser.add_argument("--tail-segments", type=str, default=None,
                        help="Tail real segments (e.g. 'cam032:161-168')")

    # options
    parser.add_argument("--bridge-min-frames", type=int, default=30)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--crf", type=int, default=6,
                        help="H.264 quality for JPG→MP4 conversion (0=lossless)")
    parser.add_argument("--resolution", type=str, default="3840x2160",
                        help="Output resolution WxH (must match SuperSplat export)")
    parser.add_argument("--source", type=str, default="raw_frames")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    python = sys.executable
    fps = args.fps
    crf = args.crf

    # ---- save config ----------------------------------------------------
    config = {
        "mode": "v2-mp4",
        "project": args.project,
        "gt_camera": args.gt_camera,
        "tail_gt_camera": args.tail_gt_camera,
        "ue_seq": args.ue_seq,
        "head_segments": args.head_segments,
        "tail_segments": args.tail_segments,
        "bridge_min_frames": args.bridge_min_frames,
        "fps": fps,
        "crf": crf,
        "resolution": args.resolution,
        "source": args.source,
    }
    config_path = proj / "config.json"
    if not config_path.exists() or args.force:
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Config saved → {config_path}")

    # ---- Step 1: bin → json --------------------------------------------
    bin_dir = proj / "colmap_bins"
    if not (bin_dir / "cameras.bin").exists():
        print(f"ERROR: {bin_dir / 'cameras.bin'} not found.")
        sys.exit(1)

    cameras_json = proj / "cameras.json"
    if args.force and cameras_json.exists():
        cameras_json.unlink()
    if not cameras_json.exists():
        step("1/6  colmap_bin_to_json",
             [python, str(SCRIPT_DIR / "colmap_bin_to_json.py"),
              "--project", args.project])
    else:
        print(f"\n  SKIP Step 1: {cameras_json} already exists  "
              f"(use --force to overwrite)")

    # ---- Step 2: extract real frame segments ---------------------------
    anchor_dir = proj / "anchor_frames"
    source_dir = proj / args.source

    if not source_dir.is_dir():
        print(f"ERROR: source not found: {source_dir}")
        sys.exit(1)

    if args.force and anchor_dir.is_dir():
        shutil.rmtree(anchor_dir)

    head_range = None
    tail_range = None

    if not anchor_dir.is_dir() or not list(anchor_dir.glob("*.jpg")):
        print(f"\n  Extracting real frames...")

        idx = 1
        if args.head_segments:
            for spec in args.head_segments.split(","):
                seg = parse_segments(spec)
                s, e = extract_segment_frames(source_dir, anchor_dir, seg, idx)
                head_range = (head_range[0] if head_range else s, e)
                idx = e + 1

        tail_start = idx
        if args.tail_segments:
            for spec in args.tail_segments.split(","):
                seg = parse_segments(spec)
                s, e = extract_segment_frames(source_dir, anchor_dir, seg, idx)
                tail_range = (tail_range[0] if tail_range else s, e)
                idx = e + 1

        total_real = idx - 1
        print(f"  Total real frames: {total_real}")
    else:
        print(f"\n  SKIP Step 2: {anchor_dir} already populated  "
              f"(use --force to overwrite)")
        # derive ranges from existing files for concat step
        files = sorted(anchor_dir.glob("*.jpg"))
        if files:
            import re
            nums = [int(re.search(r'(\d+)', f.stem).group(1)) for f in files]
            head_range = (1, nums[0] + len(nums) - 1)  # approximation
            # We need segments.json for accurate ranges. Parse --head-segments
            if args.head_segments:
                seg = parse_segments(args.head_segments.split(",")[0])
                head_range = (1, seg["end"] - seg["start"] + 1)
            if args.tail_segments and args.tail_segments.split(","):
                seg = parse_segments(args.tail_segments.split(",")[0])
                tail_start = head_range[1] + 1 if head_range else 1
                tail_range = (tail_start, tail_start + seg["end"] - seg["start"])

    # ---- Step 3: bridge + UE seq → cameras_align.json ------------------
    align_json = proj / "cameras_align.json"
    if args.force and align_json.exists():
        align_json.unlink()
    if not align_json.exists():
        with open(cameras_json, "r") as f:
            gt_cameras = json.load(f)

        center = compute_center(gt_cameras)
        print(f"\n  GT circle center: "
              f"[{center[0]:.4f}, {center[1]:.4f}, {center[2]:.4f}]")

        gt_anchor = next((c for c in gt_cameras if c["img_name"] == args.gt_camera), None)
        if gt_anchor is None:
            print(f"ERROR: GT camera '{args.gt_camera}' not found")
            sys.exit(1)

        ue_seq_path = Path(args.ue_seq)
        if not ue_seq_path.is_absolute():
            ue_seq_path = SCRIPT_DIR.parent / ue_seq_path
        with open(ue_seq_path, "r") as f:
            ue_sequence = json.load(f)
        print(f"  UE sequence: {len(ue_sequence)} frames")

        ue_positions = [p["position"] for p in ue_sequence]
        direction = detect_rotation_direction(ue_positions, center)
        print(f"  UE direction: {'CCW' if direction > 0 else 'CW'}")

        print(f"\n  --- Bridge 1: GT {args.gt_camera} → UE start ---")
        bridge1 = generate_bridge(
            anchor_pose=gt_anchor,
            target_pose=ue_sequence[0],
            center=center,
            direction=direction,
            min_frames=args.bridge_min_frames,
        )
        print(f"  Bridge1: {len(bridge1)} frames")

        bridge2 = None
        if args.tail_gt_camera:
            gt_tail = next((c for c in gt_cameras if c["img_name"] == args.tail_gt_camera), None)
            if gt_tail is None:
                print(f"ERROR: GT tail camera '{args.tail_gt_camera}' not found")
                sys.exit(1)
            # Tail bridge: always use the shortest arc. Unlike the head bridge
            # (which must match UE rotation direction for smooth entry), the
            # tail bridge is exiting the sequence — the shortest path feels
            # natural and avoids accidental full-circle detours.
            print(f"\n  --- Bridge 2: UE end → GT {args.tail_gt_camera} ---")
            bridge2 = generate_bridge(
                anchor_pose=ue_sequence[-1],
                target_pose=gt_tail,
                center=center,
                direction=0,  # 0 = shortest path
                min_frames=args.bridge_min_frames,
            )
            print(f"  Bridge2: {len(bridge2)} frames")

        merged = merge_trajectories(bridge1, ue_sequence, bridge2)
        with open(align_json, "w") as f:
            json.dump(merged, f, indent=2)
        print(f"\n  Merged: {len(merged)} frames → {align_json}")
    else:
        print(f"\n  SKIP Step 3: {align_json} already exists")

    # ---- Step 4: SuperSplat render video (MANUAL) ----------------------
    renders_dir = proj / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)

    with open(align_json, "r") as f:
        render_frames = len(json.load(f))
    render_duration_s = render_frames / fps
    print(f"\n  Render trajectory: {render_frames} frames, "
          f"{render_duration_s:.1f}s @ {fps}fps")

    wait_for_file(
        renders_dir, "render.mp4",
        f"1. Open SuperSplat\n"
        f"   2. Load your PLY model\n"
        f"   3. Import '{cameras_json.name}' → Add to GT Cameras\n"
        f"   4. Import '{align_json.name}' → Replace Timeline\n"
        f"   5. Menu → Render → Video\n"
        f"      Resolution: {args.resolution}\n"
        f"      Frame Rate: {fps}\n"
        f"      Format: MP4  Codec: H.264  Bitrate: High/Ultra\n"
        f"      Frame Range: 0–{render_frames - 1}\n"
        f"   6. Save as: render.mp4\n"
        f"   7. Move render.mp4 into:\n"
        f"      {renders_dir}\n"
        f"   8. Press ENTER here"
    )

    # ---- Step 5: JPG → MP4 (head + tail) -------------------------------
    head_mp4 = proj / "head.mp4"
    tail_mp4 = proj / "tail.mp4"

    if head_range and (args.force or not head_mp4.exists()):
        count = head_range[1] - head_range[0] + 1
        print(f"\n  Converting head: {count} JPGs → head.mp4")
        step("5a/6  head JPG→MP4",
             (f'ffmpeg -y -framerate {fps} -start_number {head_range[0]} '
              f'-i "{anchor_dir}/%04d.jpg" -frames:v {count} '
              f'-c:v libx264 -crf {crf} -preset slow -pix_fmt yuv420p '
              f'-vsync cfr -r {fps} "{head_mp4}"'),
             shell=True)
    else:
        if head_range:
            print(f"\n  SKIP Step 5a: {head_mp4} exists")
        else:
            print(f"\n  SKIP Step 5a: no head segments")

    if tail_range and (args.force or not tail_mp4.exists()):
        count = tail_range[1] - tail_range[0] + 1
        print(f"\n  Converting tail: {count} JPGs → tail.mp4")
        step("5b/6  tail JPG→MP4",
             (f'ffmpeg -y -framerate {fps} -start_number {tail_range[0]} '
              f'-i "{anchor_dir}/%04d.jpg" -frames:v {count} '
              f'-c:v libx264 -crf {crf} -preset slow -pix_fmt yuv420p '
              f'-vsync cfr -r {fps} "{tail_mp4}"'),
             shell=True)
    else:
        if tail_range:
            print(f"\n  SKIP Step 5b: {tail_mp4} exists")
        else:
            print(f"\n  SKIP Step 5b: no tail segments")

    # ---- Step 6: TS concat → output.mp4 --------------------------------
    render_mp4 = renders_dir / "render.mp4"
    output_dir = proj / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    head_ts = proj / "head.ts"
    tail_ts = proj / "tail.ts"
    render_ts = proj / "render.ts"
    output_mp4 = output_dir / "output.mp4"

    print(f"\n  Converting to TS and concatenating...")

    # head → TS
    if head_range:
        step("6a/6  head → TS",
             (f'ffmpeg -y -i "{head_mp4}" -c copy '
              f'-bsf:v h264_mp4toannexb -f mpegts "{head_ts}"'),
             shell=True)

    # render → TS
    step("6b/6  render → TS",
         (f'ffmpeg -y -i "{render_mp4}" -c copy '
          f'-bsf:v h264_mp4toannexb -f mpegts "{render_ts}"'),
         shell=True)

    # tail → TS
    if tail_range:
        step("6c/6  tail → TS",
             (f'ffmpeg -y -i "{tail_mp4}" -c copy '
              f'-bsf:v h264_mp4toannexb -f mpegts "{tail_ts}"'),
             shell=True)

    # concat
    ts_parts = []
    if head_range:
        ts_parts.append(str(head_ts))
    ts_parts.append(str(render_ts))
    if tail_range:
        ts_parts.append(str(tail_ts))

    concat_input = "|".join(ts_parts)
    step("6d/6  TS concat → output.mp4",
         (f'ffmpeg -y -i "concat:{concat_input}" -c copy '
          f'-fflags +genpts "{output_mp4}"'),
         shell=True)

    # cleanup intermediate files
    for tmp in [head_ts, render_ts, tail_ts, head_mp4, tail_mp4]:
        if tmp.exists():
            tmp.unlink()

    print(f"\n{'='*60}")
    print(f"  DONE.")
    print(f"    output:  {output_mp4}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
