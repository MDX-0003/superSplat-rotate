#!/usr/bin/env python3
"""
Pipeline v3 — JSON timeline, UE-only virtual camera, MP4 concat.

All virtual camera trajectories come from UE Sequence exports.
No bridge interpolation. A single JSON file defines the full timeline.

Usage:
  python tills/run_pipeline_v3.py --project 03 --timeline tills/timeline/tl_03_01.json

Timeline JSON schema:
{
  "fps": 60,
  "crf": 6,
  "resolution": "3840x2160",
  "segments": [
    { "type": "real",  "camera": 6,  "start": 1,  "end": 31 },
    { "type": "render", "seq": "seq_032" },
    { "type": "real",  "camera": 32, "start": 33, "end": 66 },
    { "type": "render", "seq": "seq_015" },
    { "type": "real",  "camera": 15, "start": 68, "end": 168 }
  ]
}

Directory layout:
  CameraData/<project>/
    timeline_02.json          # this file
    ue_seqs/                  # UE-exported camera JSONs
      seq_032.json
      seq_015.json
    renders/                  # SuperSplat MP4 exports (manual)
      seq_032.mp4
      seq_015.mp4
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from paths import project as proj_dir

SCRIPT_DIR = Path(__file__).resolve().parent


def step(name, cmd, shell=False):
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


def wait_for_files(proj, needed):
    """Pause until all required MP4 files exist in renders/."""
    missing = [n for n in needed if not (proj / "renders" / f"{n}.mp4").exists()]
    if not missing:
        return
    print(f"\n{'─'*60}")
    print(f"  MANUAL STEP: SuperSplat render video")
    print(f"  Missing MP4s in renders/:")
    for name in missing:
        ue_json = proj / "ue_seqs" / f"{name}.json"
        print(f"    {name}.mp4  ←  import {ue_json.name} → Replace Timeline → Render → Video")
    print(f"\n  After placing all MP4s, press ENTER to continue...")
    print(f"{'─'*60}")
    input()


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline v3: JSON timeline → extract → render → concat"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--timeline", required=True, help="Path to timeline JSON")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="Keep intermediate MP4/TS files and export all output frames as PNGs")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    python = sys.executable

    # ---- pre-clean: always remove intermediate files from prior runs ----
    for pattern in ["real_*.mp4", "*.ts"]:
        for f in proj.glob(pattern):
            f.unlink()
    output_frames_dir = proj / "output" / "frames"
    if output_frames_dir.is_dir():
        shutil.rmtree(output_frames_dir)

    # ---- load & validate timeline ---------------------------------------
    tl_path = Path(args.timeline)
    if not tl_path.is_absolute():
        tl_path = SCRIPT_DIR.parent / tl_path
    with open(tl_path, "r") as f:
        tl = json.load(f)

    fps = tl.get("fps", 60)
    crf = tl.get("crf", 6)
    resolution = tl.get("resolution", "3840x2160")
    segments = tl["segments"]

    for i, seg in enumerate(segments):
        t = seg["type"]
        if t not in ("real", "render"):
            print(f"ERROR: segment {i}: unknown type '{t}'")
            sys.exit(1)
        if t == "real":
            for k in ("camera", "start", "end"):
                if k not in seg:
                    print(f"ERROR: real segment {i}: missing '{k}'")
                    sys.exit(1)
            if seg["start"] > seg["end"]:
                print(f"ERROR: real segment {i}: start > end")
                sys.exit(1)
        if t == "render":
            if "seq" not in seg:
                print(f"ERROR: render segment {i}: missing 'seq'")
                sys.exit(1)

    render_names = sorted(set(seg["seq"] for seg in segments if seg["type"] == "render"))
    real_count = sum(1 for seg in segments if seg["type"] == "real")

    print(f"Timeline: {len(segments)} segments")
    print(f"  Real:   {real_count}")
    print(f"  Render: {len(render_names)} ({', '.join(render_names)})")
    print(f"  FPS: {fps}  CRF: {crf}  Resolution: {resolution}")

    # ---- save config ----------------------------------------------------
    config_path = proj / "config.json"
    if not config_path.exists() or args.force:
        with open(config_path, "w") as f:
            json.dump(tl, f, indent=2)
        print(f"Config saved → {config_path}")

    # ---- Step 1: colmap_bin_to_json ------------------------------------
    bin_dir = proj / "colmap_bins"
    cameras_json = proj / "cameras.json"
    if (bin_dir / "cameras.bin").exists():
        if args.force and cameras_json.exists():
            cameras_json.unlink()
        if not cameras_json.exists():
            step("1/5  colmap_bin_to_json",
                 [python, str(SCRIPT_DIR / "colmap_bin_to_json.py"),
                  "--project", args.project])
        else:
            print(f"\n  SKIP Step 1: {cameras_json} exists")
    else:
        print(f"\n  SKIP Step 1: no colmap_bins/ — "
              f"ensure {cameras_json.name} exists for GT reference")

    # ---- Step 2: extract real frames ------------------------------------
    anchor_dir = proj / "anchor_frames"
    source_dir = proj / tl.get("source", "raw_frames")

    if args.force and anchor_dir.is_dir():
        shutil.rmtree(anchor_dir)
    anchor_dir.mkdir(parents=True, exist_ok=True)

    real_ranges = {}  # seg_index_in_original → (output_start, output_end)

    if real_count > 0:
        print(f"\n  Extracting real frames from {source_dir}...")
        out_idx = 1
        for seg_idx, seg in enumerate(segments):
            if seg["type"] != "real":
                continue
            camera = seg["camera"]
            camera_file = f"{camera:03d}.jpg"
            seg_start_out = out_idx

            for frame_num in range(seg["start"], seg["end"] + 1):
                src = source_dir / f"{frame_num:04d}" / camera_file
                if src.exists():
                    shutil.copy2(src, anchor_dir / f"{out_idx:04d}.jpg")
                    out_idx += 1
                else:
                    print(f"  WARNING: {src} missing")

            count = out_idx - seg_start_out
            real_ranges[seg_idx] = (seg_start_out, out_idx - 1)
            print(f"  seg[{seg_idx}] cam{camera:03d} "
                  f"src {seg['start']}-{seg['end']}: "
                  f"{count} → {seg_start_out:04d}-{out_idx - 1:04d}")
        print(f"  Total real frames: {out_idx - 1}")
    else:
        print(f"\n  No real segments, skipping extraction")

    # ---- Step 3: validate UE seqs + wait for render MP4s ----------------
    ue_dir = proj / "ue_seqs"
    renders_dir = proj / "renders"
    ue_dir.mkdir(parents=True, exist_ok=True)
    renders_dir.mkdir(parents=True, exist_ok=True)

    missing_json = [n for n in render_names if not (ue_dir / f"{n}.json").exists()]
    if missing_json:
        print(f"\n  NOTE: missing UE JSONs in {ue_dir}/:")
        for n in missing_json:
            print(f"    {n}.json")
        print(f"  Place them before importing into SuperSplat.")

    wait_for_files(proj, render_names)

    # ---- Step 4: real JPGs → MP4 ---------------------------------------
    # merge consecutive real segments into single MP4s
    real_mp4_map = {}  # first_seg_idx_in_group → (mp4_name, mp4_path)
    i = 0
    while i < len(segments):
        if segments[i]["type"] != "real":
            i += 1
            continue
        j = i
        while j < len(segments) and segments[j]["type"] == "real":
            j += 1
        first_out = real_ranges[i][0]
        last_out = real_ranges[j - 1][1]
        first_src = segments[i]["start"]
        last_src = segments[j - 1]["end"]
        count = last_out - first_out + 1
        name = f"real_{first_src:04d}_{last_src:04d}"
        mp4_path = proj / f"{name}.mp4"

        if args.force or not mp4_path.exists():
            print(f"\n  Converting real group [{i}..{j-1}]: "
                  f"{count} JPGs → {name}.mp4")
            step(f"4  {name}",
                 (f'ffmpeg -y -framerate {fps} -start_number {first_out} '
                  f'-i "{anchor_dir}/%04d.jpg" -frames:v {count} '
                  f'-c:v libx264 -crf {crf} -preset slow -pix_fmt yuv420p '
                  f'-vsync cfr -r {fps} "{mp4_path}"'),
                 shell=True)
        else:
            print(f"\n  SKIP: {name}.mp4 exists")

        for g in range(i, j):
            real_mp4_map[g] = name
        i = j

    # ---- Step 5: TS concat → output.mp4 --------------------------------
    output_dir = proj / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # convert all to TS
    ts_map = {}  # canonical name → ts_path

    for name, mp4_path in [(n, proj / f"{n}.mp4") for n in set(real_mp4_map.values())]:
        ts_path = proj / f"{name}.ts"
        step(f"5  {name} → TS",
             (f'ffmpeg -y -i "{mp4_path}" -c copy '
              f'-bsf:v h264_mp4toannexb -f mpegts "{ts_path}"'),
             shell=True)
        ts_map[name] = ts_path

    for seq_name in render_names:
        ts_path = proj / f"{seq_name}.ts"
        step(f"5  {seq_name} → TS",
             (f'ffmpeg -y -i "{renders_dir / f"{seq_name}.mp4"}" -c copy '
              f'-bsf:v h264_mp4toannexb -f mpegts "{ts_path}"'),
             shell=True)
        ts_map[seq_name] = ts_path

    # build ordered TS list matching timeline
    ordered_ts = []
    for seg_idx, seg in enumerate(segments):
        if seg["type"] == "real":
            ordered_ts.append(ts_map[real_mp4_map[seg_idx]])
        else:
            ordered_ts.append(ts_map[seg["seq"]])

    concat_input = "|".join(str(p) for p in ordered_ts)
    output_mp4 = output_dir / "output.mp4"
    step("5  TS concat → output.mp4",
         (f'ffmpeg -y -i "concat:{concat_input}" -c copy '
          f'-fflags +genpts "{output_mp4}"'),
         shell=True)

    if args.debug:
        # keep intermediate files, export all output frames as PNGs
        output_frames_dir.mkdir(parents=True, exist_ok=True)
        step("D  output frames → PNG",
             (f'ffmpeg -y -i "{output_mp4}" '
              f'"{output_frames_dir}/frame_%04d.png"'),
             shell=True)
        print(f"\n  [DEBUG] Intermediate files kept:")
        for ts in proj.glob("*.ts"):
            print(f"    {ts}")
        for mp4 in proj.glob("real_*.mp4"):
            print(f"    {mp4}")
        print(f"  [DEBUG] Output frames: {output_frames_dir}")
        print(f"           ({len(list(output_frames_dir.glob('*.png')))} PNGs)")
    else:
        # cleanup intermediate files
        for ts in proj.glob("*.ts"):
            ts.unlink()
        for mp4 in proj.glob("real_*.mp4"):
            mp4.unlink()

    print(f"\n{'='*60}")
    print(f"  DONE.")
    print(f"    output:  {output_mp4}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
