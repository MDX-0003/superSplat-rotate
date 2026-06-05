#!/usr/bin/env python3
"""
Extract images from a fixed camera across all frame subdirectories.

Usage:
  python tills/extract_camera.py --project 02 --camera 6

Reads:  CameraData/<project>/<source_dir>/NNNN/CCC.jpg
Writes: CameraData/<project>/anchor_frames/NNNN.jpg
"""
import argparse
import shutil
import sys

from paths import project as proj_dir

# default source folder name (user can override with --source)
DEFAULT_SOURCE = "raw_frames"


def main():
    parser = argparse.ArgumentParser(
        description="Extract a fixed camera's images across all frames"
    )
    parser.add_argument("--project", required=True,
                        help="Project name under CameraData/")
    parser.add_argument("--camera", type=int, default=6,
                        help="Camera number to extract (default: 6)")
    parser.add_argument("--source", type=str, default=DEFAULT_SOURCE,
                        help=f"Source folder name (default: {DEFAULT_SOURCE})")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    source_dir = proj / args.source
    out_dir = proj / "anchor_frames"

    if not source_dir.is_dir():
        print(f"ERROR: source not found: {source_dir}")
        sys.exit(1)

    camera_file = f"{args.camera:03d}.jpg"
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = sorted(
        d for d in source_dir.iterdir() if d.is_dir() and d.name.isdigit()
    )
    if not frames:
        print(f"No frame folders found in {source_dir}")
        return

    copied = 0
    for frame_dir in frames:
        src = frame_dir / camera_file
        if not src.exists():
            print(f"WARNING: {src} missing, skipping frame {frame_dir.name}")
            continue
        dst = out_dir / f"{frame_dir.name}.jpg"
        shutil.copy2(src, dst)
        copied += 1

    print(f"Copied {copied}/{len(frames)} frames → {out_dir}")


if __name__ == "__main__":
    main()
