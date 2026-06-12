#!/usr/bin/env python3
"""
Blend real camera frames with SuperSplat rendered frames into one sequence.

v1 (legacy): fixed head/render/tail pattern
  python tills/blend_frames.py --project 02 --head 64 --tail 161 --render-count 300

v2 (segments): arbitrary ordered segments
  python tills/blend_frames.py --project 02 \
      --segments "cam006:1-64,render,cam032:161-168"

Segment types:
  camNNN:start-end   real frames from extract_camera output
  render             all rendered frames from renders/
  render:N           N rendered frames from renders/
"""
import argparse
import json
import re
import shutil
from pathlib import Path

from paths import project as proj_dir

REAL_SEGMENT_RE = re.compile(r"^cam(\d+):(\d+)-(\d+)$")
RENDER_SEGMENT_RE = re.compile(r"^render(?::(\d+))?$")


def resolve_segment(seg_spec, anchor_dir, render_dir, segments_meta):
    """Resolve a segment spec to a list of (src_path, is_render) tuples.

    Returns empty list if segment is invalid (warning printed).
    """
    seg_spec = seg_spec.strip()

    # render:N or render
    rm = RENDER_SEGMENT_RE.match(seg_spec)
    if rm:
        count_str = rm.group(1)
        render_files = sorted(
            [f for f in render_dir.glob("circle_*.png") if f.is_file()],
            key=lambda f: extract_number(f)
        )
        if count_str:
            count = int(count_str)
            render_files = render_files[:count]
        return [(f, True) for f in render_files]

    # camNNN:start-end
    rm_real = REAL_SEGMENT_RE.match(seg_spec)
    if rm_real:
        camera = int(rm_real.group(1))
        start = int(rm_real.group(2))
        end = int(rm_real.group(3))

        # find matching entry in segments metadata
        for entry in segments_meta:
            if (entry["camera"] == camera and
                    entry["start"] == start and
                    entry["end"] == end):
                out_start = entry["output_start"]
                out_end = entry["output_end"]
                files = []
                for idx in range(out_start, out_end + 1):
                    src = anchor_dir / f"{idx:04d}.jpg"
                    if src.exists():
                        files.append((src, False))
                return files

        # fallback: try direct frame numbers
        print(f"WARNING: segment '{seg_spec}' not found in segments.json, "
              f"trying direct frame numbers")
        files = []
        for frame in range(start, end + 1):
            src = anchor_dir / f"{frame:04d}.jpg"
            if src.exists():
                files.append((src, False))
        return files

    print(f"WARNING: unrecognized segment '{seg_spec}', skipping")
    return []


def extract_number(path: Path):
    m = re.search(r'(\d+)', path.stem)
    return int(m.group(1)) if m else -1


def main():
    parser = argparse.ArgumentParser(
        description="Blend real + rendered frames into one sequence"
    )
    parser.add_argument("--project", required=True,
                        help="Project name under CameraData/")

    # v2: segments
    parser.add_argument("--segments", type=str, default=None,
                        help="Segment spec: 'cam006:1-64,render,cam032:161-168'")

    # v1: legacy
    parser.add_argument("--head", type=int, default=None,
                        help="Legacy: real frames 1..HEAD kept")
    parser.add_argument("--tail", type=int, default=None,
                        help="Legacy: real frames TAIL..end kept")
    parser.add_argument("--render-count", type=int, default=300,
                        help="Legacy: expected render count (default: 300)")
    parser.add_argument("--render-dir", default=None,
                        help="Override render source dir")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    anchor_dir = proj / "anchor_frames"
    render_dir = Path(args.render_dir) if args.render_dir else proj / "renders"
    output_dir = proj / "blended"

    if not render_dir.is_dir():
        print(f"ERROR: render dir not found: {render_dir}")
        return

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    # determine mode
    if args.segments:
        # ---- v2: segments mode ----
        seg_specs = [s.strip() for s in args.segments.split(",")]

        # read segments metadata if available
        segments_meta = []
        meta_path = anchor_dir / "segments.json"
        if meta_path.exists():
            with open(meta_path, "r") as f:
                segments_meta = json.load(f)
            print(f"Loaded segments metadata: {len(segments_meta)} entries")
        else:
            print("No segments.json found, using direct frame numbering")

        # resolve all segments
        all_files = []
        for spec in seg_specs:
            resolved = resolve_segment(spec, anchor_dir, render_dir, segments_meta)
            if not resolved:
                print(f"WARNING: segment '{spec}' produced no files")
            all_files.extend(resolved)

        # write output
        idx = 1
        real_count = 0
        render_count = 0
        for src, is_render in all_files:
            ext = src.suffix.lower()
            dst = output_dir / f"frame_{idx:04d}{ext}"
            shutil.copy2(src, dst)
            idx += 1
            if is_render:
                render_count += 1
            else:
                real_count += 1

        total = idx - 1
        print(f"Output: {total} frames → {output_dir}")
        print(f"  Real: {real_count}  Render: {render_count}")

    else:
        # ---- v1: legacy mode ----
        head = args.head
        tail = args.tail
        if head is None or tail is None:
            print("ERROR: --segments not provided, "
                  "both --head and --tail are required for legacy mode")
            return

        real_files = sorted(
            [f for f in anchor_dir.glob("*.jpg") if f.is_file()],
            key=extract_number
        )
        print(f"Real frames found: {len(real_files)} in {anchor_dir}")

        head_frames = [f for f in real_files if extract_number(f) <= head]
        tail_frames = [f for f in real_files if extract_number(f) >= tail]
        replaced = [f for f in real_files
                     if head < extract_number(f) < tail]
        print(f"  Head (≤{head}): {len(head_frames)}  "
              f"Replaced ({head+1}~{tail-1}): {len(replaced)}  "
              f"Tail (≥{tail}): {len(tail_frames)}")

        render_files = sorted(
            [f for f in render_dir.glob("circle_*.png") if f.is_file()],
            key=extract_number
        )
        print(f"SuperSplat renders found: {len(render_files)} in {render_dir}")
        if len(render_files) != args.render_count:
            print(f"WARNING: expected {args.render_count} renders, "
                  f"got {len(render_files)}")

        idx = 1

        def copy_out(src, num):
            nonlocal idx
            ext = src.suffix.lower()
            dst = output_dir / f"frame_{num:04d}{ext}"
            shutil.copy2(src, dst)
            idx += 1

        for f in head_frames:
            copy_out(f, extract_number(f))
        head_end = idx - 1

        for f in render_files:
            copy_out(f, idx)
        render_end = idx - 1

        for f in tail_frames:
            copy_out(f, idx)
        tail_end = idx - 1

        total = idx - 1
        print(f"Output: {total} frames → {output_dir}")
        print(f"  frame_0001 ~ frame_{head_end:04d}  ← real ({len(head_frames)} frames)")
        print(f"  frame_{head_end + 1:04d} ~ frame_{render_end:04d}  ← SuperSplat ({len(render_files)} frames)")
        if tail_frames:
            print(f"  frame_{render_end + 1:04d} ~ frame_{tail_end:04d}  ← real tail "
                  f"({len(tail_frames)} frames, orig {tail}~{extract_number(tail_frames[-1])})")


if __name__ == "__main__":
    main()
