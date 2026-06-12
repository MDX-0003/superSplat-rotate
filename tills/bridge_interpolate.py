#!/usr/bin/env python3
"""
Arc bridge between two camera poses rotating around a vertical axis.

Generates N poses along an arc from anchor_pose to target_pose, rotating
around the given center. The rotation direction can be forced to match
an existing sequence (e.g. UE trajectory), ensuring the bridge does not
reverse the camera motion.

Usage:
  python tills/bridge_interpolate.py \
      --anchor anchor.json --target target.json \
      --center 0,0,0 --direction cw \
      --min-frames 30 --output bridge.json
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
def lookat_colmap(position, center, world_up=None):
    """Camera-to-world rotation matrix for COLMAP/3DGS convention.

    R = [right, -up, forward] where:
      forward = normalize(center - position)   (looking toward center)
      right   = normalize(world_up × forward)
      up      = forward × right                (COLMAP +Y is down)
    """
    if world_up is None:
        world_up = np.array([0.0, 1.0, 0.0])
    forward = center - position
    forward = forward / np.linalg.norm(forward)
    right = np.cross(world_up, forward)
    right_norm = np.linalg.norm(right)
    if right_norm < 1e-10:
        right = np.array([1.0, 0.0, 0.0])
    else:
        right = right / right_norm
    up = np.cross(forward, right)
    return np.column_stack([right, up, forward])


# ---------------------------------------------------------------------------
def compute_center(gt_cameras):
    """Compute centroid of GT camera positions as the circle center."""
    positions = np.array([c["position"] for c in gt_cameras])
    return np.mean(positions, axis=0)


# ---------------------------------------------------------------------------
def detect_rotation_direction(positions, center):
    """Return +1 for CCW, -1 for CW based on first ~10 frames.

    Uses the Y component of the cross product of successive position
    vectors in the XZ plane relative to the center.
    """
    n = min(10, len(positions) - 1)
    if n < 1:
        return 1  # default CCW
    deltas = []
    for i in range(n):
        v1 = np.array(positions[i]) - center
        v2 = np.array(positions[i + 1]) - center
        cross_y = v1[0] * v2[2] - v1[2] * v2[0]
        deltas.append(cross_y)
    avg = np.mean(deltas)
    return 1 if avg >= 0 else -1


# ---------------------------------------------------------------------------
def generate_bridge(
    anchor_pose,
    target_pose,
    center,
    direction,
    min_frames=30,
    world_up=None,
):
    """Generate arc bridge poses from anchor_pose to target_pose.

    Args:
        anchor_pose: dict with position, rotation, fx, fy, width, height
        target_pose: dict with position, rotation, fx, fy, width, height
        center: (3,) array, center of rotation
        direction: +1 for CCW, -1 for CW, 0 for shortest path
        min_frames: minimum number of bridge frames
        world_up: (3,) array, default [0, 1, 0]

    Returns:
        list of pose dicts in cameras.json format
    """
    if world_up is None:
        world_up = np.array([0.0, 1.0, 0.0])

    p_anchor = np.array(anchor_pose["position"])
    p_target = np.array(target_pose["position"])

    # horizontal distances from center
    dx_a = p_anchor[0] - center[0]
    dz_a = p_anchor[2] - center[2]
    dx_t = p_target[0] - center[0]
    dz_t = p_target[2] - center[2]

    r_anchor = math.sqrt(dx_a * dx_a + dz_a * dz_a)
    r_target = math.sqrt(dx_t * dx_t + dz_t * dz_t)

    if r_anchor < 1e-6 or r_target < 1e-6:
        raise ValueError("Anchor or target pose too close to rotation center")

    # polar angles in XZ plane
    theta_anchor = math.atan2(dz_a, dx_a)
    theta_target = math.atan2(dz_t, dx_t)

    # compute delta in the specified direction
    raw_delta = theta_target - theta_anchor
    # normalize to [-pi, pi]
    while raw_delta > math.pi:
        raw_delta -= 2 * math.pi
    while raw_delta < -math.pi:
        raw_delta += 2 * math.pi

    if direction == 0:
        # shortest path (default for tail bridges)
        delta = raw_delta
    elif direction > 0:  # CCW (positive angle increase)
        if raw_delta < 0:
            delta = raw_delta + 2 * math.pi
        else:
            delta = raw_delta
    else:  # CW (negative angle increase)
        if raw_delta > 0:
            delta = raw_delta - 2 * math.pi
        else:
            delta = raw_delta

    # frame count: proportional to angular span
    density = 300.0 / (2 * math.pi)  # ~300 frames per full circle
    N = max(min_frames, int(math.ceil(abs(delta) * density)))

    # heights
    y_anchor = p_anchor[1]
    y_target = p_target[1]

    # intrinsics
    fx_a = anchor_pose.get("fx", 808)
    fy_a = anchor_pose.get("fy", 808)
    fx_t = target_pose.get("fx", 808)
    fy_t = target_pose.get("fy", 808)
    w_a = anchor_pose.get("width", 1920)
    h_a = anchor_pose.get("height", 1080)
    w_t = target_pose.get("width", 1920)
    h_t = target_pose.get("height", 1080)

    poses = []
    for i in range(N):
        t = i / max(N - 1, 1)

        angle = theta_anchor + t * delta
        radius = r_anchor + t * (r_target - r_anchor)
        height = y_anchor + t * (y_target - y_anchor)

        x = center[0] + radius * math.cos(angle)
        z = center[2] + radius * math.sin(angle)
        pos = np.array([x, height, z])

        rot = lookat_colmap(pos, center, world_up)

        fx = fx_a + t * (fx_t - fx_a)
        fy = fy_a + t * (fy_t - fy_a)
        w = w_a + t * (w_t - w_a)
        h = h_a + t * (h_t - h_a)

        poses.append({
            "id": i,
            "img_name": f"bridge_{i:04d}",
            "width": int(round(w)),
            "height": int(round(h)),
            "position": [round(float(v), 6) for v in pos],
            "rotation": [[round(float(v), 6) for v in row] for row in rot.tolist()],
            "fy": round(float(fy), 6),
            "fx": round(float(fx), 6),
        })

    return poses


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Arc bridge pose generator")
    parser.add_argument("--anchor", required=True,
                        help="JSON file with anchor pose (GT camera)")
    parser.add_argument("--target", required=True,
                        help="JSON file with target pose (UE sequence endpoint)")
    parser.add_argument("--center", required=True,
                        help="Rotation center as x,y,z")
    parser.add_argument("--direction", required=True, choices=["cw", "ccw"],
                        help="Rotation direction")
    parser.add_argument("--min-frames", type=int, default=30,
                        help="Minimum bridge frames (default: 30)")
    parser.add_argument("--output", required=True, help="Output JSON file")
    args = parser.parse_args()

    center = np.array([float(v) for v in args.center.split(",")])
    direction = 1 if args.direction == "ccw" else -1

    with open(args.anchor, "r") as f:
        anchor_data = json.load(f)
    with open(args.target, "r") as f:
        target_data = json.load(f)

    # if loaded data is an array, take the first element as the pose
    anchor_pose = anchor_data[0] if isinstance(anchor_data, list) else anchor_data
    target_pose = target_data[0] if isinstance(target_data, list) else target_data

    poses = generate_bridge(
        anchor_pose=anchor_pose,
        target_pose=target_pose,
        center=center,
        direction=direction,
        min_frames=args.min_frames,
    )

    with open(args.output, "w") as f:
        json.dump(poses, f, indent=2)

    delta = abs(math.atan2(
        poses[-1]["position"][2] - center[2],
        poses[-1]["position"][0] - center[0]
    ) - math.atan2(
        poses[0]["position"][2] - center[2],
        poses[0]["position"][0] - center[0]
    ))
    print(f"Bridge: {len(poses)} frames, "
          f"angular span: {math.degrees(delta):.1f}°, "
          f"direction: {args.direction}")


if __name__ == "__main__":
    main()
