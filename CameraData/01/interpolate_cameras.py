#!/usr/bin/env python3
"""
--offset-mode     towardCenter|alongForward
# 默认参数
python interpolate_cameras.py

# 向前方推 0.5（拍摄更广）
python interpolate_cameras.py --max-index 44 --insert 3 --smooth-sigma 1.0 --smooth-window 5 --offset-distance 0.5 --offset-mode alongForward

python interpolate_cameras.py --max-index 44 --insert 3 --smooth-sigma 8.0 --smooth-window 8 --offset-distance 0.5 --offset-mode alongForward
Interpolate camera poses along a fitted circle with optional smoothing.

Keeps poses 0..max_index from cameras.json, fits a 3D circle to their positions,
slerp-interpolates angles + rotations to add N points between each consecutive pair,
then smooths and writes the result to cameras_align.json.
"""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.interpolate import interp1d
from scipy.ndimage import gaussian_filter1d
from scipy.spatial.transform import Rotation, Slerp

SCRIPT_DIR = Path(__file__).resolve().parent


def fit_circle(points: np.ndarray):
    """
    Fit a 3D circle to a set of points.

    Returns (center, normal, radius, angles, centroid, u1, u2)
    where angles are in [-pi, pi] measured from u1 in the (u1,u2) plane.
    """
    centroid = np.mean(points, axis=0)
    shifted = points - centroid
    _, _, vh = np.linalg.svd(shifted)
    normal = vh[2]
    u1 = vh[0]  # first basis vector in plane
    u2 = vh[1]  # second basis vector in plane

    # project to 2D
    x = shifted @ u1
    y = shifted @ u2

    # least-squares circle: x^2 + y^2 = 2*cx*x + 2*cy*y + (r^2 - cx^2 - cy^2)
    A = np.column_stack([2 * x, 2 * y, np.ones_like(x)])
    b = x * x + y * y
    sol, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy, d = sol
    r = float(np.sqrt(d + cx * cx + cy * cy))

    center_3d = centroid + cx * u1 + cy * u2
    angles = np.arctan2(y - cy, x - cx)

    return center_3d, normal, r, angles, centroid, u1, u2


def sort_by_angle(data, angles):
    """Sort data entries by angle ascending, return sorted data and unwrapped angles."""
    order = np.argsort(angles)
    sorted_data = [data[i] for i in order]
    unwrapped = np.unwrap(angles[order])
    return sorted_data, unwrapped


def interpolate_angles(angles, insert):
    """
    Linearly interpolate angles with `insert` new points between each
    consecutive pair (including wrap-around for full-circle data).
    Returns new_angles array and (segment_index, local_t) for each new point.
    """
    n = len(angles)
    segments = []

    for i in range(n):
        a0 = angles[i]
        a1 = angles[(i + 1) % n] if i < n - 1 else angles[0] + 2 * np.pi
        steps = insert + 1  # total pts per segment including both ends
        for j in range(steps):
            if i == n - 1 and j == insert:
                break  # skip duplicate wrap point
            t = j / steps
            segments.append((i, t))
            segments[-1] = float(a0 + t * (a1 - a0))

    return np.array(segments), segments


def slerp_rotations(rotations, insert):
    """Interpolate rotations using quaternion slerp, same insert pattern as positions."""
    n = len(rotations)
    rots = Rotation.from_matrix(rotations)
    new_rots = []

    for i in range(n):
        r0 = rots[i]
        r1 = rots[(i + 1) % n]
        steps = insert + 1
        slerp = Slerp([0, 1], Rotation.concatenate([r0, r1]))
        for j in range(steps):
            if i == n - 1 and j == insert:
                break
            t = j / steps
            new_rots.append(slerp(t).as_matrix())

    return new_rots


def angles_to_3d(angles, center, radius, u1, u2):
    """Convert angles back to 3D points on the fitted circle."""
    pts = []
    for a in angles:
        p = center + u1 * (radius * np.cos(a)) + u2 * (radius * np.sin(a))
        pts.append(p.tolist())
    return pts


def smooth_angular(angles, sigma, radius=0):
    """
    Smooth angles in the circular domain via complex representation,
    then convert back.  Avoids the linear-unwrap boundary problem.

    Args:
        sigma: Gaussian sigma (standard deviation)
        radius: filter radius in points (0 = auto 4*sigma)
    """
    kwargs = dict(sigma=sigma, mode='wrap')
    if radius > 0:
        kwargs['radius'] = radius
    z = np.exp(1j * np.array(angles))
    z_real = gaussian_filter1d(z.real, **kwargs)
    z_imag = gaussian_filter1d(z.imag, **kwargs)
    return np.arctan2(z_imag, z_real)


def main():
    parser = argparse.ArgumentParser(
        description="Interpolate camera poses along a fitted circle"
    )
    parser.add_argument(
        "--max-index", type=int, default=44,
        help="Keep poses with id 0..max_index from cameras.json (default: 44)"
    )
    parser.add_argument(
        "--insert", type=int, default=3,
        help="Number of new points to insert between each consecutive pair (default: 3)"
    )
    parser.add_argument(
        "--offset-distance", type=float, default=0.0,
        help="Offset each camera along --offset-mode direction before interpolation (default: 0)"
    )
    parser.add_argument(
        "--offset-mode", type=str, default="towardCenter",
        choices=["towardCenter", "alongForward"],
        help="Offset direction: 'towardCenter' (toward circle center) or 'alongForward' (along camera +Z axis)"
    )
    parser.add_argument(
        "--smooth-sigma", type=float, default=1.0,
        help="Gaussian smoothing sigma (0 = no smoothing, default: 1.0)"
    )
    parser.add_argument(
        "--smooth-window", type=int, default=0,
        help="Gaussian filter radius in points (0 = auto from sigma, default: 0)"
    )
    parser.add_argument(
        "--input", type=str, default=None,
        help="Input file path (default: <script_dir>/cameras.json)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output file path (default: <script_dir>/cameras_align.json)"
    )
    args = parser.parse_args()

    input_path = Path(args.input) if args.input else SCRIPT_DIR / "cameras.json"
    output_path = Path(args.output) if args.output else SCRIPT_DIR / "cameras_align.json"

    # Load
    with open(input_path, "r") as f:
        data = json.load(f)

    total_loaded = len(data)
    data = data[:args.max_index + 1]
    print(f"Loaded {total_loaded} poses, keeping 0..{args.max_index} ({len(data)} poses)")

    # Extract positions & rotations
    positions = np.array([d["position"] for d in data])
    rotations = [np.array(d["rotation"]) for d in data]

    # Apply offset to original keyframes (before fitting circle)
    if args.offset_distance != 0:
        # Compute center from original positions for towardCenter mode
        temp_center, _, _, _, _, _, _ = fit_circle(positions)
        offset_vecs = []

        for i in range(len(data)):
            if args.offset_mode == "towardCenter":
                direction = temp_center - positions[i]
                direction /= np.linalg.norm(direction)
            else:  # alongForward — camera +Z in world space is rotation[:, 2]
                direction = rotations[i][:, 2]
                direction /= np.linalg.norm(direction)
            offset_vecs.append(direction * args.offset_distance)
            # Update the dict so subsequent steps use adjusted positions
            new_pos = positions[i] + offset_vecs[-1]
            data[i]["position"] = new_pos.tolist()

        # Re-extract after offset
        positions = np.array([d["position"] for d in data])
        print(f"Applied offset: {args.offset_distance} ({args.offset_mode})")

    # Fit circle
    center, normal, radius, angles, centroid, u1, u2 = fit_circle(positions)
    print(f"Circle center : [{center[0]:.4f}, {center[1]:.4f}, {center[2]:.4f}]")
    print(f"Circle radius : {radius:.4f}")

    # Sort by angle so interpolation follows the circle
    sorted_data, sorted_angles = sort_by_angle(data, angles)

    # Interpolate angles
    new_angles, _ = interpolate_angles(sorted_angles, args.insert)
    print(f"After interpolation: {len(new_angles)} poses")

    # Smooth angles (circular-aware)
    if args.smooth_sigma > 0:
        new_angles = smooth_angular(new_angles, args.smooth_sigma, args.smooth_window)
        win = args.smooth_window if args.smooth_window > 0 else int(4 * args.smooth_sigma)
        print(f"Applied Gaussian smoothing (sigma={args.smooth_sigma}, window={2*win+1}pts)")

    # Convert angles back to 3D
    new_positions = angles_to_3d(new_angles, center, radius, u1, u2)

    # Interpolate rotations
    sorted_rots = [r for _, r in sorted(zip(sorted_angles, rotations),
                                         key=lambda x: x[0])]
    # Actually, rotations are already in sorted order from sort_by_angle
    sorted_rots_array = [np.array(d["rotation"]) for d in sorted_data]
    new_rots = slerp_rotations(sorted_rots_array, args.insert)

    # Interpolate intrinsics (linear interpolation between nearest originals)
    orig_positions = np.array([d["position"] for d in sorted_data])
    fx_vals = np.array([d["fx"] for d in sorted_data])
    fy_vals = np.array([d["fy"] for d in sorted_data])
    w_vals = np.array([d["width"] for d in sorted_data])
    h_vals = np.array([d["height"] for d in sorted_data])

    # Map each new angle to interpolated intrinsics
    n_orig = len(sorted_angles)
    # Recompute new_angles generation to get (orig_index, t) mapping
    seg_info = []
    for i in range(n_orig):
        a0 = sorted_angles[i]
        a1 = sorted_angles[(i + 1) % n_orig] if i < n_orig - 1 else sorted_angles[0] + 2 * np.pi
        steps = args.insert + 1
        for j in range(steps):
            if i == n_orig - 1 and j == args.insert:
                break
            t = j / steps
            seg_info.append((i, t))

    new_fx = []
    new_fy = []
    new_w = []
    new_h = []
    for i, t in seg_info:
        i_next = (i + 1) % n_orig
        new_fx.append(float(fx_vals[i] + t * (fx_vals[i_next] - fx_vals[i])))
        new_fy.append(float(fy_vals[i] + t * (fy_vals[i_next] - fy_vals[i])))
        new_w.append(float(w_vals[i] + t * (w_vals[i_next] - w_vals[i])))
        new_h.append(float(h_vals[i] + t * (h_vals[i_next] - h_vals[i])))

    # Build output
    output = []
    for idx in range(len(new_positions)):
        output.append({
            "id": idx,
            "img_name": f"interp_{idx:04d}",
            "width": int(round(new_w[idx])),
            "height": int(round(new_h[idx])),
            "position": [round(v, 6) for v in new_positions[idx]],
            "rotation": [[round(v, 6) for v in row] for row in new_rots[idx]],
            "fy": round(new_fy[idx], 6),
            "fx": round(new_fx[idx], 6)
        })

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(output)} poses to {output_path}")


if __name__ == "__main__":
    main()
