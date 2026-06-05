#!/usr/bin/env python3
"""
Blend real camera frames with SuperSplat rendered frames into one sequence.

Real frames 1..head are kept as-is.
The middle block is replaced by SuperSplat PNGs (circle_0001..circle_NNNN).
Real frames tail..end are kept, renumbered after the render block.

Usage:
  python tills/blend_frames.py --project 02 --head 64 --tail 161 --render-count 300
"""
import argparse
import re
import shutil
from pathlib import Path

from paths import project as proj_dir


def extract_number(path: Path):
    m = re.search(r'(\d+)', path.stem)
    return int(m.group(1)) if m else -1


def main():
    parser = argparse.ArgumentParser(
        description="Blend real + rendered frames into one sequence"
    )
    parser.add_argument("--project", required=True,
                        help="Project name under CameraData/")
    parser.add_argument("--head", type=int, default=64,
                        help="Real frames 1..HEAD are kept (default: 64)")
    parser.add_argument("--tail", type=int, default=161,
                        help="Real frames TAIL..end are kept (default: 161)")
    parser.add_argument("--render-count", type=int, default=300,
                        help="Expected number of SuperSplat PNGs (default: 300)")
    parser.add_argument("--render-dir", default=None,
                        help="Override render source dir (default: <project>/renders)")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    real_dir    = proj / "anchor_frames"
    render_dir  = Path(args.render_dir) if args.render_dir else proj / "renders"
    output_dir  = proj / "blended"

    # ---- collect real frames -------------------------------------------
    real_files = sorted(
        [f for f in real_dir.glob("*.jpg") if f.is_file()],
        key=extract_number
    )
    print(f"Real frames found: {len(real_files)} in {real_dir}")

    head_frames = [f for f in real_files if extract_number(f) <= args.head]
    tail_frames = [f for f in real_files if extract_number(f) >= args.tail]
    replaced    = [f for f in real_files
                   if args.head < extract_number(f) < args.tail]
    print(f"  Head (≤{args.head}): {len(head_frames)}  "
          f"Replaced ({args.head+1}~{args.tail-1}): {len(replaced)}  "
          f"Tail (≥{args.tail}): {len(tail_frames)}")

    # ---- collect SuperSplat renders ------------------------------------
    if not render_dir.is_dir():
        print(f"ERROR: render dir not found: {render_dir}")
        print(f"  (Run SuperSplat export and place circle_*.png here, then retry)")
        return

    render_files = sorted(
        [f for f in render_dir.glob("circle_*.png") if f.is_file()],
        key=extract_number
    )
    print(f"SuperSplat renders found: {len(render_files)} in {render_dir}")

    if len(render_files) != args.render_count:
        print(f"WARNING: expected {args.render_count} renders, got {len(render_files)}")

    # ---- output --------------------------------------------------------
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    idx = 1

    def copy_out(src: Path, num: int):
        nonlocal idx
        ext = src.suffix.lower()
        dst = output_dir / f"frame_{num:04d}{ext}"
        shutil.copy2(src, dst)
        idx += 1

    # 1) head
    for f in head_frames:
        copy_out(f, extract_number(f))
    head_end = idx - 1

    # 2) renders
    for f in render_files:
        copy_out(f, idx)
    render_end = idx - 1

    # 3) tail
    for f in tail_frames:
        copy_out(f, idx)
    tail_end = idx - 1

    total = idx - 1
    print(f"Output: {total} frames → {output_dir}")
    print(f"  frame_0001 ~ frame_{head_end:04d}  ← real ({len(head_frames)} frames)")
    print(f"  frame_{head_end + 1:04d} ~ frame_{render_end:04d}  ← SuperSplat ({len(render_files)} frames)")
    if tail_frames:
        print(f"  frame_{render_end + 1:04d} ~ frame_{tail_end:04d}  ← real tail "
              f"({len(tail_frames)} frames, orig {args.tail}~{extract_number(tail_frames[-1])})")


if __name__ == "__main__":
    main()
