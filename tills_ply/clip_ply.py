#!/usr/bin/env python3
"""
Clip (remove) the largest N% of Gaussian splats from each PLY file, ranked by
volume proxy = scale_0 + scale_1 + scale_2 (sum of log-scales, equivalent to
SuperSplat's exp(s0)*exp(s1)*exp(s2) ranking since exp is monotonic).

Optionally (--denoise), remove isolated floater Gaussians. Two methods:

  region-grow (default): Grid-based region-growing from the density peak.
    Projects cylinder points to 2D, grids them, finds the densest cell, then
    grows outward via 8-neighbor connectivity. Only cells with ≥ min_points
    are included. Isolated cells with enough points but not connected to the
    main body are discarded. Requires cameras.json for circle fitting.

  components: 3D connected-components via voxelisation + 26-neighbor BFS.
    Small components (< min_points) are discarded. Does NOT require
    cameras.json. Good for sparse floaters; less effective against isolated
    dense clusters.

Optionally (--ring-delete), remove points in a ring-shaped region between two
concentric circles fitted to camera positions.

Outputs processed PLYs to {path}-clip/, keeping original filenames.

Usage:
  # volume-clip only
  python tills_ply/clip_ply.py

  # volume-clip + region-grow denoise
  python tills_ply/clip_ply.py --denoise

  # volume-clip + region-grow denoise + ring delete
  python tills_ply/clip_ply.py --denoise --ring-delete

  # volume-clip + 3D components denoise
  python tills_ply/clip_ply.py --denoise --denoise-method components

Config file (JSON) — all keys optional; CLI args take precedence:
  {
    "path": "CameraData/05",
    "clip_percent": 10,
    "denoise": true,
    "denoise_method": "region-grow",
    "denoise_min_points": 30,
    "denoise_grid_cell": 0.15,
    "denoise_voxel_size": 0.30,
    "height_up": 2.0,
    "height_down": 0.5,
    "ring_delete": true,
    "max_index": 89,
    "radius_scale": 0.5,
    "ring_height_up": 1.5,
    "ring_height_down": 0.3,
    "ring_outer_delta": 0.2,
    "ring_inner_delta": 0.3
  }

---- 参数说明 ------------------------------------------------------------
  clip_percent        删除体积最大的前 X% GS 点 (0~100)。默认 10。
                      PLY 中 scale 以 log 空间存储；体积排名 = s0+s1+s2
                      (等价 SuperSplat 的 exp(s0)*exp(s1)*exp(s2))。
                      设为 0 则不删除任何点（纯拷贝）。

  -- 以下参数在 --denoise 时生效 -----------------------------------------
  denoise             启用去噪 (bool, 默认 false)。
  denoise_method      去噪方法: "region-grow" (默认) 或 "components"。
                      region-grow 需要 cameras.json 存在。
  denoise_min_points  region-grow: 每个 grid cell 的最低点数阈值 (默认 30)。
                      components: 连通分量保留的最低点数 (默认 50)。
  denoise_grid_cell   [region-grow] 2D 网格边长 (米, 默认 0.15)。
  denoise_voxel_size  [components] 3D 体素边长 (米, 默认 0.30)。
  height_up/down      [region-grow] 圆柱沿拟合平面法向量的上下高度 (米)。
  max_index           [region-grow + ring-delete] 拟合圆所用的相机范围。
  radius_scale        [region-grow + ring-delete] 拟合圆半径缩放系数。

  -- 以下参数仅在 --ring-delete 时生效 -----------------------------------
  ring_delete         启用环形区域点删除 (bool, 默认 false)。
  ring_outer_delta    外环扩张量 (米, 默认 0.5)。
  ring_inner_delta    内环收缩量 (米, 默认 0.3)。
  ring_height_up/down 环形区域沿平面法向量的上下高度 (米)。
-----------------------------------------------------------------------
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

from ply_utils import read_ply, write_ply, fit_circle


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
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            print(f"Config loaded    : {p}")
            return cfg
    return {}


# ---------------------------------------------------------------------------
# 3D connected-components denoise
# ---------------------------------------------------------------------------

def _denoise_components(verts, voxel_size=0.15, min_points=50):
    """Remove isolated floater Gaussians via 3D connected components.

    The point cloud is voxelised at *voxel_size* resolution.  26-connected
    components are discovered by BFS.  Components with fewer than *min_points*
    points are discarded as floaters / artefacts; all larger components are
    kept intact.

    Because the person's feet and the ground surface occupy adjacent (or the
    same) 3D voxels, they naturally form one large connected component —
    unlike the old 2D cylinder method which lost the connection in projection.
    Sparse floaters at any height form their own tiny components and are
    removed regardless of their spatial location.

    Returns (filtered_verts, n_removed).
    """
    xyz = verts[:, :3]
    n_total = xyz.shape[0]

    # ---- voxelize -------------------------------------------------------
    mins = xyz.min(axis=0)
    voxel_idx = np.floor((xyz - mins) / voxel_size).astype(np.int32)  # (N, 3)

    # Build voxel → point-indices map
    voxel_to_pts = {}
    for i in range(n_total):
        key = (int(voxel_idx[i, 0]), int(voxel_idx[i, 1]), int(voxel_idx[i, 2]))
        if key in voxel_to_pts:
            voxel_to_pts[key].append(i)
        else:
            voxel_to_pts[key] = [i]

    n_voxels = len(voxel_to_pts)
    if n_voxels <= 1:
        return verts, 0

    # ---- 26-connected components via BFS --------------------------------
    visited = set()
    components = []                     # list of lists of point indices

    for seed in voxel_to_pts:
        if seed in visited:
            continue

        # BFS from this seed
        comp_pts = []
        queue = [seed]
        visited.add(seed)

        # Pre-compute neighbour offsets (avoids deep nested loops)
        _neighbour_offsets = [
            (dx, dy, dz)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
            if not (dx == 0 and dy == 0 and dz == 0)
        ]

        while queue:
            v = queue.pop()
            comp_pts.extend(voxel_to_pts[v])

            vx, vy, vz = v
            for dx, dy, dz in _neighbour_offsets:
                nb = (vx + dx, vy + dy, vz + dz)
                if nb in voxel_to_pts and nb not in visited:
                    visited.add(nb)
                    queue.append(nb)

        components.append(comp_pts)

    if len(components) <= 1:
        return verts, 0

    # ---- discard components smaller than min_points ---------------------
    components.sort(key=len, reverse=True)
    keep_mask = np.zeros(n_total, dtype=bool)
    n_kept_comps = 0
    for comp in components:
        if len(comp) < min_points:
            break
        keep_mask[comp] = True
        n_kept_comps += 1

    n_removed = n_total - int(keep_mask.sum())
    return verts[keep_mask], n_removed


# ---------------------------------------------------------------------------
# grid-based region-growing denoise (original method)
# ---------------------------------------------------------------------------
def _denoise_region_grow(verts, center, normal, u1, u2, effective_r,
                         height_up, height_down, min_points, grid_cell=0.15):
    """Remove isolated floater points inside the cylinder via grid-based
    region-growing from the density peak.

    Projects cylinder points to the 2D circle plane, grids them at *grid_cell*
    resolution, finds the densest cell, then region-grows outward via 8-neighbor
    connectivity.  Only cells whose point count ≥ *min_points* are included in
    the grown region.

    Points inside the cylinder but outside the grown region are discarded as
    artefacts.  Points outside the cylinder (radial or height) are left
    untouched — they were already excluded by the volume-clip step.

    Isolated dense cells that are not 8-connected to the main body are naturally
    excluded because the region-growing cannot reach them.

    Returns (filtered_verts, n_removed).
    """
    shifted = verts[:, :3] - center
    pts_2d = np.column_stack([shifted @ u1, shifted @ u2])
    signed_dist = shifted @ normal
    radial = np.linalg.norm(pts_2d, axis=1)

    in_cyl = (radial <= effective_r) & (signed_dist >= -height_down) & (signed_dist <= height_up)
    cyl_indices = np.where(in_cyl)[0]

    if len(cyl_indices) < min_points:
        return verts, 0

    cyl_2d = pts_2d[cyl_indices]

    # ---- grid bin ----------------------------------------------------------
    mins = np.min(cyl_2d, axis=0) - grid_cell
    maxs = np.max(cyl_2d, axis=0) + grid_cell
    nx = max(1, int(np.ceil((maxs[0] - mins[0]) / grid_cell)))
    ny = max(1, int(np.ceil((maxs[1] - mins[1]) / grid_cell)))

    ix = np.clip(np.floor((cyl_2d[:, 0] - mins[0]) / grid_cell).astype(np.int32), 0, nx - 1)
    iy = np.clip(np.floor((cyl_2d[:, 1] - mins[1]) / grid_cell).astype(np.int32), 0, ny - 1)
    flat = ix * ny + iy
    counts = np.bincount(flat, minlength=nx * ny)

    # ---- density peak ------------------------------------------------------
    peak_flat = int(np.argmax(counts))
    if counts[peak_flat] < min_points:
        return verts, 0

    # ---- 8-neighbor region-growing -----------------------------------------
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

    # ---- map back: keep cylinder points that land in grown cells -----------
    cyl_keep = in_cluster[flat]
    n_removed = int((~cyl_keep).sum())

    final_mask = np.ones(len(verts), dtype=bool)
    final_mask[cyl_indices] = cyl_keep

    return verts[final_mask], n_removed


# ---------------------------------------------------------------------------
# ring delete: remove points in the ring between two concentric circles
# ---------------------------------------------------------------------------
def ring_delete(verts, center, normal, effective_r, outer_delta, inner_delta,
                height_up, height_down):
    """Delete points in the ring [inner_r, outer_r] within height bounds.
    inner_r = effective_r - inner_delta  (the shrunk circle C)
    outer_r = effective_r + outer_delta  (the expanded circle B)
    Returns (filtered_verts, n_removed)."""
    ring_outer = effective_r + outer_delta
    ring_inner = effective_r - inner_delta
    if ring_inner <= 0:
        print(f"ERROR: ring_inner_delta ({inner_delta}) >= effective radius ({effective_r:.4f})")
        sys.exit(1)

    xyz = verts[:, :3]
    shifted = xyz - center
    signed_dist = shifted @ normal
    proj = xyz - np.outer(signed_dist, normal)
    radial = np.linalg.norm(proj - center, axis=1)

    in_ring = (
        (radial >= ring_inner) &
        (radial <= ring_outer) &
        (signed_dist >= -height_down) &
        (signed_dist <= height_up)
    )
    n_removed = int(in_ring.sum())
    return verts[~in_ring], n_removed


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
                        help="Enable denoising (see --denoise-method)")
    parser.add_argument("--denoise-method", type=str,
                        default=cfg.get("denoise_method", "region-grow"),
                        choices=["region-grow", "components"],
                        help="Denoise method: 'region-grow' (grid density-peak + 8-neighbor, "
                             "needs cameras.json) or 'components' (3D voxel 26-conn BFS). "
                             "Default: region-grow")
    parser.add_argument("--denoise-min-points", type=int,
                        default=cfg.get("denoise_min_points", 30),
                        help="[denoise] region-grow: min points per grid cell (default 30); "
                             "components: min points per component (default 50)")
    parser.add_argument("--denoise-grid-cell", type=float,
                        default=cfg.get("denoise_grid_cell", 0.15),
                        help="[denoise region-grow] 2D grid cell size in metres (default: 0.15)")
    parser.add_argument("--denoise-voxel-size", type=float,
                        default=cfg.get("denoise_voxel_size", 0.30),
                        help="[denoise components] 3D voxel size in metres (default: 0.30)")
    parser.add_argument("--height-up", type=float,
                        default=cfg.get("height_up"),
                        help="[denoise region-grow] Cylinder height above fitted plane (m)")
    parser.add_argument("--height-down", type=float,
                        default=cfg.get("height_down"),
                        help="[denoise region-grow] Cylinder height below fitted plane (m)")
    parser.add_argument("--max-index", type=int,
                        default=cfg.get("max_index"),
                        help="[denoise region-grow + ring-delete] Cameras id=0..max_index for circle fitting")
    parser.add_argument("--radius-scale", type=float,
                        default=cfg.get("radius_scale", 1.0),
                        help="[denoise region-grow + ring-delete] Scale the fitted circle radius (default: 1.0)")
    parser.add_argument("--ring-delete", action="store_true",
                        default=cfg.get("ring_delete", False),
                        help="Enable ring-region point deletion between two concentric circles")
    parser.add_argument("--ring-outer-delta", type=float,
                        default=cfg.get("ring_outer_delta", 0.5),
                        help="[ring-delete] Outer radius expansion in meters (default: 0.5)")
    parser.add_argument("--ring-inner-delta", type=float,
                        default=cfg.get("ring_inner_delta", 0.3),
                        help="[ring-delete] Inner radius contraction in meters (default: 0.3)")
    parser.add_argument("--ring-height-up", type=float,
                        default=cfg.get("ring_height_up"),
                        help="[ring-delete] Height above fitted plane for ring deletion (m)")
    parser.add_argument("--ring-height-down", type=float,
                        default=cfg.get("ring_height_down"),
                        help="[ring-delete] Height below fitted plane for ring deletion (m)")
    args = parser.parse_args(remaining)

    if not (0.0 <= args.clip_percent <= 100.0):
        print("ERROR: --clip-percent must be between 0 and 100")
        sys.exit(1)

    if args.ring_delete:
        if args.ring_height_up is None or args.ring_height_down is None:
            print("ERROR: --ring-delete requires --ring-height-up and --ring-height-down")
            sys.exit(1)
        if args.ring_outer_delta <= 0:
            print("ERROR: --ring-outer-delta must be positive")
            sys.exit(1)
        if args.ring_inner_delta <= 0:
            print("ERROR: --ring-inner-delta must be positive")
            sys.exit(1)

    needs_circle = args.ring_delete or (args.denoise and args.denoise_method == "region-grow")
    if needs_circle:
        if args.max_index is None:
            print("ERROR: --ring-delete / --denoise with region-grow requires --max-index")
            sys.exit(1)

    if args.denoise and args.denoise_method == "region-grow":
        if args.height_up is None or args.height_down is None:
            print("ERROR: --denoise region-grow requires --height-up and --height-down")
            sys.exit(1)
        if args.denoise_grid_cell <= 0:
            print("ERROR: --denoise-grid-cell must be positive")
            sys.exit(1)
        if args.radius_scale <= 0:
            print("ERROR: --radius-scale must be positive")
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

    # ----- circle setup (for ring-delete and/or region-grow denoise) ---------
    center = normal = u1 = u2 = None
    effective_r = None

    if needs_circle:
        cameras_path = proj_dir / "cameras.json"
        if not cameras_path.exists():
            print(f"ERROR: {cameras_path} not found")
            sys.exit(1)

        with open(cameras_path, "r", encoding="utf-8") as f:
            cameras = json.load(f)
        cam_subset = cameras[:args.max_index + 1]
        if len(cam_subset) < 3:
            print(f"ERROR: need at least 3 cameras for circle fitting, got {len(cam_subset)}")
            sys.exit(1)

        positions = np.array([c["position"] for c in cam_subset])
        center, normal, r_fit, u1, u2 = fit_circle(positions)
        effective_r = r_fit * args.radius_scale

        print(f"Circle setup     : max_index=0..{args.max_index} ({len(cam_subset)} cameras)")
        print(f"  Circle center  : [{center[0]:.4f}, {center[1]:.4f}, {center[2]:.4f}]")
        print(f"  Fit radius     : {r_fit:.4f}  (scaled: {effective_r:.4f})")

    if args.ring_delete:
        ring_outer = effective_r + args.ring_outer_delta
        ring_inner = effective_r - args.ring_inner_delta
        if ring_inner <= 0:
            print(f"ERROR: ring_inner_delta ({args.ring_inner_delta}) makes inner radius <= 0 "
                  f"(effective_r={effective_r:.4f}, ring_inner={ring_inner:.4f})")
            sys.exit(1)
        print(f"  Ring delete    : inner={ring_inner:.4f}  outer={ring_outer:.4f}")
        print(f"  Ring height    : [-{args.ring_height_down:.4f}, +{args.ring_height_up:.4f}]")

    if args.denoise:
        if args.denoise_method == "region-grow":
            print(f"  Denoise        : region-grow  grid={args.denoise_grid_cell:.2f}m  "
                  f"min_points={args.denoise_min_points}")
            print(f"  Denoise height : [-{args.height_down:.4f}, +{args.height_up:.4f}]")
        else:
            print(f"  Denoise        : 3D components  voxel={args.denoise_voxel_size:.2f}m  "
                  f"min_points={args.denoise_min_points}")

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
            if args.denoise_method == "region-grow":
                clipped, n_denoised = _denoise_region_grow(
                    clipped, center, normal, u1, u2, effective_r,
                    args.height_up, args.height_down,
                    args.denoise_min_points, args.denoise_grid_cell,
                )
            else:
                clipped, n_denoised = _denoise_components(
                    clipped,
                    voxel_size=args.denoise_voxel_size,
                    min_points=args.denoise_min_points,
                )

        # ----- ring delete (optional) -------------------------------------
        n_ring = 0
        if args.ring_delete:
            clipped, n_ring = ring_delete(
                clipped, center, normal, effective_r,
                args.ring_outer_delta, args.ring_inner_delta,
                args.ring_height_up, args.ring_height_down)

        n_final = clipped.shape[0]

        out_path = out_dir / ply_path.name
        write_ply(str(out_path), header_lines, properties, clipped)

        parts = [f"{n_orig} pts -> {n_final} kept"]
        if n_removed_vol > 0:
            parts.append(f"vol-clip {n_removed_vol}")
        if n_denoised > 0:
            parts.append(f"denoise {n_denoised}")
        if n_ring > 0:
            parts.append(f"ring {n_ring}")
        print(f"  {'  |  '.join(parts)}")

    print(f"\nDone. {len(ply_files)} files -> {out_dir}")


if __name__ == "__main__":
    main()
