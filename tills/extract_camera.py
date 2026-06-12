#!/usr/bin/env python3
"""
Extract camera images from specified frame ranges across multiple cameras.

Usage:
  # legacy: extract all frames for a single camera
  python tills/extract_camera.py --project 02 --camera 6

  # v2: extract specific segments
  python tills/extract_camera.py --project 02 --segments "cam006:1-64,cam032:161-168"

Reads:  CameraData/<project>/<source_dir>/NNNN/CCC.jpg
Writes: CameraData/<project>/anchor_frames/NNNN.jpg  (sequential)
        CameraData/<project>/anchor_frames/segments.json  (metadata)
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from paths import project as proj_dir

DEFAULT_SOURCE = "raw_frames"

SEGMENT_RE = re.compile(r"^cam(\d+):(\d+)-(\d+)$")


def parse_segments(spec):
    """Parse segment string like 'cam006:1-64,cam032:161-168'.

    Returns list of dicts: [{camera: int, start: int, end: int}, ...]
    """
    segments = []
    for part in spec.split(","):
        part = part.strip()
        m = SEGMENT_RE.match(part)
        if not m:
            raise ValueError(
                f"Invalid segment '{part}'. Expected format: camNNN:start-end"
            )
        camera = int(m.group(1))
        start = int(m.group(2))
        end = int(m.group(3))
        if start > end:
            raise ValueError(f"Segment '{part}' has start > end")
        segments.append({"camera": camera, "start": start, "end": end})
    return segments


def extract_segments(source_dir, out_dir, segments):
    """Extract frames per segment, writing sequentially numbered output.

    Returns list of metadata entries for segments.json.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata = []
    output_idx = 1

    for seg in segments:
        camera = seg["camera"]
        start = seg["start"]
        end = seg["end"]
        camera_file = f"{camera:03d}.jpg"

        seg_start_out = output_idx
        copied = 0

        for frame_num in range(start, end + 1):
            frame_dir = source_dir / f"{frame_num:04d}"
            src = frame_dir / camera_file
            if not src.exists():
                print(f"WARNING: {src} missing, skipping")
                continue
            dst = out_dir / f"{output_idx:04d}.jpg"
            shutil.copy2(src, dst)
            output_idx += 1
            copied += 1

        metadata.append({
            "camera": camera,
            "start": start,
            "end": end,
            "output_start": seg_start_out,
            "output_end": seg_start_out + copied - 1,
        })
        print(f"  cam{camera:03d} frames {start}-{end}: "
              f"copied {copied} → output {seg_start_out:04d}-{seg_start_out + copied - 1:04d}")

    return metadata


def extract_single_camera(source_dir, out_dir, camera_num):
    """Legacy mode: extract all frames for a single camera.

    Each output file is named after its frame number (preserves original frame number).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    camera_file = f"{camera_num:03d}.jpg"

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


def main():
    parser = argparse.ArgumentParser(
        description="Extract camera images from specified segments or single camera"
    )
    parser.add_argument("--project", required=True,
                        help="Project name under CameraData/")
    parser.add_argument("--camera", type=int, default=None,
                        help="Camera number to extract (legacy, default: not used)")
    parser.add_argument("--segments", type=str, default=None,
                        help="Segment spec e.g. 'cam006:1-64,cam032:161-168'")
    parser.add_argument("--source", type=str, default=DEFAULT_SOURCE,
                        help=f"Source folder name (default: {DEFAULT_SOURCE})")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    source_dir = proj / args.source
    out_dir = proj / "anchor_frames"

    if not source_dir.is_dir():
        print(f"ERROR: source not found: {source_dir}")
        sys.exit(1)

    if args.segments:
        # v2: multi-segment extraction
        segments = parse_segments(args.segments)
        print(f"Segments: {len(segments)}")
        metadata = extract_segments(source_dir, out_dir, segments)

        # write metadata
        meta_path = out_dir / "segments.json"
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)
        print(f"Metadata → {meta_path}")
    else:
        # legacy: single camera extraction
        camera_num = args.camera if args.camera is not None else 6
        print(f"Camera: {camera_num:03d} (legacy mode, all frames)")
        extract_single_camera(source_dir, out_dir, camera_num)


if __name__ == "__main__":
    main()
