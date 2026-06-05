#!/usr/bin/env python3
"""
Fit a circle to all keyframes (0..max_index) to get the center, then pick
2 anchor keyframes whose exact position/rotation/intrinsics are preserved.
The remaining (total - 2) poses lie on an elliptical arc with radius
interpolated between the two anchors, and rotations derived from a look-at
toward the center + a slerp blend of the two anchor orientations.

Usage:
  python interpolate_cameras_circle.py
  python interpolate_cameras_circle.py --total 300 --anchor1 0 --anchor2 22 --radius-scale 0.8
"""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# circle fitting
# ---------------------------------------------------------------------------
def fit_circle(points: np.ndarray):
    """Fit a 3D circle via SVD plane + least-squares. Returns (center, normal, r_fit, angles, radii, centroid, u1, u2)."""
    centroid = np.mean(points, axis=0)
    shifted = points - centroid
    _, _, vh = np.linalg.svd(shifted)
    normal = vh[2]
    u1 = vh[0]
    u2 = vh[1]

    x = shifted @ u1
    y = shifted @ u2
    A = np.column_stack([2 * x, 2 * y, np.ones_like(x)])
    b = x * x + y * y
    sol, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy, d = sol
    r_fit = float(np.sqrt(d + cx * cx + cy * cy))

    center = centroid + cx * u1 + cy * u2
    angles = np.arctan2(y - cy, x - cx)
    radii = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)

    return center, normal, r_fit, angles, radii, centroid, u1, u2


# ---------------------------------------------------------------------------
# look-at rotation (3DGS / COLMAP convention)
# Camera-to-world: R = [right, -up, forward]  where
#   forward = normalize(center - position)   (looking toward center)
#   right   = normalize(world_up × forward)
#   up      = forward × right               (COLMAP +Y is down, so -up goes up)
# ---------------------------------------------------------------------------
def lookat_colmap(position, center, world_up):
    forward = center - position
    forward = forward / np.linalg.norm(forward)
    right = np.cross(world_up, forward)
    right = right / np.linalg.norm(right)
    up = np.cross(forward, right)               # points downward in world
    R = np.column_stack([right, up, forward])    # R = [right, down, forward]
    return R


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def angle_to_3d(a, r, center, u1, u2):
    """Convert (angle, radius) back to 3D point on the fitted plane."""
    return center + u1 * (r * np.cos(a)) + u2 * (r * np.sin(a))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Circle interpolation with look-at rotations"
    )
    parser.add_argument("--max-index", type=int, default=44)
    parser.add_argument("--total", type=int, default=300)
    parser.add_argument("--anchor1", type=int, default=0,
                        help="Index (in original JSON) of first anchor keyframe")
    parser.add_argument("--anchor2", type=int, default=22,
                        help="Index (in original JSON) of second anchor keyframe")
    parser.add_argument("--input", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--radius-scale", type=float, default=1.0,
                        help="Scale the ellipse radii (1.0 = original, >1 = wider orbit, <1 = tighter)")
    args = parser.parse_args()

    input_path = Path(args.input) if args.input else SCRIPT_DIR / "cameras.json"
    output_path = Path(args.output) if args.output else SCRIPT_DIR / "cameras_align.json"

    # ----- load ----------------------------------------------------------
    with open(input_path, "r") as f:
        data = json.load(f)

    total_loaded = len(data)
    data = data[:args.max_index + 1]
    print(f"Loaded {total_loaded} poses, keeping 0..{args.max_index} ({len(data)})")

    # ----- extract -------------------------------------------------------
    positions = np.array([d["position"] for d in data])

    # optional radius scaling (applied to final sample radii, not keyframes)
    scale = args.radius_scale
    if scale != 1.0:
        print(f"Radius scale   : {scale:.4f}")

    # ----- fit circle from ALL keyframes (only for center) ---------------
    center, normal, r_fit, _, _, _, u1, u2 = fit_circle(positions)
    print(f"Circle center   : [{center[0]:.4f}, {center[1]:.4f}, {center[2]:.4f}]")
    print(f"Fit radius      : {r_fit:.4f}")
    print(f"Plane normal    : [{normal[0]:.4f}, {normal[1]:.4f}, {normal[2]:.4f}]")

    # ----- pick two anchors (by their original JSON index) ----------------
    anchor_a = data[args.anchor1]
    anchor_b = data[args.anchor2]
    p_a = np.array(anchor_a["position"])
    p_b = np.array(anchor_b["position"])
    r_a = float(np.linalg.norm(p_a - center))
    r_b = float(np.linalg.norm(p_b - center))
    print(f"Anchor radii    : r1={r_a:.4f} (id={args.anchor1})  r2={r_b:.4f} (id={args.anchor2})")

    # compute anchor angles in the fitted plane
    # project onto (u1,u2) and compute atan2
    d_a = p_a - center
    d_b = p_b - center
    x_a, y_a = d_a @ u1, d_a @ u2
    x_b, y_b = d_b @ u1, d_b @ u2
    ang_a = float(np.arctan2(y_a, x_a))
    ang_b = float(np.arctan2(y_b, x_b))

    # unwrap so ang_b > ang_a and span ∈ (0, 2π]
    while ang_b <= ang_a:
        ang_b += 2 * np.pi
    span = ang_b - ang_a
    print(f"Angular span    : {np.degrees(span):.1f}°  ({ang_a:.4f} → {ang_b:.4f})")

    # ----- build output angles -------------------------------------------
    N = args.total
    # place the two anchors exactly at their angles (in output angle space)
    # output angle 0 → ang_a,  output angle (N-1) → ang_b (going the "long way")
    # Actually we want 300 uniform poses covering 0..2π, with anchors at their angles.
    # Map: output i's world-angle = ang_a + (ang_b - ang_a) * i / (N - 1)
    # This covers from anchor1 to anchor2 through the angular span.
    # But we want all 2π covered. With 2 anchors at ang_a and ang_b,
    # we use the SHORT span from ang_a to ang_b as the full circle.
    # If the user's data has ~352°, wrap to 360° conceptually.
    if span > np.pi:
        full_span = 2 * np.pi
    else:
        full_span = span  # preserve exact angular span

    sample_angles = np.linspace(ang_a, ang_a + full_span, N, endpoint=False)
    # ensure anchors land exactly at their positions
    sample_angles[0] = ang_a
    # find the closest sample to ang_b and pin it
    idx_b = np.argmin(np.abs(sample_angles - ang_b))
    sample_angles[idx_b] = ang_b

    # ----- per-sample radius (linear between r_a and r_b) ---------------
    # radius at angle θ = r_a + (r_b - r_a) * (θ - ang_a) / (ang_b - ang_a)
    # for the "other side" (θ > ang_b or θ < ang_a), wrap around
    def radius_at_angle(a):
        # normalize a into [ang_a, ang_a + 2π)
        a_mod = (a - ang_a) % (2 * np.pi) + ang_a
        if a_mod <= ang_b:
            t = (a_mod - ang_a) / span
        else:
            t = (a_mod - ang_b) / (2 * np.pi - span)
            # going from r_b back to r_a
            t = 1.0 - max(t, 0)  # smooth transition
        t = max(0.0, min(1.0, t))
        return r_a + t * (r_b - r_a)

    sample_radii = np.array([radius_at_angle(a) * scale for a in sample_angles])

    # ----- 3D positions --------------------------------------------------
    sample_positions = []
    for a, r in zip(sample_angles, sample_radii):
        sample_positions.append(angle_to_3d(a, r, center, u1, u2))

    # ----- rotations: blend of look-at + anchor slerp --------------------
    R_a = np.array(anchor_a["rotation"])
    R_b = np.array(anchor_b["rotation"])
    rot_a = Rotation.from_matrix(R_a)
    rot_b = Rotation.from_matrix(R_b)
    world_up = normal  # use the fitted plane normal as up

    # compute the "look-at" rotation at each anchor to get the delta
    # between look-at and the actual anchor rotation
    R_look_a = lookat_colmap(p_a, center, world_up)
    R_look_b = lookat_colmap(p_b, center, world_up)
    rot_look_a = Rotation.from_matrix(R_look_a)
    rot_look_b = Rotation.from_matrix(R_look_b)

    # residual rotation at each anchor (what the SfM solves beyond look-at)
    residual_a = rot_look_a.inv() * rot_a
    residual_b = rot_look_b.inv() * rot_b

    sample_rots = []
    for i, (pos, a) in enumerate(zip(sample_positions, sample_angles)):
        # look-at from this position
        R_look = lookat_colmap(np.array(pos), center, world_up)
        rot_look = Rotation.from_matrix(R_look)

        # how far are we between the two anchor angles?
        # map a into [ang_a, ang_a+2π), compute fraction to ang_b
        a_mod = (a - ang_a) % (2 * np.pi) + ang_a
        if a_mod <= ang_b:
            t = (a_mod - ang_a) / span
        else:
            t = 1.0 + (a_mod - ang_b) / (2 * np.pi - span)
        t = t % 1.0  # wrap for full circle

        # slerp the residual between the two anchor residuals
        if t <= 0.5:
            frac = t / 0.5  # 0→1 from anchor_a to anchor_b (via span)
            slerp = Slerp([0, 1], Rotation.concatenate([residual_a, residual_b]))
            residual = slerp(frac)
        else:
            frac = (t - 0.5) / 0.5  # 0→1 from anchor_b back to anchor_a
            slerp = Slerp([0, 1], Rotation.concatenate([residual_b, residual_a]))
            residual = slerp(frac)

        rot = rot_look * residual
        sample_rots.append(rot.as_matrix())

    # ----- intrinsics ----------------------------------------------------
    fx_a, fy_a = anchor_a["fx"], anchor_a["fy"]
    fx_b, fy_b = anchor_b["fx"], anchor_b["fy"]
    w_a, h_a = anchor_a["width"], anchor_a["height"]
    w_b, h_b = anchor_b["width"], anchor_b["height"]

    def interp_linear(a_mod):
        if a_mod <= ang_b:
            t = (a_mod - ang_a) / span
        else:
            t = 1.0 - (a_mod - ang_b) / (2 * np.pi - span)
        t = max(0.0, min(1.0, t))
        return t

    sample_fx = []
    sample_fy = []
    sample_w = []
    sample_h = []
    for a in sample_angles:
        a_mod = (a - ang_a) % (2 * np.pi) + ang_a
        t = interp_linear(a_mod)
        sample_fx.append(fx_a + t * (fx_b - fx_a))
        sample_fy.append(fy_a + t * (fy_b - fy_a))
        sample_w.append(w_a + t * (w_b - w_a))
        sample_h.append(h_a + t * (h_b - h_a))

    # ----- output --------------------------------------------------------
    output = []
    for i in range(N):
        output.append({
            "id": i,
            "img_name": f"circle_{i:04d}",
            "width": int(round(sample_w[i])),
            "height": int(round(sample_h[i])),
            "position": [round(float(v), 6) for v in sample_positions[i]],
            "rotation": [[round(float(v), 6) for v in row]
                         for row in sample_rots[i]],
            "fy": round(float(sample_fy[i]), 6),
            "fx": round(float(sample_fx[i]), 6),
        })

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Output          : {len(output)} poses → {output_path}")
    print(f"Anchors at idx  : 0 (angle {ang_a:.4f})  {idx_b} (angle {ang_b:.4f})")


if __name__ == "__main__":
    main()
