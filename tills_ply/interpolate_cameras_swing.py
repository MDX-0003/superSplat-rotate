#!/usr/bin/env python3
"""
Arc-swing camera trajectory: start at the anchor camera, arc around the fitted
circle by ``swing`` degrees, then swing back — ending at a pose that is
*bit-identical* to the first one so the SuperSplat timeline loops seamlessly.

This is a sibling of ``interpolate_cameras_circle.py`` (which produces a full
360° orbit → ``cameras_align.json``).  It reuses the same circle fitting,
COLMAP look-at convention and per-sample residual blending, but samples a
triangle-wave angle profile instead of a full revolution, and freezes the
intrinsics at the anchor camera's values (a ±30° swing is not a zoom shot).

Why "pose is a pure function of angle"
--------------------------------------
Every output pose is computed from its angle only, never from "which leg of
the swing we are on".  The outgoing leg and the returning leg therefore produce
*identical* poses at identical angles, so there is no jitter at the turnaround
point, and the seam (last frame → first frame, which is where the SuperSplat
timeline wraps: ``time = (time + dt * frameRate) % frames``) is exact.

Closure maths
-------------
The default profile is a *semi-implicit* raised cosine — the half-sample offset
is what makes the closure exact, not just approximately exact:

    v[i]     = 0.5 * (1 - cos(2*pi*(i + 0.5) / N))   -> v[0] == v[N-1] == 0
    angle[i] = ang_a + dir * radians(X) * v[i]

Sampling at segment midpoints puts the cosine's zero crossings exactly on the
first and last frame, so the two endpoint poses are identical by construction
(and the profile is symmetric about the turnaround).  With a plain
``cos(2*pi*i/N)`` the sampled period would span i = 0..N, leaving
``v[N-1] ~ (pi/N)^2`` — for N=300 that is 0.15 mm of motion at a 2.6 m radius,
i.e. a visible seam step.

A linear triangle cannot close exactly either: reaching zero again needs equal
step counts on both legs (i_peak == (N-1)/2, only possible for odd N), and
forcing it makes the final step ~100x smaller than every other step so the
timeline stalls at the ends.

Peak-to-peak: the camera swings to +X degrees, comes back through 0 and reaches
-X degrees at the last frame — a 2X sweep whose end pose equals its start pose.
There is deliberately no option to leave the trajectory open: an unclosed swing
would jump at the timeline wrap, which is the one thing this trajectory exists to
avoid.

Usage:
  # via project name (standalone — reads cameras.json from CameraData/<project>)
  uv run python tills_ply/interpolate_cameras_swing.py --project 03 --swing-deg 30

  # via direct path (used by fuse_server / ply_pipeline)
  uv run python tills_ply/interpolate_cameras_swing.py --path CameraData/03 \
      --max-index 89 --total 300 --anchor-camera 006 --swing-deg 30 --dir auto
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

# ---------------------------------------------------------------------------
# shared maths — same algorithms as interpolate_cameras_circle.py
# ---------------------------------------------------------------------------
def fit_circle(points: np.ndarray):
    """Fit a 3D circle via SVD plane + least-squares.

    Returns (center, normal, r_fit, angles, radii, centroid, u1, u2).
    """
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


def lookat_colmap(position, center, world_up):
    """Camera-to-world rotation for the COLMAP/3DGS convention.

    R = [right, down, forward] with
      forward = normalize(center - position)
      right   = normalize(world_up x forward)
      up      = forward x right          (COLMAP +Y is down)
    """
    forward = center - position
    forward = forward / np.linalg.norm(forward)
    right = np.cross(world_up, forward)
    right = right / np.linalg.norm(right)
    up = np.cross(forward, right)
    return np.column_stack([right, up, forward])


def angle_to_3d(a, r, center, u1, u2):
    """Convert (angle, radius) back to a 3D point on the fitted plane."""
    return center + u1 * (r * np.cos(a)) + u2 * (r * np.sin(a))


def _wrap_progress(a, ang_a, ang_b, span):
    """Position of ``a`` within the [ang_a, ang_a + 2π) revolution.

    ``0`` at the anchor, rising to ``1`` at the far anchor ``ang_b``, then
    falling back to ``0`` around the rest of the circle.  This is the exact
    triangle profile the circle version relies on, kept here so the radial
    sweep behaves identically (it closes by construction).
    """
    a_mod = (a - ang_a) % (2 * np.pi) + ang_a
    if a_mod <= ang_b:
        return (a_mod - ang_a) / span
    return 1.0 - (a_mod - ang_b) / (2 * np.pi - span)


def _triangular(p):
    """Fold a progress value into a 0→1→0 triangle wave."""
    return p if p <= 0.5 else 1.0 - p


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Arc-swing camera interpolation (swing out, swing back, "
                    "loop-closed) around the fitted circle"
    )
    parser.add_argument("--project",
                        help="Project name under CameraData/ (standalone use)")
    parser.add_argument("--path",
                        help="Direct path to project directory (e.g. CameraData/03)")
    parser.add_argument("--max-index", type=int, default=89,
                        help="Fit the circle / search anchors over cameras id=0..N "
                             "(keep this identical to the circle pass so the swing "
                             "uses the same centre, plane and radii)")
    parser.add_argument("--total", type=int, default=300,
                        help="Total output frames (= SuperSplat timeline length)")
    parser.add_argument("--anchor-camera", type=str, default="006",
                        help="Camera img_name the swing starts and ends at")
    parser.add_argument("--input", type=str, default=None,
                        help="Input JSON (default: <proj>/cameras.json)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSON (default: <proj>/cameras_spin.json)")
    parser.add_argument("--swing-deg", type=float, default=30.0,
                        help="Swing amplitude in degrees (default 30). The camera "
                             "reaches +X at the turnaround and -X at the last "
                             "frame, i.e. a 2X peak-to-peak sweep that returns to "
                             "the start pose. >0 travels along the capture "
                             "direction, <0 against it.")
    parser.add_argument("--dir", type=str, default="auto",
                        choices=["auto", "left", "right"],
                        help="Sign of +swing-deg. auto = follow the capture order "
                             "(same as the circle pass), left/right = force it. "
                             "The actual on-screen direction depends on the fitted "
                             "plane normal; verify with --output and a test import.")
    parser.add_argument("--turn-frame", type=int, default=None,
                        help="Frame index of the turnaround (default: the exact "
                             "midpoint (N-1)/2, which is forced by the symmetry "
                             "required for loop closure). off-centre angles emit a "
                             "warning and fall back to the exact midpoint.")
    parser.add_argument("--residual-blend", type=str, default="auto",
                        choices=["auto", "full", "none"],
                        help="How to blend the two anchors' SfM residuals: "
                             "auto = triangular (0→1→0, anchors exact), "
                             "full = out leg 0→1 / return leg 1→0, "
                             "none = pure look-at (no SfM correction)")
    parser.add_argument("--lock-intrinsics", dest="lock_intrinsics",
                        action="store_true", default=True,
                        help="[default] Keep fx/fy/width/height at the anchor values")
    parser.add_argument("--no-lock-intrinsics", dest="lock_intrinsics",
                        action="store_false",
                        help="Lerp intrinsics towards the far anchor like the circle pass")
    parser.add_argument("--radius-scale", type=float, default=1.0,
                        help="Scale the sampled radii (1.0 = original)")
    parser.add_argument("--height-offset", type=float, default=0.0,
                        help="Shift the whole sweep along the plane normal (metres)")
    parser.add_argument("--pitch-offset", type=float, default=0.0,
                        help="Rotate every camera around its own right-axis (degrees, "
                             "positive = look up)")
    parser.add_argument("--fov-x", type=float, default=80.0,
                        help="Horizontal FOV in degrees for all output cameras")
    args = parser.parse_args()

    # ----- resolve project directory --------------------------------------
    if args.path:
        proj = Path(args.path).resolve()
    elif args.project:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tills"))
        from paths import project as proj_dir  # noqa: E402
        proj = proj_dir(args.project)
    else:
        print("ERROR: either --project or --path is required")
        sys.exit(1)

    input_path = Path(args.input) if args.input else proj / "cameras.json"
    output_path = Path(args.output) if args.output else proj / "cameras_spin.json"

    if not input_path.exists():
        print(f"ERROR: input not found: {input_path}")
        sys.exit(1)

    # ----- load ----------------------------------------------------------
    with open(input_path, "r") as f:
        data = json.load(f)

    total_loaded = len(data)
    data = data[:args.max_index + 1]
    print(f"Loaded {total_loaded} poses, keeping 0..{args.max_index} ({len(data)})")

    # find the anchor camera by img_name (remember the ARRAY INDEX — cameras.json
    # happens to number id == index today, but never rely on that)
    anchor_img = args.anchor_camera
    anchor_idx = None
    for i, d in enumerate(data):
        if d["img_name"] == anchor_img:
            anchor_idx = i
            break
    if anchor_idx is None:
        print(f"ERROR: camera with img_name='{anchor_img}' not found in input")
        sys.exit(1)
    print(f"Anchor camera  : img_name={anchor_img}  array_index={anchor_idx}  "
          f"(file id={data[anchor_idx]['id']})")

    positions = np.array([d["position"] for d in data])

    # ----- fit circle from ALL keyframes (same as the circle pass) --------
    center, normal, r_fit, _, _, _, u1, u2 = fit_circle(positions)
    print(f"Circle center   : [{center[0]:.4f}, {center[1]:.4f}, {center[2]:.4f}]")
    print(f"Fit radius      : {r_fit:.4f}")
    print(f"Plane normal    : [{normal[0]:.4f}, {normal[1]:.4f}, {normal[2]:.4f}]")

    # align the normal with the consensus camera 'down' axis (COLMAP R[:,1]),
    # exactly like the circle pass — the look-at helper needs it.
    cam_downs = np.array([np.array(d["rotation"])[:, 1] for d in data])
    avg_down = np.mean(cam_downs, axis=0)
    if np.dot(normal, avg_down) < 0:
        normal = -normal
        print(f"Normal flipped (align with camera down): "
              f"[{normal[0]:.4f}, {normal[1]:.4f}, {normal[2]:.4f}]")

    # ----- capture angular direction (defines what "+swing" means) --------
    proj_pts = np.column_stack([(positions - center) @ u1, (positions - center) @ u2])
    ang_steps = np.arctan2(
        proj_pts[:-1, 0] * proj_pts[1:, 1] - proj_pts[:-1, 1] * proj_pts[1:, 0],
        proj_pts[:-1, 0] * proj_pts[1:, 0] + proj_pts[:-1, 1] * proj_pts[1:, 1],
    )
    mean_step = float(np.mean(ang_steps))
    capture_sign = 1.0 if mean_step >= 0 else -1.0
    if args.dir == "auto":
        dir_sign = capture_sign
    elif args.dir == "right":
        dir_sign = 1.0
    else:  # left
        dir_sign = -1.0
    print(f"Capture step    : {np.degrees(mean_step):+.3f} deg/frame "
          f"({'CCW' if mean_step >= 0 else 'CW'} in the (u1,u2) frame)")
    print(f"Direction       : --dir {args.dir} -> "
          f"{'+angle (u1->u2)' if dir_sign > 0 else '-angle (u2->u1)'}")

    # ----- anchors: A = anchor camera, B = angularly farthest keyframe -----
    all_angles = []
    for d in data:
        v = np.array(d["position"]) - center
        all_angles.append(float(np.arctan2(v @ u2, v @ u1)))

    ang_a = all_angles[anchor_idx]
    best_j, best_dist = -1, -1.0
    for j, a in enumerate(all_angles):
        d_forward = (a - ang_a) % (2 * np.pi)
        d_back = (ang_a - a) % (2 * np.pi)
        dist = min(d_forward, d_back)
        if dist > best_dist:
            best_dist = dist
            best_j = j

    anchor_a = data[anchor_idx]
    anchor_b = data[best_j]
    p_a = np.array(anchor_a["position"])
    p_b = np.array(anchor_b["position"])
    r_a = float(np.linalg.norm(p_a - center))
    r_b = float(np.linalg.norm(p_b - center))

    ang_b = all_angles[best_j]
    while ang_b <= ang_a:
        ang_b += 2 * np.pi
    span = ang_b - ang_a

    print(f"Anchor A       : img_name={anchor_img}  idx={anchor_idx}  "
          f"r={r_a:.4f}  angle={np.degrees(ang_a):.2f}°")
    print(f"Anchor B (auto): idx={best_j}  r={r_b:.4f}  "
          f"angle={np.degrees(all_angles[best_j]):.2f}°  "
          f"gap={np.degrees(best_dist):.1f}°")

    # ----- swing amplitude -------------------------------------------------
    # X is the amplitude of the raised-cosine profile in the default
    # (close-loop) mode, i.e. the camera reaches +X degrees at the turnaround
    # and comes back through 0 to -X degrees at the last frame — a 2X peak-to-
    # peak sweep with an exact loop closure.  With --no-close-loop, X is the
    # literal one-sided amplitude and the profile is a piecewise-linear triangle.
    N = args.total
    if N < 4:
        print("ERROR: --total must be >= 4")
        sys.exit(1)

    amp_deg = args.swing_deg
    if amp_deg == 0.0:
        print("ERROR: --swing-deg must be non-zero")
        sys.exit(1)

    i_peak = args.turn_frame
    exact_peak = (N - 1) / 2.0
    if i_peak is None:
        i_peak = int(round(exact_peak))
    elif abs(i_peak - exact_peak) > 0.5:
        print(f"WARNING: --turn-frame {i_peak} is off-centre; a profile that "
              f"returns to the anchor is symmetric, so the turnaround is forced "
              f"to the midpoint ({exact_peak:g}). Using that instead.")
        i_peak = int(round(exact_peak))
    if not (1 <= i_peak <= N - 2):
        print(f"ERROR: turnaround frame {i_peak} is out of range for --total {N}")
        sys.exit(1)

    # The radial triangle profile is bounded by construction — ``_wrap_progress``
    # never exceeds 1, so the sampled radius always stays within [r_a, r_b] and
    # there is no state in which the profile "blows up".  The only true ceiling is
    # a full revolution (359°), where the triangle folds right back onto the
    # anchor.
    #
    # Sweeping past the far anchor (X > 2*pi - span, i.e. past the angularly
    # opposite side) does fold the profile, but the practical effect is a soft
    # bulge towards r_b, NOT a radial slide: measured on 0913 (span 180.25°,
    # r_a 4.3354 / r_b 4.3331) the worst deviation across the whole sweep is
    # 2.3 mm at ±359°, and 3e-6 m at ±180°.  So this is reported, never clamped —
    # silently shrinking a user's ±180° to ±179.75° would be worse than the
    # sub-millimetre effect it guards against.
    room_deg = math.degrees(2 * math.pi - span)
    max_sweep = 359.0

    # Semi-implicit raised cosine — the half-sample offset is what makes the
    # closure EXACT.  With a plain cos(2*pi*i/N) the sampled period spans
    # i=0..N, so v[N-1] is only ~0 (1.1e-4 for N=300, which is 0.15 mm of motion
    # at a 2.6 m radius): a visible seam step.  Sampling the cosine at the
    # segment MIDPOINTS i+0.5 puts the zero crossings exactly on i=0 and i=N-1,
    # so v[0] == v[N-1] == 0 and the profile is exactly symmetric about the
    # turnaround.
    v = np.array([0.5 * (1.0 - np.cos(2.0 * np.pi * (i + 0.5) / N))
                  for i in range(N)])
    peak_deg = abs(amp_deg)
    profile = ("raised-cosine (exact closure, zero velocity at both ends, "
               f"peak at frames {i_peak}/{i_peak + 1})")

    if peak_deg > max_sweep:
        print(f"WARNING: sweep ±{peak_deg:.2f}° reaches a full revolution — "
              f"clamped to ±{max_sweep:.2f}° (the radial profile folds back onto "
              f"the anchor there; use the circle pass for a full orbit)")
        amp_deg = math.copysign(max_sweep, amp_deg)
        peak_deg = abs(amp_deg)
    elif peak_deg > room_deg:
        print(f"NOTE: sweep ±{peak_deg:.2f}° goes {peak_deg - room_deg:.2f}° past "
              f"the angularly opposite side (anchor gap {math.degrees(best_dist):.1f}°"
              f" → room {room_deg:.2f}°). The radius profile folds slightly there; "
              f"harmless when the two anchor radii are close.")

    angles = ang_a + dir_sign * np.radians(amp_deg) * v

    # ----- per-sample progress (drives radius / residual / intrinsics) -----
    # Clamped triangle around the circle, exactly like the circle pass: 0 at the
    # anchor, rising to 1 at the angularly far anchor, then back.  p[0] == 0 and
    # p[N-1] == 0, so radius, residual and intrinsics all close with it.
    progress = []

    # ----- radius profile -------------------------------------------------
    scale = args.radius_scale
    height_offset = args.height_offset

    def radius_at_angle(a):
        p = _wrap_progress(a, ang_a, ang_b, span)
        return r_a + max(0.0, min(1.0, p)) * (r_b - r_a)

    radii = np.array([radius_at_angle(a) * scale for a in angles])

    # ----- residuals (per-sample, anchored exactly at A) ------------------
    R_a = np.array(anchor_a["rotation"])
    R_b = np.array(anchor_b["rotation"])
    rot_a = Rotation.from_matrix(R_a)
    rot_b = Rotation.from_matrix(R_b)
    world_up = normal

    rot_look_a = Rotation.from_matrix(lookat_colmap(p_a, center, world_up))
    rot_look_b = Rotation.from_matrix(lookat_colmap(p_b, center, world_up))
    residual_a = rot_look_a.inv() * rot_a
    residual_b = rot_look_b.inv() * rot_b

    # ----- intrinsics -----------------------------------------------------
    fx_a, fy_a = anchor_a["fx"], anchor_a["fy"]
    fx_b, fy_b = anchor_b["fx"], anchor_b["fy"]
    w_a, h_a = anchor_a["width"], anchor_a["height"]
    w_b, h_b = anchor_b["width"], anchor_b["height"]

    # ----- optional pitch offset (around the camera right axis) -----------
    pitch_R = None
    if args.pitch_offset != 0.0:
        pitch_R = Rotation.from_rotvec([np.radians(args.pitch_offset), 0, 0])

    # ----- build poses ----------------------------------------------------
    positions_out = []
    rotations_out = []
    intrinsics_out = []

    for a, r in zip(angles, radii):
        pos = angle_to_3d(a, r, center, u1, u2)
        if height_offset != 0.0:
            pos = pos + normal * height_offset
        positions_out.append(pos)

        # Residual blend weight, driven only by the ANGLE (never by which leg of
        # the swing we are on), so the turnaround point cannot jump.  Since the
        # angle never leaves the arc between the anchor and the far anchor, the
        # progress is monotone there: 0 -> 1 -> 0.
        p = _wrap_progress(a, ang_a, ang_b, span)
        progress.append(p)

        if args.residual_blend == "none":
            frac = 0.0
        elif args.residual_blend == "auto":
            frac = 2.0 * _triangular(p)     # peaks at the turnaround
        else:  # full
            frac = 2.0 * min(p, 0.5)        # reaches the far anchor's residual
        frac = max(0.0, min(1.0, frac))

        slerp = _slerp(residual_a, residual_b, frac)
        rot = Rotation.from_matrix(lookat_colmap(pos, center, world_up)) * slerp
        if pitch_R is not None:
            rot = rot * pitch_R
        rotations_out.append(rot.as_matrix())

        if args.lock_intrinsics:
            intrinsics_out.append((fx_a, fy_a, w_a, h_a))
        else:
            t = p
            intrinsics_out.append((
                fx_a + t * (fx_b - fx_a),
                fy_a + t * (fy_b - fy_a),
                w_a + t * (w_b - w_a),
                h_a + t * (h_b - h_a),
            ))

    # ----- output ---------------------------------------------------------
    output = []
    for i in range(N):
        fx, fy, w, h = intrinsics_out[i]
        output.append({
            "id": i + 1,
            "img_name": f"swing_{i + 1:04d}",
            "width": int(round(w)),
            "height": int(round(h)),
            "position": [round(float(v), 6) for v in positions_out[i]],
            "rotation": [[round(float(v), 6) for v in row]
                         for row in rotations_out[i]],
            "fy": round(float(fy), 6),
            "fx": round(float(fx), 6),
            "fov_x": round(args.fov_x, 6),
        })

    # ----- self-check: the whole point of this trajectory is loop closure --
    d_pos = float(np.linalg.norm(positions_out[0] - positions_out[-1]))
    delta_rot = (Rotation.from_matrix(rotations_out[0]).inv()
                 * Rotation.from_matrix(rotations_out[-1]))
    d_rot = float(np.degrees(delta_rot.magnitude()))
    last_step_deg = float(abs(np.degrees(angles[-1] - angles[-2])))
    seam_step_deg = float(abs(np.degrees(angles[0] - angles[-1])))
    max_step_deg = float(np.max(np.abs(np.degrees(np.diff(angles)))))
    max_progress = float(max(progress))

    print(f"Profile         : {profile}")
    print(f"Swing           : ±{peak_deg:.3f}° (peak-to-peak "
          f"{2 * peak_deg:.3f}°)  turnaround frame={i_peak}  "
          f"max step={max_step_deg:.4f}°/frame  avg="
          f"{float(np.mean(np.abs(np.degrees(np.diff(angles))))):.4f}°/frame")
    print(f"Residual blend  : {args.residual_blend}   "
          f"radius progress: 0→{max_progress:.3f}→"
          f"{progress[-1]:.3f}")
    print(f"Self-check      : |pos[0]-pos[N-1]|={d_pos:.3e} m  "
          f"rot delta={d_rot:.3e}°  seam step={seam_step_deg:.5f}°  "
          f"last step={last_step_deg:.5f}°")

    if max_progress >= 0.999 and peak_deg > 300.0:
        print("WARNING: the sweep nearly reaches the far anchor's angle — the "
              "radial/residual profile is at its degenerate limit. "
              "Consider a smaller --swing-deg.")

    # Hard gate: the SuperSplat timeline wraps last frame -> frame 0, so a
    # mismatch here is a visible jump in the rendered video.
    if d_pos > 1e-5 or d_rot > 1e-3:
        print("ERROR: loop closure self-check FAILED — refusing to write a "
              "trajectory that would visibly jump at the timeline wrap.")
        print(f"       (d_pos={d_pos:.6e} m, d_rot={d_rot:.6e}°)")
        sys.exit(1)
    print("Closure verified: pose[N-1] == pose[0]  OK")

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Output          : {len(output)} poses → {output_path}")
    print(f"Start/end pose  : anchor camera {anchor_img} "
          f"(angle {np.degrees(ang_a):.2f}°)")


def _slerp(ra, rb, frac):
    """Shortest-arc slerp between two single rotations (frac in [0,1])."""
    if frac <= 0.0:
        return ra
    if frac >= 1.0:
        return rb
    return Slerp([0.0, 1.0], Rotation.concatenate([ra, rb]))(frac)


if __name__ == "__main__":
    main()
