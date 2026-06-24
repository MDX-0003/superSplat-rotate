#!/usr/bin/env python3
"""
Fuse selected PLY point clouds using a cylindrical region defined by a circle
fitted to camera positions.

- All points from the "main" PLY (first user selection) are kept in full.
- Points from other selected PLYs are filtered: only those inside the cylinder
  (projected radial distance ≤ scaled fit radius, and signed height along the
  plane normal within [--height-down, --height-up]) are retained.
  camera index consider in circle[0,max_index]

Usage:
  # CLI only
  python tills_ply/fuse_ply.py --path CameraData/04 --max-index 89 --height-up 0.6 --height-down 0.5 --radius-scale 0.5 --bias

  # with config file (auto-discovered or explicit)
  python tills_ply/fuse_ply.py
  python tills_ply/fuse_ply.py --config my_config.json

Config file (JSON) — all keys optional; CLI args take precedence:
  {
    "path": "CameraData/04",
    "max_index": 89,
    "radius_scale": 0.5,
    "height_up": 0.6,
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
  height_up/height_down
                      圆柱沿拟合平面法向量的上下高度(米)。法向量由 SVD 给出,
                      指向场景"上方"未必是世界 Z 轴。调整这两个值可裁剪掉
                      地面上方/下方的杂物。
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
                        help="Cylinder height below the fitted plane, opposite the plane normal")
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

    filtered_others = []
    for ply_path, ply_label in other:
        print(f"Reading: {ply_path.name} ...")
        _, _, verts = read_ply(str(ply_path))
        xyz = verts[:, :3]

        signed_dist = (xyz - center) @ normal
        proj = xyz - np.outer(signed_dist, normal)
        radial = np.linalg.norm(proj - center, axis=1)

        mask = (
            (radial <= effective_r) &
            (signed_dist >= -args.height_down) &
            (signed_dist <= args.height_up)
        )
        kept = verts[mask]
        filtered_others.append((kept, ply_label))
        pct = 100.0 * kept.shape[0] / verts.shape[0] if verts.shape[0] else 0.0
        print(f"  {verts.shape[0]} points -> {kept.shape[0]} kept ({pct:.1f}%)")

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
