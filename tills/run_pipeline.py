#!/usr/bin/env python3
"""
Full pipeline orchestrator.

Steps:
  1. colmap bin → cameras.json
  2. extract anchor camera frames
  3. circle interpolation → cameras_align.json
  4. [MANUAL] SuperSplat render → wait for renders/
  5. blend real + rendered frames
  6. encode MP4

Usage:
  python tills/run_pipeline.py --project 02 --camera 6 --head 64 --tail 161
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from paths import project as proj_dir

SCRIPT_DIR = Path(__file__).resolve().parent


def step(name, cmd):
    """Run a step, print header, abort on failure."""
    print(f"\n{'='*60}")
    print(f"  STEP: {name}")
    print(f"  CMD : {' '.join(cmd)}")
    print(f"{'='*60}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n  FAILED at: {name}")
        sys.exit(1)


def wait_for_files(directory, pattern, count, step_desc):
    """Pause until user has placed the required files."""
    print(f"\n{'─'*60}")
    print(f"  MANUAL STEP: {step_desc}")
    print(f"  Expected: {count} files matching '{pattern}'")
    print(f"  In:       {directory}")
    print(f"\n  After completing this step, press ENTER to continue...")
    print(f"{'─'*60}")
    input()

    actual = sorted(directory.glob(pattern))
    if len(actual) != count:
        print(f"  WARNING: expected {count} files, found {len(actual)}")
    else:
        print(f"  OK: {len(actual)} files found")
    return actual


def main():
    parser = argparse.ArgumentParser(
        description="Full pipeline: bin→json→extract→circle→blend→mp4"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--camera", type=int, default=6,
                        help="Anchor camera number (default: 6)")
    parser.add_argument("--head", type=int, default=64,
                        help="Real frames 1..HEAD kept (default: 64)")
    parser.add_argument("--tail", type=int, default=161,
                        help="Real frames TAIL..end kept (default: 161)")
    parser.add_argument("--total", type=int, default=300,
                        help="SuperSplat render count (default: 300)")
    parser.add_argument("--radius-scale", type=float, default=1.0)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--crf", type=int, default=6)
    parser.add_argument("--source", type=str, default="raw_frames",
                        help="Raw frames subfolder name")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    camera_str = f"{args.camera:03d}"
    python = sys.executable

    # ---- save config ----------------------------------------------------
    config = {
        "project": args.project,
        "camera": args.camera,
        "head": args.head,
        "tail": args.tail,
        "total": args.total,
        "radius_scale": args.radius_scale,
        "source": args.source,
    }
    config_path = proj / "config.json"
    if not config_path.exists():
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Config saved → {config_path}")

    # ---- Step 1: bin → json --------------------------------------------
    bin_dir = proj / "colmap_bins"
    if not (bin_dir / "cameras.bin").exists():
        print(f"ERROR: {bin_dir / 'cameras.bin'} not found. Place COLMAP bin files there first.")
        sys.exit(1)

    cameras_json = proj / "cameras.json"
    if not cameras_json.exists():
        step("1/6  colmap_bin_to_json",
             [python, str(SCRIPT_DIR / "colmap_bin_to_json.py"),
              "--project", args.project])
    else:
        print(f"\n  SKIP Step 1: {cameras_json} already exists")

    # ---- Step 2: extract anchor camera ---------------------------------
    anchor_dir = proj / "anchor_frames"
    if not anchor_dir.is_dir() or not list(anchor_dir.glob("*.jpg")):
        step("2/6  extract_camera",
             [python, str(SCRIPT_DIR / "extract_camera.py"),
              "--project", args.project,
              "--camera", str(args.camera),
              "--source", args.source])
    else:
        print(f"\n  SKIP Step 2: {anchor_dir} already populated")

    # ---- Step 3: circle interpolation -----------------------------------
    align_json = proj / "cameras_align.json"
    if not align_json.exists():
        step("3/6  interpolate_cameras_circle",
             [python, str(SCRIPT_DIR / "interpolate_cameras_circle.py"),
              "--project", args.project,
              "--anchor-camera", camera_str,
              "--total", str(args.total),
              "--radius-scale", str(args.radius_scale)])
    else:
        print(f"\n  SKIP Step 3: {align_json} already exists")

    # ---- Step 4: SuperSplat render (MANUAL) ----------------------------
    renders_dir = proj / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)

    wait_for_files(
        renders_dir, "circle_*.png", args.total,
        f"1. Open SuperSplat\n"
        f"   2. Load your PLY model\n"
        f"   3. Import: {align_json}\n"
        f"   4. View Panel → Export All\n"
        f"   5. Move downloaded circle_*.png into:\n"
        f"      {renders_dir}\n"
        f"   6. Press ENTER here"
    )

    # ---- Step 5: blend -------------------------------------------------
    blended_dir = proj / "blended"
    step("5/6  blend_frames",
         [python, str(SCRIPT_DIR / "blend_frames.py"),
          "--project", args.project,
          "--head", str(args.head),
          "--tail", str(args.tail),
          "--render-count", str(args.total)])

    # ---- Step 6: mp4 ---------------------------------------------------
    step("6/6  pngs_to_mp4",
         [python, str(SCRIPT_DIR / "pngs_to_mp4.py"),
          "--project", args.project,
          "--fps", str(args.fps),
          "--crf", str(args.crf)])

    print(f"\n{'='*60}")
    print(f"  DONE. Output:")
    print(f"    blended: {blended_dir}")
    print(f"    output:  {proj / 'output'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
