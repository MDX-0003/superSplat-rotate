#!/usr/bin/env python3
"""
Merge bridge segments and a UE camera sequence into a unified trajectory JSON.

Concatenates: bridge1 (GT anchor → UE start) + UE sequence + bridge2 (UE end → GT tail)
Outputs a single cameras_align.json with sequential IDs and img_names.

Usage:
  python tills/merge_trajectory.py \
      --bridge1 bridge1.json --ue-seq ue_cameras.json \
      --output cameras_align.json

  # with tail bridge
  python tills/merge_trajectory.py \
      --bridge1 bridge1.json --ue-seq ue_cameras.json \
      --bridge2 bridge2.json --output cameras_align.json
"""
import argparse
import json
from pathlib import Path


def format_pose(pose, new_id):
    """Clone a pose with a new sequential ID and img_name."""
    return {
        "id": new_id,
        "img_name": f"frame_{new_id:04d}",
        "width": pose.get("width", 1920),
        "height": pose.get("height", 1080),
        "position": [round(float(v), 6) for v in pose["position"]],
        "rotation": [[round(float(v), 6) for v in row]
                      for row in pose["rotation"]],
        "fy": round(float(pose.get("fy", 808)), 6),
        "fx": round(float(pose.get("fx", 808)), 6),
    }


def merge_trajectories(bridge1, ue_sequence, bridge2=None):
    """Merge bridge1 + UE sequence + optional bridge2 into a single list.

    Returns list of pose dicts with sequential IDs starting from 1.
    """
    output = []
    idx = 1

    for pose in bridge1:
        output.append(format_pose(pose, idx))
        idx += 1

    for pose in ue_sequence:
        output.append(format_pose(pose, idx))
        idx += 1

    if bridge2:
        for pose in bridge2:
            output.append(format_pose(pose, idx))
            idx += 1

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Merge bridge segments and UE sequence into one trajectory"
    )
    parser.add_argument("--bridge1", type=str, default=None,
                        help="Bridge segment: GT anchor → UE start")
    parser.add_argument("--ue-seq", required=True,
                        help="UE camera sequence JSON")
    parser.add_argument("--bridge2", type=str, default=None,
                        help="Bridge segment: UE end → GT tail (optional)")
    parser.add_argument("--output", required=True, help="Output JSON file")
    args = parser.parse_args()

    bridge1 = []
    if args.bridge1:
        with open(args.bridge1, "r") as f:
            bridge1 = json.load(f)
        print(f"Bridge1: {len(bridge1)} frames")

    with open(args.ue_seq, "r") as f:
        ue_sequence = json.load(f)
    print(f"UE Seq : {len(ue_sequence)} frames")

    bridge2 = None
    if args.bridge2:
        with open(args.bridge2, "r") as f:
            bridge2 = json.load(f)
        print(f"Bridge2: {len(bridge2)} frames")

    merged = merge_trajectories(bridge1, ue_sequence, bridge2)

    with open(args.output, "w") as f:
        json.dump(merged, f, indent=2)

    total = len(merged)
    b1_end = len(bridge1)
    ue_end = b1_end + len(ue_sequence)
    print(f"\nMerged: {total} frames → {args.output}")
    print(f"  frames 0001~{b1_end:04d}  ← bridge1")
    print(f"  frames {b1_end+1:04d}~{ue_end:04d}  ← UE sequence")
    if bridge2:
        print(f"  frames {ue_end+1:04d}~{total:04d}  ← bridge2")


if __name__ == "__main__":
    main()
