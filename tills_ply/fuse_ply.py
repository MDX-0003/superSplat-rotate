#!/usr/bin/env python3
"""
Fuse selected PLY point clouds using a cylindrical region defined by a circle
fitted to camera positions.

- All points from the "main" PLY (first user selection) are kept in full.
- Points from other selected PLYs are filtered using adaptive ground-surface
  detection: the cylinder's radial bound and upper height bound act as hard
  cutoffs, while the lower bound (ground side) uses a grid-based local-minimum
  algorithm that removes ONLY the ground surface while preserving foot/ankle
  points that sit at a similar height.
  camera index consider in circle[0,max_index]

Usage:
  # CLI only
  python tills_ply/fuse_ply.py --path CameraData/04 --max-index 89 --height-up 2 --height-down 0.5 --radius-scale 0.5 --bias

  # with config file (auto-discovered or explicit)
  python tills_ply/fuse_ply.py
  python tills_ply/fuse_ply.py --config my_config.json

Config file (JSON) — all keys optional; CLI args take precedence:
  {
    "path": "CameraData/04",
    "max_index": 89,
    "radius_scale": 0.5,
    "height_up": 2,
    "height_down": 0.5,
    "bias": true,
    "bias_margin": 0.5,
    "bias_radius_percentile": 20,
    "indices": [1, 2, 3],
    "output_subfix": ""
  }

---- 参数说明 ------------------------------------------------------------
  max_index           拟合圆所用的相机范围 id=0..max_index (从0开始,包含max_index)
                      这些相机应围绕场景中心大致排成一个圆。
  radius_scale        对拟合圆半径的缩放系数。 <1 收紧圆柱,只保留更靠近圆心
                      的点; >1 放宽。典型值 0.3~1.0。
  height_up           圆柱沿拟合平面法向量上方的保留高度(米)。法向量由 SVD 给出,
                      指向场景"上方"未必是世界 Z 轴。人物身高约 2m,建议设 2~3。
  height_down         地面侧搜索范围(米)。signed_dist < -height_down 的点无条件
                      删除(安全底板); signed_dist 在 [-height_down, height_up]
                      范围内的点由自适应网格算法识别地面表面并精准切除。
                      典型值 0.3~0.5。越小则地面检测的搜索空间越小。
  bias                是否启用人物重叠分离。开启后,脚本会找出每个 PLY 在拟合
                      平面上的密度峰值(网格 bin + argmax),隔离人物核心点
                      (峰值周围 0.5m),比较核心质心之间的 overlap,对重合的
                      非 main PLY 整体施加 XY 平面平移。越界点会被径向 clamp
                      回圆柱表面。
  bias_margin         分离后两个人物核心之间的额外安全距离(米)。
                      越大越"暴力",越小越保守。典型值 0.05~0.5。
  bias_radius_percentile
                      核心半径的百分位数(0~100)。值越小估算的核心越紧(只包含
                      最高密度区),值越大核心越宽(包含更多周围点)。典型值 20~50。
  indices             要融合的 PLY 索引列表(空格分隔或 JSON 数组)。首位 = main
                      PLY(全部点保留)。省略则进入交互式输入。
  output_subfix       输出文件名的后缀。为空则自动取所有参与融合的 index 拼接
                      (如 indices=[1,2] → ...combine_1-2.ply)；显式指定则使用该值。
                      用于区分不同参数组合的产物。

---- 自适应地面检测原理 --------------------------------------------------
  旧的 height_down 硬阈值无法区分地面 GS 点和脚部 GS 点,因为两者的
  signed_dist 在拟合平面附近重叠。新算法利用"地面是每个 2D 网格单元中
  最底层点"这一几何特征:

    1. 将待过滤 PLY 的点投影到拟合平面 → (u1, u2) 坐标
    2. 在 2D 平面上划分网格 (默认 0.1m)
    3. 每个网格取 signed_dist 最小值作为"局部地面高度"
    4. signed_dist 距局部地面高度 ≤ 3cm 的点 → 地面表面 → 删除
    5. 高于局部地面的点 → 人物/物体 → 保留

  这保证了脚部点(虽然 signed_dist 很小,但在其网格中它不是最低的那层)
  不会被错误移除。

  网格精度和容忍带可作为模块常量 GROUND_CELL_SIZE / GROUND_EPS 调整。
-----------------------------------------------------------------------
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

from ply_utils import read_ply, write_ply, fit_circle


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------
def load_config(explicit_path=None):
    """Load a JSON config file.  Search order:
      1. explicit --config path
      2. ./fuse_config.json (cwd)
      3. <script_dir>/fuse_config.json"""
    if explicit_path:
        candidates = [Path(explicit_path)]
    else:
        script_dir = Path(__file__).resolve().parent
        candidates = [
            Path.cwd() / "fuse_config.json",
            script_dir / "fuse_config.json",
        ]
    for p in candidates:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            print(f"Config loaded    : {p}")
            return cfg
    return {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def longest_common_prefix(strings):
    """Return the longest common prefix of a list of strings."""
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


# ---------------------------------------------------------------------------
# adaptive ground-surface filter (grid-based local-minimum detection)
# ---------------------------------------------------------------------------

# Grid resolution for the 2D projection plane (metres).
# Smaller = finer ground discrimination; 0.10 is a good default for outdoor scenes.
_GROUND_CELL_SIZE = 0.10

# Tolerance band above the local cell minimum (metres).
# Points within this distance of the cell's lowest point are classified as
# ground surface.  0.03 works well for typical 3DGS reconstruction noise.
_GROUND_EPS = 0.03

# Phase 2: cross-PLY residual ground suppression.
# After Phase 1 removes ground within each non-main PLY, Phase 2 checks
# surviving points against the MAIN PLY's person footprint: if a non-main
# point sits at the main PLY's ground level in a cell where the main PLY
# has person points, it is removed.  This prevents residual ground from
# piling up directly under the person's feet.
_CROSS_GROUND_EPS = 0.05        # tolerance for "at main's ground level" (m)
_PERSON_ABOVE_GROUND = 0.15     # min height above local ground → "person" (m)


def _filter_ground_adaptive(verts, center, normal, u1, u2,
                            effective_r, height_up, height_down):
    """Filter a non-main PLY: remove ground-surface points adaptively.

    Uses a grid-based local-minimum algorithm in the 2D projection plane.
    For each grid cell the point with the smallest signed distance to the
    plane defines the "local ground level"; any point within _GROUND_EPS of
    that level is treated as ground surface and removed.

    Hard constraints (applied before grid analysis):
      * radial distance must be <= effective_r (cylinder wall)
      * signed_dist must be <= height_up (ceiling)
      * signed_dist must be >= -height_down (safety floor — points below
        this are unconditionally discarded)

    Returns (kept_verts, stats_dict).
    """
    xyz = verts[:, :3]
    signed_dist = (xyz - center) @ normal                      # (N,)
    proj = xyz - np.outer(signed_dist, normal)
    radial = np.linalg.norm(proj - center, axis=1)             # (N,)

    # -- geometric hard filters -------------------------------------------
    in_cylinder = radial <= effective_r
    below_ceiling = signed_dist <= height_up
    above_safety = signed_dist >= -height_down
    geo_mask = in_cylinder & below_ceiling & above_safety      # passes hard filters

    n_radial_cut = int((~in_cylinder).sum())
    n_ceiling_cut = int((in_cylinder & ~below_ceiling).sum())
    n_safety_cut = int((in_cylinder & below_ceiling & ~above_safety).sum())

    # -- grid-based ground-surface detection ------------------------------
    candidates_idx = np.where(geo_mask)[0]

    if len(candidates_idx) < 100:
        # Too few points — fall back to pure geometric filter.
        kept = verts[geo_mask]
        return kept, {
            "total": verts.shape[0], "kept": kept.shape[0],
            "ground_removed": 0, "radial_cut": n_radial_cut,
            "ceiling_cut": n_ceiling_cut, "safety_cut": n_safety_cut,
        }

    candidates_xyz = xyz[candidates_idx]
    candidates_sd = signed_dist[candidates_idx]

    # Project candidates to the 2D plane
    shifted = candidates_xyz - center
    coords_2d = np.column_stack([shifted @ u1, shifted @ u2])  # (K, 2)

    # Build 2D grid
    cs = _GROUND_CELL_SIZE
    mins = coords_2d.min(axis=0) - cs
    maxs = coords_2d.max(axis=0) + cs
    nx = max(1, int(np.ceil((maxs[0] - mins[0]) / cs)))
    ny = max(1, int(np.ceil((maxs[1] - mins[1]) / cs)))

    ix = np.clip(
        np.floor((coords_2d[:, 0] - mins[0]) / cs).astype(np.int32), 0, nx - 1)
    iy = np.clip(
        np.floor((coords_2d[:, 1] - mins[1]) / cs).astype(np.int32), 0, ny - 1)
    cell_flat = ix * ny + iy                                    # (K,) cell index

    # Per-cell minimum signed distance — the "local ground level"
    n_cells = nx * ny
    cell_min_sd = np.full(n_cells, np.inf, dtype=np.float32)
    np.minimum.at(cell_min_sd, cell_flat, candidates_sd.astype(np.float32))

    # Cell occupancy: only classify points as ground when the cell has enough
    # neighbours to give the "local minimum" concept meaning.  In sparse cells
    # (< 3 pts) every point would trivially be the minimum — skip those cells.
    cell_counts = np.bincount(cell_flat, minlength=n_cells)
    _MIN_CELL_PTS = 3
    cell_valid = cell_counts >= _MIN_CELL_PTS

    # Points within _GROUND_EPS of the cell minimum AND in a dense enough cell
    local_min = cell_min_sd[cell_flat]                          # (K,)
    is_ground_candidate = (
        ((candidates_sd - local_min) <= _GROUND_EPS) &
        cell_valid[cell_flat]
    )

    # Map back to full point cloud
    ground_mask = np.zeros(verts.shape[0], dtype=bool)
    ground_mask[candidates_idx[is_ground_candidate]] = True

    n_ground = int(ground_mask.sum())

    # Final keep mask
    keep_mask = geo_mask & ~ground_mask
    kept = verts[keep_mask]

    return kept, {
        "total": verts.shape[0], "kept": kept.shape[0],
        "ground_removed": n_ground, "radial_cut": n_radial_cut,
        "ceiling_cut": n_ceiling_cut, "safety_cut": n_safety_cut,
    }


# ---------------------------------------------------------------------------
# Phase 2: cross-PLY residual ground suppression
# ---------------------------------------------------------------------------

def _cross_ply_suppress(main_xyz, filtered_others, center, normal, u1, u2):
    """Remove residual ground points from non-main PLYs in cells where the
    main PLY has a person standing.

    Builds a 2D reference map from the main PLY:
      - main_ground[cell]  : per-cell minimum signed_dist (ground height)
      - main_person[cell]  : True when the cell contains points at least
                             _PERSON_ABOVE_GROUND above the local ground

    For each non-main PLY (already filtered by Phase 1), a surviving point
    is removed when ALL of these hold:
      1. It falls in a cell where main_person is True.
      2. Its signed_dist is within _CROSS_GROUND_EPS of main_ground[cell].

    This catches ground points that Phase 1 missed and that sit directly
    under the main PLY's person, where they would cause the most occlusion.
    """
    cs = _GROUND_CELL_SIZE       # reuse same grid resolution as Phase 1
    cross_eps = _CROSS_GROUND_EPS
    person_above = _PERSON_ABOVE_GROUND

    # ---- build main PLY reference map --------------------------------
    main_sd = (main_xyz - center) @ normal
    shifted = main_xyz - center
    coords_2d = np.column_stack([shifted @ u1, shifted @ u2])

    mins = coords_2d.min(axis=0) - cs
    maxs = coords_2d.max(axis=0) + cs
    nx = max(1, int(np.ceil((maxs[0] - mins[0]) / cs)))
    ny = max(1, int(np.ceil((maxs[1] - mins[1]) / cs)))

    ix = np.clip(np.floor((coords_2d[:, 0] - mins[0]) / cs).astype(np.int32), 0, nx - 1)
    iy = np.clip(np.floor((coords_2d[:, 1] - mins[1]) / cs).astype(np.int32), 0, ny - 1)
    cell_flat = ix * ny + iy
    n_cells = nx * ny

    # Per-cell ground height
    main_ground = np.full(n_cells, np.inf, dtype=np.float32)
    np.minimum.at(main_ground, cell_flat, main_sd.astype(np.float32))

    # Per-cell maximum height (for person-above-ground check)
    main_max_sd = np.full(n_cells, -np.inf, dtype=np.float32)
    np.maximum.at(main_max_sd, cell_flat, main_sd.astype(np.float32))

    # Person present when max > ground + threshold AND ground is known
    main_person = (main_max_sd > main_ground + person_above) & (main_ground < np.inf)

    n_person_cells = int(main_person.sum())
    print(f"\n  Phase 2: main PLY reference map  "
          f"grid={nx}x{ny}  person_cells={n_person_cells}  "
          f"cross_eps={cross_eps:.2f}m  person_above={person_above:.2f}m")

    # ---- filter each non-main PLY ------------------------------------
    result = []
    for verts, ply_label in filtered_others:
        xyz = verts[:, :3]
        sd_nm = (xyz - center) @ normal
        shifted_nm = xyz - center
        coords_nm = np.column_stack([shifted_nm @ u1, shifted_nm @ u2])

        # Map to main grid (don't clip — detect out-of-bounds separately)
        ix_raw = np.floor((coords_nm[:, 0] - mins[0]) / cs).astype(np.int32)
        iy_raw = np.floor((coords_nm[:, 1] - mins[1]) / cs).astype(np.int32)
        in_bounds = (ix_raw >= 0) & (ix_raw < nx) & (iy_raw >= 0) & (iy_raw < ny)

        ix_nm = np.clip(ix_raw, 0, nx - 1)
        iy_nm = np.clip(iy_raw, 0, ny - 1)
        cell_nm = ix_nm * ny + iy_nm

        # Residual ground: in a person cell AND at main's ground level
        is_residual = (
            in_bounds &
            main_person[cell_nm] &
            (np.abs(sd_nm - main_ground[cell_nm]) <= cross_eps)
        )

        n_removed = int(is_residual.sum())
        kept = verts[~is_residual]

        print(f"  Phase 2 [{ply_label}]: removed {n_removed} pts  "
              f"({kept.shape[0]} kept)")

        result.append((kept, ply_label))

    return result


# ---------------------------------------------------------------------------
# bias correction: density-peak isolation + rigid 2D offset
# ---------------------------------------------------------------------------
def apply_bias(main_verts, filtered_others, center, normal, u1, u2,
               effective_r, args):
    """Isolate the person core in each PLY via grid-based density-peak
    detection, then detect overlap between main and each non-main core.
    When overlap exists, apply a single rigid 2D offset to ALL points of
    that non-main PLY.  The offset is clamped so no point exits the cylinder."""
    def project_to_plane(xyz):
        shifted = xyz - center
        return np.column_stack([shifted @ u1, shifted @ u2])

    def density_core(pts_2d, core_radius=0.5, grid_cell=0.15):
        """Find the densest grid cell → peak.  Collect all points within
        core_radius of the peak, return (core_centroid, core_radius, peak, core_mask)."""
        mins = np.min(pts_2d, axis=0) - grid_cell
        maxs = np.max(pts_2d, axis=0) + grid_cell
        nx = max(1, int(np.ceil((maxs[0] - mins[0]) / grid_cell)))
        ny = max(1, int(np.ceil((maxs[1] - mins[1]) / grid_cell)))

        ix = np.clip(np.floor((pts_2d[:, 0] - mins[0]) / grid_cell).astype(np.int32), 0, nx - 1)
        iy = np.clip(np.floor((pts_2d[:, 1] - mins[1]) / grid_cell).astype(np.int32), 0, ny - 1)
        flat = ix * ny + iy
        counts = np.bincount(flat, minlength=nx * ny)
        peak_flat = int(np.argmax(counts))

        peak = np.array([mins[0] + (peak_flat // ny + 0.5) * grid_cell,
                         mins[1] + (peak_flat % ny + 0.5) * grid_cell])

        core_mask = np.linalg.norm(pts_2d - peak, axis=1) <= core_radius
        core_pts = pts_2d[core_mask]
        if core_pts.shape[0] < 10:
            core_pts = pts_2d   # fallback: use all points
            core_mask = np.ones(pts_2d.shape[0], dtype=bool)

        c = np.mean(core_pts, axis=0)
        r = float(np.percentile(np.linalg.norm(core_pts - c, axis=1),
                                args.bias_radius_percentile))
        return c, r, peak, core_mask

    pct = args.bias_radius_percentile
    main_2d = project_to_plane(main_verts[:, :3])
    main_c, main_r, main_peak, main_core = density_core(main_2d)

    print(f"\nBias correction  : margin={args.bias_margin}m  radius_percentile={pct:.0f}%")
    print(f"  main PLY        : {main_verts.shape[0]} pts  core={int(main_core.sum())} pts  "
          f"centroid=[{main_c[0]:.3f},{main_c[1]:.3f}]  r={main_r:.3f}m")

    result = []
    for verts, ply_label in filtered_others:
        xyz = verts[:, :3]
        pts_2d = project_to_plane(xyz)
        self_c, self_r, self_peak, self_core = density_core(pts_2d)

        diff = self_c - main_c
        dist = float(np.linalg.norm(diff))
        overlap = (main_r + self_r + args.bias_margin) - dist

        if overlap <= 0:
            print(f"  {ply_label:16s}: {verts.shape[0]} pts  core={int(self_core.sum())} pts  "
                  f"self_r={self_r:.3f}m  dist={dist:.3f}m  -- no overlap")
            result.append((verts, ply_label))
            continue

        # offset direction: away from main centroid
        if dist < 1e-8:
            direction = np.array([1.0, 0.0])
        else:
            direction = diff / dist

        offset_2d = direction * overlap
        offset_mag = float(np.linalg.norm(offset_2d))

        # apply 2D plane offset → 3D world, to ALL points
        offset_3d = u1 * offset_2d[0] + u2 * offset_2d[1]
        corrected = verts.copy()
        corrected[:, 0] += offset_3d[0]
        corrected[:, 1] += offset_3d[1]
        corrected[:, 2] += offset_3d[2]

        # clamp any point that left the cylinder: radially project back
        new_xyz = corrected[:, :3]
        new_sd = (new_xyz - center) @ normal
        new_proj = new_xyz - np.outer(new_sd, normal)
        new_radial = np.linalg.norm(new_proj - center, axis=1)
        oob = new_radial > effective_r
        n_oob = int(oob.sum())
        if n_oob > 0:
            scale = effective_r / new_radial[oob]
            clamped = center + (new_proj[oob] - center) * scale[:, None]
            corrected[oob, 0] = clamped[:, 0] + (new_sd[oob] * normal[0])
            corrected[oob, 1] = clamped[:, 1] + (new_sd[oob] * normal[1])
            corrected[oob, 2] = clamped[:, 2] + (new_sd[oob] * normal[2])

        print(f"  {ply_label:16s}: {verts.shape[0]} pts  core={int(self_core.sum())} pts  "
              f"self_r={self_r:.3f}m  dist={dist:.3f}m  overlap={overlap:.3f}m  "
              f"offset={offset_mag:.3f}m  clamped={n_oob}")

        result.append((corrected, ply_label))

    return result


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
        description="Fuse PLY files using cylinder region from fitted camera circle"
    )
    parser.add_argument("--config", type=str, default=None,
                        help="Path to JSON config file (auto-discovered if omitted)")
    parser.add_argument("--path", required=("path" not in cfg),
                        default=cfg.get("path"),
                        help="Path to project directory (e.g. CameraData/04)")
    parser.add_argument("--max-index", type=int,
                        required=("max_index" not in cfg),
                        default=cfg.get("max_index"),
                        help="Use cameras id=0..max_index (0-based, inclusive) for circle fitting")
    parser.add_argument("--radius-scale", type=float,
                        default=cfg.get("radius_scale", 1.0),
                        help="Scale the fitted circle radius (default: 1.0)")
    parser.add_argument("--height-up", type=float,
                        required=("height_up" not in cfg),
                        default=cfg.get("height_up"),
                        help="Cylinder height above the fitted plane, along the plane normal")
    parser.add_argument("--height-down", type=float,
                        required=("height_down" not in cfg),
                        default=cfg.get("height_down"),
                        help="Ground-side search range (m). Points below -height_down are "
                             "unconditionally removed; within [-height_down, height_up] the "
                             "adaptive grid algorithm isolates and removes the ground surface. "
                             "Typical: 0.3–0.5")
    parser.add_argument("--bias", action="store_true",
                        default=cfg.get("bias", False),
                        help="Enable centroid-based overlap correction for non-main PLYs")
    parser.add_argument("--bias-margin", type=float,
                        default=cfg.get("bias_margin", 0.05),
                        help="Extra separation margin in meters between point masses after offset (default: 0.05)")
    parser.add_argument("--bias-radius-percentile", type=float,
                        default=cfg.get("bias_radius_percentile", 50.0),
                        help="Percentile for core radius estimation (default: 50 = median). Lower = tighter core.")
    parser.add_argument("--output-subfix", type=str,
                        default=cfg.get("output_subfix", ""),
                        help="Appended to output filename: combine_{subfix}.ply (default: empty = no subfix)")
    parser.add_argument("--indices", type=str,
                        default=cfg.get("indices"),
                        help="Space-separated PLY indices to fuse (first = main).  If set, skips interactive prompt.")
    args = parser.parse_args(remaining)

    proj_dir = Path(args.path)
    if not proj_dir.is_absolute():
        proj_dir = Path.cwd() / proj_dir
    proj_dir = proj_dir.resolve()

    # ----- load cameras ----------------------------------------------------
    cameras_path = proj_dir / "cameras.json"
    if not cameras_path.exists():
        print(f"ERROR: {cameras_path} not found")
        sys.exit(1)

    with open(cameras_path, "r", encoding="utf-8") as f:
        cameras = json.load(f)

    # cameras id is 0-based and matches array index; take id 0..max_index
    cam_subset = cameras[:args.max_index + 1]
    if len(cam_subset) < 3:
        print(f"ERROR: need at least 3 cameras for circle fitting, got {len(cam_subset)}")
        sys.exit(1)

    print(f"Loaded {len(cameras)} cameras, using id=0..{args.max_index} ({len(cam_subset)} cameras) for circle fitting")

    positions = np.array([c["position"] for c in cam_subset])

    # ----- fit circle ------------------------------------------------------
    center, normal, r_fit, u1, u2 = fit_circle(positions)
    scale = args.radius_scale
    effective_r = r_fit * scale

    print(f"Circle center    : [{center[0]:.4f}, {center[1]:.4f}, {center[2]:.4f}]")
    print(f"Fit radius       : {r_fit:.4f}")
    print(f"Radius scale     : {scale:.4f}  -> effective r = {effective_r:.4f}")
    print(f"Plane normal     : [{normal[0]:.4f}, {normal[1]:.4f}, {normal[2]:.4f}]")
    print(f"Cylinder height  : [-{args.height_down:.4f}, +{args.height_up:.4f}] along normal")

    # ----- discover PLY files ----------------------------------------------
    plys_dir = proj_dir / "plys"
    if plys_dir.is_dir():
        ply_files = sorted(plys_dir.glob("*.ply"))
    else:
        ply_files = sorted(proj_dir.glob("*.ply"))

    if not ply_files:
        print(f"ERROR: no .ply files found in {proj_dir} or {plys_dir}")
        sys.exit(1)

    # extract suffix identifiers: strip longest common prefix + ".ply"
    ply_names = [p.name for p in ply_files]
    common_prefix = longest_common_prefix(ply_names)
    suffixes = []
    for name in ply_names:
        s = name[len(common_prefix):]
        if s.endswith(".ply"):
            s = s[:-4]
        suffixes.append(s)

    # sort by suffix, assign 1-based display indices
    indexed = sorted(enumerate(suffixes), key=lambda x: x[1])
    sorted_info = [(idx + 1, suf, ply_files[orig_idx])
                   for idx, (orig_idx, suf) in enumerate(indexed)]

    print(f"\nFound {len(ply_files)} PLY files (common prefix: '{common_prefix}'):")
    for idx, suffix, fpath in sorted_info:
        print(f"  index {idx}: {suffix}")

    # ----- user selection --------------------------------------------------
    if args.indices is not None:
        if isinstance(args.indices, str):
            selected = [int(x) for x in args.indices.split()]
        elif isinstance(args.indices, list):
            selected = [int(x) for x in args.indices]
        else:
            selected = [int(args.indices)]
        print(f"\nPLY indices (from config/CLI): {selected}")
    else:
        print("\nEnter space-separated indices of PLYs to fuse (first = main, all points kept):")
        user_input = input("> ").strip()
        selected = [int(x) for x in user_input.split()]

    if not selected:
        print("No indices entered, exiting.")
        sys.exit(0)

    # resolve user indices → (path, label) pairs
    idx_map = {info[0]: (info[2], info[1]) for info in sorted_info}
    selection = []
    for uid in selected:
        if uid not in idx_map:
            print(f"ERROR: index {uid} not in list")
            sys.exit(1)
        selection.append(idx_map[uid])

    main_path, main_label = selection[0]
    other = selection[1:]

    print(f"\nMain PLY  : {main_label}  ({main_path.name})  -- all points kept")
    if other:
        other_labels = ", ".join(lab for _, lab in other)
        print(f"Other PLYs: {other_labels}  -- cylinder-filtered")

    # ----- read & filter ---------------------------------------------------
    # xyz are columns 0,1,2 in the PLY vertex record

    print(f"\nReading main PLY: {main_path.name} ...")
    header_lines, properties, main_verts = read_ply(str(main_path))
    print(f"  {main_verts.shape[0]} points (all kept)")

    if other:
        print(f"  Ground strategy  : adaptive grid (Plan A)")

    filtered_others = []
    for ply_path, ply_label in other:
        print(f"Reading: {ply_path.name} ...")
        _, _, verts = read_ply(str(ply_path))

        kept, stats = _filter_ground_adaptive(
            verts, center, normal, u1, u2,
            effective_r, args.height_up, args.height_down,
        )

        filtered_others.append((kept, ply_label))
        pct = 100.0 * stats["kept"] / stats["total"] if stats["total"] else 0.0
        print(f"  {stats['total']} points -> {stats['kept']} kept ({pct:.1f}%)  "
              f"[ground:{stats['ground_removed']} radial:{stats['radial_cut']} "
              f"ceiling:{stats['ceiling_cut']} safety:{stats['safety_cut']}]")

    # ----- Phase 2: cross-PLY residual ground suppression -------------------
    if filtered_others:
        filtered_others = _cross_ply_suppress(
            main_verts[:, :3], filtered_others,
            center, normal, u1, u2,
        )

    # ----- bias correction (optional) --------------------------------------
    if args.bias and filtered_others:
        filtered_others = apply_bias(main_verts, filtered_others,
                                     center, normal, u1, u2, effective_r, args)

    # ----- write output ----------------------------------------------------
    vertex_blocks = [main_verts] + [v for v, _ in filtered_others]
    # strip trailing digits so "0613-23" -> "0613-"
    clean_prefix = common_prefix.rstrip("0123456789")
    base = f"{clean_prefix}combine" if clean_prefix else "combine"
    subfix = args.output_subfix if args.output_subfix else "-".join(str(i) for i in selected)
    out_name = f"{base}-{subfix}.ply"
    out_path = proj_dir / out_name

    write_ply(str(out_path), header_lines, properties, vertex_blocks)

    total_pts = sum(v.shape[0] for v in vertex_blocks)
    print(f"\nOutput          : {out_path}")
    print(f"Total points    : {total_pts}")
    print(f"Merge summary   :")
    print(f"  main (all kept)   : {main_label}  ({main_path.name})")
    for _, ply_label in other:
        print(f"  filtered          : {ply_label}")


if __name__ == "__main__":
    main()
