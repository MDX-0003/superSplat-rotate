#!/usr/bin/env python3
"""
PLY processing pipeline: interpolate → fuse → clip, with named preset management.

Usage:
  # Preset management
  python tills_ply/ply_pipeline.py --list
  python tills_ply/ply_pipeline.py --show <name>
  python tills_ply/ply_pipeline.py --save <name>
  python tills_ply/ply_pipeline.py --del <name>

  # Run pipeline07-0622测试
  python tills_ply/ply_pipeline.py --preset <name>
  python tills_ply/ply_pipeline.py --preset <name> --step fuse
  python tills_ply/ply_pipeline.py --preset <name> --step clip
  python tills_ply/ply_pipeline.py --preset <name> --step interpolate

  # Interactive: pick preset from a numbered list
  python tills_ply/ply_pipeline.py
  
# 1. 调整参数 (跟以前一样，编辑三个 config)
   编辑 tills_ply/fuse_config.json
   编辑 tills_ply/clip_config.json
   编辑 tills_ply/interpolate_config.json

# 2. 保存为预设快照
   python tills_ply/ply_pipeline.py --save 06-激进版

# 3. 一键执行 (interpolate → fuse → clip)
   python tills_ply/ply_pipeline.py --preset 06-激进版

# 4. 下次切换项目，直接选预设名
   python tills_ply/ply_pipeline.py --preset 05-default

Preset file: tills_ply/presets.json (override with --presets-file)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PRESETS_FILE = SCRIPT_DIR / "presets.json"


# ===========================================================================
#  preset storage helpers
# ===========================================================================

def load_presets(filepath):
    """Load presets.json.  Returns {"presets": {...}} dict (creates if missing)."""
    if filepath.exists():
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"presets": {}}


def save_presets(filepath, data):
    """Write presets dict back to JSON file."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    print(f"Presets saved -> {filepath}")


# ===========================================================================
#  preset management commands
# ===========================================================================

def cmd_list(presets_data):
    presets = presets_data.get("presets", {})
    if not presets:
        print("No presets found.  Use --save <name> to create one.")
        return
    print(f"\n{'Preset':<20} {'Path':<24} Description")
    print("-" * 70)
    for name, p in presets.items():
        desc = p.get("description", "-")
        path = p.get("path", "-")
        print(f"{name:<20} {path:<24} {desc}")
    print()


def cmd_show(presets_data, name):
    presets = presets_data.get("presets", {})
    if name not in presets:
        print(f"ERROR: preset '{name}' not found.  Use --list to see available presets.")
        sys.exit(1)
    p = presets[name]
    print(f"\nPreset: {name}")
    print(f"  Path        : {p.get('path', '-')}")
    print(f"  max_index   : {p.get('max_index', '-')}")
    print(f"  Description : {p.get('description', '-')}")
    print(f"  -- interpolate --")
    for k, v in p.get("interpolate", {}).items():
        print(f"    {k:24s}: {v}")
    print(f"  -- fuse --")
    for k, v in p.get("fuse", {}).items():
        print(f"    {k:24s}: {v}")
    print(f"  -- clip --")
    for k, v in p.get("clip", {}).items():
        print(f"    {k:24s}: {v}")
    print()


def cmd_save(presets_data, presets_file, name):
    """Read current fuse_config.json + clip_config.json + interpolate_config.json,
    save as a named preset.  Shared ``max_index`` is extracted to top level."""
    fuse_cfg = _read_config("fuse_config.json")
    clip_cfg = _read_config("clip_config.json")
    interp_cfg = _read_config("interpolate_config.json")

    fuse_path = fuse_cfg.get("path", "")
    clip_path = clip_cfg.get("path", "")

    if fuse_path and clip_path and fuse_path != clip_path:
        print(f"WARNING: fuse path '{fuse_path}' ≠ clip path '{clip_path}' — using fuse path")

    preset_path = fuse_path or clip_path
    if not preset_path:
        print("ERROR: no 'path' field found in fuse_config.json or clip_config.json")
        sys.exit(1)

    # ---- shared max_index (prefer fuse, fall back to clip) ----
    max_index = fuse_cfg.get("max_index") or clip_cfg.get("max_index")

    # build clean fuse section (only keys fuse_ply.py actually uses)
    fuse_section = {
        "radius_scale": fuse_cfg.get("radius_scale", 1.0),
        "height_up": fuse_cfg.get("height_up"),
        "height_down": fuse_cfg.get("height_down"),
        "bias": fuse_cfg.get("bias", False),
        "bias_margin": fuse_cfg.get("bias_margin", 0.05),
        "bias_radius_percentile": fuse_cfg.get("bias_radius_percentile", 50.0),
        "indices": fuse_cfg.get("indices"),
        "output_subfix": fuse_cfg.get("output_subfix", ""),
    }

    clip_section = {
        "clip_percent": clip_cfg.get("clip_percent", 10.0),
        "denoise": clip_cfg.get("denoise", False),
        "radius_scale": clip_cfg.get("radius_scale", 1.0),
        "height_up": clip_cfg.get("height_up"),
        "height_down": clip_cfg.get("height_down"),
        "denoise_min_points": clip_cfg.get("denoise_min_points", 30),
        "ring_delete": clip_cfg.get("ring_delete", False),
        "ring_outer_delta": clip_cfg.get("ring_outer_delta", 0.5),
        "ring_inner_delta": clip_cfg.get("ring_inner_delta", 0.3),
        "ring_height_up": clip_cfg.get("ring_height_up"),
        "ring_height_down": clip_cfg.get("ring_height_down"),
    }

    interp_section = {
        "total": interp_cfg.get("total", 300),
        "anchor_camera": interp_cfg.get("anchor_camera", "006"),
        "radius_scale": interp_cfg.get("radius_scale", 1.0),
    }

    presets_data["presets"][name] = {
        "path": preset_path,
        "max_index": max_index,
        "description": "",
        "interpolate": interp_section,
        "fuse": fuse_section,
        "clip": clip_section,
    }

    save_presets(presets_file, presets_data)
    print(f"Preset '{name}' saved (path={preset_path})")


def cmd_delete(presets_data, presets_file, name):
    presets = presets_data.get("presets", {})
    if name not in presets:
        print(f"ERROR: preset '{name}' not found.")
        sys.exit(1)
    del presets[name]
    save_presets(presets_file, presets_data)
    print(f"Preset '{name}' deleted.")


def _read_config(filename):
    """Read a JSON config from script dir, return empty dict if not found."""
    p = SCRIPT_DIR / filename
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    print(f"NOTE: {filename} not found in {SCRIPT_DIR}, using defaults")
    return {}


# ===========================================================================
#  pipeline execution
# ===========================================================================

def step(name, cmd, force_clean=None):
    """Print header, optionally clean output, then run a subprocess step."""
    print(f"\n{'='*60}")
    print(f"  STEP: {name}")
    print(f"  CMD : {' '.join(cmd)}")
    print(f"{'='*60}")

    if force_clean and os.path.exists(force_clean):
        if os.path.isdir(force_clean):
            shutil.rmtree(force_clean)
        else:
            os.remove(force_clean)
        print(f"  (force) cleaned: {force_clean}")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n  FAILED at: {name}")
        sys.exit(1)


def build_fuse_args(preset):
    """Convert a preset's 'fuse' section to CLI args for fuse_ply.py."""
    f = preset["fuse"]
    max_index = preset.get("max_index") or f.get("max_index")  # top-level preferred
    args = [
        sys.executable, str(SCRIPT_DIR / "fuse_ply.py"),
        "--path", preset["path"],
        "--max-index", str(max_index),
        "--radius-scale", str(f.get("radius_scale", 1.0)),
        "--height-up", str(f["height_up"]),
        "--height-down", str(f["height_down"]),
    ]
    if f.get("bias"):
        args.append("--bias")
        args.extend(["--bias-margin", str(f.get("bias_margin", 0.05))])
        args.extend(["--bias-radius-percentile", str(f.get("bias_radius_percentile", 50))])
    if f.get("output_subfix"):
        args.extend(["--output-subfix", str(f["output_subfix"])])
    if f.get("indices"):
        args.extend(["--indices", " ".join(str(i) for i in f["indices"])])
    return args


def build_clip_args(preset):
    """Convert a preset's 'clip' section to CLI args for clip_ply.py."""
    c = preset["clip"]
    max_index = preset.get("max_index") or c.get("max_index")  # top-level preferred
    args = [
        sys.executable, str(SCRIPT_DIR / "clip_ply.py"),
        "--path", preset["path"],
        "--clip-percent", str(c.get("clip_percent", 10.0)),
    ]
    if c.get("denoise"):
        args.append("--denoise")
        args.extend(["--denoise-voxel-size", str(c.get("denoise_voxel_size", 0.30))])
        args.extend(["--denoise-min-points", str(c.get("denoise_min_points", 50))])
    if c.get("ring_delete"):
        args.append("--ring-delete")
        args.extend(["--max-index", str(max_index)])
        args.extend(["--radius-scale", str(c.get("radius_scale", 1.0))])
        args.extend(["--ring-outer-delta", str(c.get("ring_outer_delta", 0.5))])
        args.extend(["--ring-inner-delta", str(c.get("ring_inner_delta", 0.3))])
        args.extend(["--ring-height-up", str(c["ring_height_up"])])
        args.extend(["--ring-height-down", str(c["ring_height_down"])])
    return args


def build_interpolate_args(preset):
    """Convert a preset's 'interpolate' section to CLI args for
    interpolate_cameras_circle.py (local to tills_ply/)."""
    ip = preset.get("interpolate", {})
    max_index = preset.get("max_index") or ip.get("max_index")
    args = [
        sys.executable, str(SCRIPT_DIR / "interpolate_cameras_circle.py"),
        "--path", preset["path"],
        "--max-index", str(max_index),
        "--total", str(ip.get("total", 300)),
        "--anchor-camera", str(ip.get("anchor_camera", "006")),
        "--radius-scale", str(ip.get("radius_scale", 1.0)),
    ]
    return args


def run_pipeline(preset, run_step=None):
    """Execute interpolate → fuse → clip (or a single step).
    Always force-cleans previous outputs (deletes before regenerate)."""
    proj = Path(preset["path"])
    if not proj.is_absolute():
        proj = Path.cwd() / proj
    proj = proj.resolve()

    clip_out = proj.parent / f"{proj.name}-clip"

    # ---- interpolate ----
    if run_step in (None, "interpolate"):
        output_json = proj / "cameras_align.json"
        clean = str(output_json) if output_json.exists() else None
        step("interpolate", build_interpolate_args(preset), force_clean=clean)

    # ---- fuse ----
    if run_step in (None, "fuse"):
        clean = None
        if proj.is_dir():
            combines = list(proj.glob("*combine*.ply"))
            if combines:
                clean = str(combines[0])  # only cleans the first match
                for c in combines[1:]:
                    os.remove(c)
        step("fuse", build_fuse_args(preset), force_clean=clean)

    # ---- clip ----
    if run_step in (None, "clip"):
        clean = str(clip_out) if clip_out.exists() else None
        step("clip", build_clip_args(preset), force_clean=clean)

    print(f"\nDone.  Output: {clip_out}")


def interactive_select(presets_data):
    """Show numbered preset list, return chosen preset name or None."""
    presets = presets_data.get("presets", {})
    if not presets:
        print("No presets found.  Use --save <name> to create one first.")
        return None

    names = list(presets.keys())
    print("\nAvailable presets:")
    for i, name in enumerate(names, 1):
        p = presets[name]
        desc = p.get("description", "")
        desc_str = f" — {desc}" if desc else ""
        print(f"  [{i}] {name}  ->  {p.get('path', '-')}{desc_str}")

    choice = input("\nEnter number or name (q to quit): ").strip()
    if choice.lower() == "q":
        return None

    # try as number first
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(names):
            return names[idx]
    except ValueError:
        pass

    # try as name
    if choice in presets:
        return choice

    print(f"'{choice}' not recognized.")
    return None


# ===========================================================================
#  main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="PLY pipeline: interpolate → fuse → clip with preset management"
    )
    # preset management
    parser.add_argument("--list", action="store_true", help="List all presets")
    parser.add_argument("--show", type=str, metavar="NAME", help="Show preset details")
    parser.add_argument("--save", type=str, metavar="NAME", help="Save current fuse/clip/interpolate configs as a named preset")
    parser.add_argument("--del", type=str, metavar="NAME", dest="delete_name", help="Delete a preset")
    # pipeline execution
    parser.add_argument("--preset", type=str, metavar="NAME", help="Preset name to run")
    parser.add_argument("--step", type=str, choices=["interpolate", "fuse", "clip"],
                        help="Run only one step (default: all three)")
    parser.add_argument("--force", action="store_true", help="(deprecated — force is always on)")
    # config
    parser.add_argument("--presets-file", type=str, default=None,
                        help=f"Path to presets JSON (default: {DEFAULT_PRESETS_FILE})")

    args = parser.parse_args()

    presets_file = Path(args.presets_file) if args.presets_file else DEFAULT_PRESETS_FILE
    presets_data = load_presets(presets_file)

    # ---- management commands (exit after) ----
    if args.list:
        cmd_list(presets_data)
        return
    if args.show:
        cmd_show(presets_data, args.show)
        return
    if args.save:
        cmd_save(presets_data, presets_file, args.save)
        return
    if args.delete_name:
        cmd_delete(presets_data, presets_file, args.delete_name)
        return

    # ---- pipeline execution ----
    if args.preset:
        presets = presets_data.get("presets", {})
        if args.preset not in presets:
            print(f"ERROR: preset '{args.preset}' not found.  Use --list to see available presets.")
            sys.exit(1)
        preset = presets[args.preset]
        run_pipeline(preset, run_step=args.step)
        return

    # ---- interactive mode ----
    name = interactive_select(presets_data)
    if name:
        preset = presets_data["presets"][name]
        run_pipeline(preset)


if __name__ == "__main__":
    main()
