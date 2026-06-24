#!/usr/bin/env python3
"""
Clip (remove) the largest N% of Gaussian splats from each PLY file, ranked by
volume proxy = scale_0 + scale_1 + scale_2 (sum of log-scales, equivalent to
SuperSplat's exp(s0)*exp(s1)*exp(s2) ranking since exp is monotonic).

Optionally (--denoise), also remove isolated floater Gaussians inside a
cylinder fitted to camera positions — same circle-fit + cylinder logic as
fuse_ply.py.  Uses grid-based region-growing from the density peak to identify
the person cluster; points inside the cylinder but outside the grown region
are discarded as artefacts.

Outputs processed PLYs to {path}-clip/, keeping original filenames.

Usage:
  # volume-clip only
  python tills/clip_ply.py

  # volume-clip + cylinder denoise
  python tills/clip_ply.py --denoise

Config file (JSON) — all keys optional; CLI args take precedence:
  {
    "path": "CameraData/05",
    "clip_percent": 10,
    "denoise": true,
    "max_index": 89,
    "radius_scale": 0.5,
    "height_up": 0.6,
    "height_down": 0.5,
    "denoise_min_points": 30
  }

---- 参数说明 ------------------------------------------------------------
  clip_percent        删除体积最大的前 X% GS 点 (0~100)。默认 10。
                      PLY 中 scale 以 log 空间存储；体积排名 = s0+s1+s2
                      (等价 SuperSplat 的 exp(s0)*exp(s1)*exp(s2))。
                      设为 0 则不删除任何点（纯拷贝）。

  -- 以下参数仅在 --denoise 时生效 ----------------------------------------
  denoise             启用圆柱区域内孤立伪影剔除 (bool, 默认 false)。
                      开启后需要 cameras.json 存在。
  max_index           拟合圆所用的相机范围 id=0..max_index (从0开始,包含max_index)。
  radius_scale        拟合圆半径缩放系数 (<1 收紧, >1 放宽)。典型值 0.3~1.0。
  height_up/height_down
                      圆柱沿拟合平面法向量的上下高度(米)。
  denoise_min_points  网格 region-growing 的最低点数阈值 (默认 30)。
                      一个 0.15m×0.15m 的 cell 内点数 ≥ 此值才被纳入人物区域。
                      值越小越宽松(保留更多), 越大越激进(剔除更多)。
-----------------------------------------------------------------------
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# config loading (same pattern as fuse_ply.py)
# ---------------------------------------------------------------------------
def load_config(explicit_path=None):
    """Load a JSON config file.  Search order:
      1. explicit --config path
      2. ./clip_config.json (cwd)
      3. <script_dir>/clip_config.json"""
    if explicit_path:
        candidates = [Path(explicit_path)]
    else:
        script_dir = Path(__file__).resolve().parent
        candidates = [
            Path.cwd() / "clip_config.json",
            script_dir / "clip_config.json",
        ]
    for p in candidates:
        if p.exists():
            with open(p, "r") as f:
                cfg = json.load(f)
            print(f"Config loaded    : {p}")
            return cfg
    return {}


# ---------------------------------------------------------------------------
# PLY binary read / write (same as fuse_ply.py)
# ---------------------------------------------------------------------------
def read_ply(filepath: str):
    """Read a binary little-endian PLY file.
    Returns (header_lines, properties, vertices) where vertices is (N, P) float32."""
    with open(filepath, "rb") as f:
        header_lines = []
        while True:
            line = f.readline().decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            header_lines.append(line)
            if line == "end_header":
                break

    vertex_count = 0
    properties = []
    for line in header_lines:
        if line.startswith("element vertex "):
            vertex_count = int(line.split()[-1])
        elif line.startswith("property "):
            properties.append(line)

    num_props = len(properties)
    header_text = "\n".join(header_lines) + "\n"
    header_len = len(header_text.encode("utf-8"))

    with open(filepath, "rb") as f:
        f.seek(header_len)
        raw = f.read()

    expected = vertex_count * num_props * 4
    if len(raw) < expected:
        print(f"WARNING: {filepath}: expected {expected} bytes, got {len(raw)}")

    vertices = np.frombuffer(raw[:expected], dtype=np.float32).reshape(vertex_count, num_props)
    return header_lines, properties, vertices


def write_ply(filepath: str, header_lines, properties, vertices):
    """Write a binary little-endian PLY file with N vertices."""
    total = vertices.shape[0]

    with open(filepath, "wb") as f:
        for line in header_lines:
            if line.startswith("element vertex "):
                f.write(f"element vertex {total}\n".encode("utf-8"))
            else:
                f.write(f"{line}\n".encode("utf-8"))
        f.write(vertices.tobytes())


# ---------------------------------------------------------------------------
# circle fitting (same algorithm as fuse_ply.py)
# ---------------------------------------------------------------------------
def fit_circle(points: np.ndarray):
    """Fit a 3D circle via SVD plane + least-squares.
    Returns (center, normal, r_fit, u1, u2)."""
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
    return center, normal, r_fit, u1, u2


# ---------------------------------------------------------------------------
# cylinder denoise: grid-based region-growing from density peak
# ---------------------------------------------------------------------------
def cylinder_denoise(verts, center, normal, u1, u2, effective_r,
                     height_up, height_down, min_points, grid_cell=0.15):
    """Remove isolated floater points inside the cylinder.
    Grid-bins cylinder points, finds the density-peak cell, then region-grows
    from it across 8-neighbor cells.  Points in the grown region are kept;
    points inside the cylinder but outside the grown region are discarded.
    Points outside the cylinder are left untouched.
    Returns (filtered_verts, n_removed)."""
    shifted = verts[:, :3] - center
    pts_2d = np.column_stack([shifted @ u1, shifted @ u2])
    signed_dist = shifted @ normal
    radial = np.linalg.norm(pts_2d, axis=1)

    in_cyl = (radial <= effective_r) & (signed_dist >= -height_down) & (signed_dist <= height_up)
    cyl_indices = np.where(in_cyl)[0]

    if len(cyl_indices) < min_points:
        return verts, 0

    cyl_2d = pts_2d[cyl_indices]

    # ---- grid bin ----
    mins = np.min(cyl_2d, axis=0) - grid_cell
    maxs = np.max(cyl_2d, axis=0) + grid_cell
    nx = max(1, int(np.ceil((maxs[0] - mins[0]) / grid_cell)))
    ny = max(1, int(np.ceil((maxs[1] - mins[1]) / grid_cell)))

    ix = np.clip(np.floor((cyl_2d[:, 0] - mins[0]) / grid_cell).astype(np.int32), 0, nx - 1)
    iy = np.clip(np.floor((cyl_2d[:, 1] - mins[1]) / grid_cell).astype(np.int32), 0, ny - 1)
    flat = ix * ny + iy
    counts = np.bincount(flat, minlength=nx * ny)

    # ---- density peak ----
    peak_flat = int(np.argmax(counts))
    if counts[peak_flat] < min_points:
        return verts, 0

    # ---- 8-neighbor region-growing ----
    visited = np.zeros(nx * ny, dtype=bool)
    in_cluster = np.zeros(nx * ny, dtype=bool)
    queue = [peak_flat]
    visited[peak_flat] = True

    while queue:
        cell = queue.pop()
        if counts[cell] < min_points:
            continue
        in_cluster[cell] = True
        cx, cy = cell // ny, cell % ny
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx_c, ny_c = cx + dx, cy + dy
                if 0 <= nx_c < nx and 0 <= ny_c < ny:
                    nf = nx_c * ny + ny_c
                    if not visited[nf]:
                        visited[nf] = True
                        queue.append(nf)

    # ---- map back: keep cylinder points that land in grown cells ----
    cyl_keep = in_cluster[flat]
    n_removed = int((~cyl_keep).sum())

    final_mask = np.ones(len(verts), dtype=bool)
    final_mask[cyl_indices] = cyl_keep

    return verts[final_mask], n_removed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    # ---- first pass: extract --config from CLI ---------------------------
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", type=str, default=None)
    pre_args, remaining = pre_parser.parse_known_args()

    cfg = load_config(pre_args.config)

    # ---- build main parser with config values as defaults ----------------
    parser = argparse.ArgumentParser(
        description="Clip largest Gaussian splats by volume from each PLY"
    )
    parser.add_argument("--config", type=str, default=None,
                        help="Path to JSON config file (auto-discovered if omitted)")
    parser.add_argument("--path", required=("path" not in cfg),
                        default=cfg.get("path"),
                        help="Path to project directory (e.g. CameraData/05)")
    parser.add_argument("--clip-percent", type=float,
                        default=cfg.get("clip_percent", 10.0),
                        help="Remove the top X%% of GS points by volume (default: 10)")
    parser.add_argument("--denoise", action="store_true",
                        default=cfg.get("denoise", False),
                        help="Enable cylinder-based isolated floater removal")
    parser.add_argument("--max-index", type=int,
                        default=cfg.get("max_index"),
                        help="[denoise] Cameras id=0..max_index for circle fitting")
    parser.add_argument("--radius-scale", type=float,
                        default=cfg.get("radius_scale", 1.0),
                        help="[denoise] Scale the fitted circle radius (default: 1.0)")
    parser.add_argument("--height-up", type=float,
                        default=cfg.get("height_up"),
                        help="[denoise] Cylinder height above fitted plane (m)")
    parser.add_argument("--height-down", type=float,
                        default=cfg.get("height_down"),
                        help="[denoise] Cylinder height below fitted plane (m)")
    parser.add_argument("--denoise-min-points", type=int,
                        default=cfg.get("denoise_min_points", 30),
                        help="[denoise] Min points per grid cell for region-growing (default: 30)")
    args = parser.parse_args(remaining)

    if not (0.0 <= args.clip_percent <= 100.0):
        print("ERROR: --clip-percent must be between 0 and 100")
        sys.exit(1)

    # resolve paths
    proj_dir = Path(args.path)
    if not proj_dir.is_absolute():
        proj_dir = Path.cwd() / proj_dir
    proj_dir = proj_dir.resolve()
    if not proj_dir.is_dir():
        print(f"ERROR: directory not found: {proj_dir}")
        sys.exit(1)

    out_dir = proj_dir.parent / f"{proj_dir.name}-clip"
    os.makedirs(out_dir, exist_ok=True)

    # ----- cylinder setup (only if denoise enabled) -------------------------
    center = normal = u1 = u2 = None
    effective_r = height_up = height_down = None

    if args.denoise:
        if args.max_index is None:
            print("ERROR: --denoise requires --max-index")
            sys.exit(1)
        if args.height_up is None or args.height_down is None:
            print("ERROR: --denoise requires --height-up and --height-down")
            sys.exit(1)

        cameras_path = proj_dir / "cameras.json"
        if not cameras_path.exists():
            print(f"ERROR: {cameras_path} not found (required for --denoise)")
            sys.exit(1)

        with open(cameras_path, "r") as f:
            cameras = json.load(f)
        cam_subset = cameras[:args.max_index + 1]
        if len(cam_subset) < 3:
            print(f"ERROR: need at least 3 cameras for circle fitting, got {len(cam_subset)}")
            sys.exit(1)

        positions = np.array([c["position"] for c in cam_subset])
        center, normal, r_fit, u1, u2 = fit_circle(positions)
        effective_r = r_fit * args.radius_scale
        height_up = args.height_up
        height_down = args.height_down

        print(f"Denoise setup    : max_index=0..{args.max_index} ({len(cam_subset)} cameras)")
        print(f"  Circle center  : [{center[0]:.4f}, {center[1]:.4f}, {center[2]:.4f}]")
        print(f"  Fit radius     : {r_fit:.4f}  (scaled: {effective_r:.4f})")
        print(f"  Cylinder       : [-{height_down:.4f}, +{height_up:.4f}] along normal")
        print(f"  Region-grow    : min {args.denoise_min_points} pts/cell")

    # ----- discover PLY files ----------------------------------------------
    plys_dir = proj_dir / "plys"
    if plys_dir.is_dir():
        ply_files = sorted(plys_dir.glob("*.ply"))
    else:
        ply_files = sorted(proj_dir.glob("*.ply"))

    if not ply_files:
        print(f"ERROR: no .ply files found in {proj_dir} or {plys_dir}")
        sys.exit(1)

    clip_pct = args.clip_percent
    keep_frac = 1.0 - clip_pct / 100.0

    print(f"Input           : {proj_dir}")
    print(f"Output          : {out_dir}")
    print(f"Clip percent    : {clip_pct:.1f}%  (keep bottom {keep_frac*100:.1f}%)")
    print(f"PLY files       : {len(ply_files)}")

    for ply_path in ply_files:
        print(f"\nProcessing: {ply_path.name} ...")

        header_lines, properties, verts = read_ply(str(ply_path))
        n_orig = verts.shape[0]

        # locate scale_0, scale_1, scale_2 column indices
        scale_cols = []
        for ci, prop_line in enumerate(properties):
            name = prop_line.split()[-1]
            if name in ("scale_0", "scale_1", "scale_2"):
                scale_cols.append(ci)

        if len(scale_cols) != 3:
            print(f"  WARNING: expected 3 scale columns, found {len(scale_cols)}. Skip.")
            continue

        s0, s1, s2 = scale_cols
        # Scale values in PLY are stored as log(scale), same convention as
        # SuperSplat.  True volume = exp(s0)*exp(s1)*exp(s2) = exp(s0+s1+s2).
        # Since exp() is monotonic, sorting by sum of log-scales gives the
        # same ranking without float-overflow risk.
        volume = verts[:, s0] + verts[:, s1] + verts[:, s2]

        if clip_pct <= 0:
            clipped = verts
        else:
            # sort descending by volume (largest first);
            # skip the first n_remove (largest), keep the rest (= smallest)
            order = np.argsort(-volume)
            n_remove = int(n_orig * clip_pct / 100.0)
            keep_idx = order[n_remove:]
            clipped = verts[keep_idx]

        n_clipped = clipped.shape[0]
        n_removed_vol = n_orig - n_clipped

        # ----- denoise (optional) ------------------------------------------
        n_denoised = 0
        if args.denoise:
            clipped, n_denoised = cylinder_denoise(
                clipped, center, normal, u1, u2, effective_r,
                height_up, height_down, args.denoise_min_points)

        n_final = clipped.shape[0]

        out_path = out_dir / ply_path.name
        write_ply(str(out_path), header_lines, properties, clipped)

        parts = [f"{n_orig} pts -> {n_final} kept"]
        if n_removed_vol > 0:
            parts.append(f"vol-clip {n_removed_vol}")
        if n_denoised > 0:
            parts.append(f"denoise {n_denoised}")
        print(f"  {'  |  '.join(parts)}")

    print(f"\nDone. {len(ply_files)} files -> {out_dir}")


if __name__ == "__main__":
    main()
