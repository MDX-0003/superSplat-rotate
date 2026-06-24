#!/usr/bin/env python3
"""
Pipeline v4 — flat sequential images, 0-indexed, real→render→real concat.

All source images are flat sequential JPGs (single camera, no frame subdirs).
Render segments read the full MP4; real segments extract via 0-indexed ranges.
No COLMAP / UE dependency.

Usage:
  python tills/run_pipeline_v4.py --project 09 --timeline tills/timeline/tl_09_01.json

Timeline JSON schema:
{
  "fps": 25,
  "crf": 6,
  "resolution": "3840x2160",
  "source": "raw_images",
  "segments": [
    { "type": "real",   "start": 0,   "end": 74 },
    { "type": "render", "seq": "seq_f075", "replace_frames": 90 },
    { "type": "real",   "start": 165, "end": 238 }
  ]
}
- start/end:  0-indexed flat image indices (match source filenames)
- camera:      tolerated but NOT read by v4
- replace_frames: optional per render segment — documents how many source
  frames the render replaces (for logging only; MP4 is read in full)
- source:      directory under project root for flat images (default "raw_images")

Directory layout:
  CameraData/<project>/
    raw_images/               # flat sequential JPGs (or one subfolder deep)
    renders/                  # SuperSplat MP4 exports (manual step)
      seq_f075.mp4
    anchor_frames/            # extracted real frames (auto)
    output/                   # final output.mp4
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from paths import project as proj_dir

SCRIPT_DIR = Path(__file__).resolve().parent


# ── helpers ────────────────────────────────────────────────────────────────

def step(name, cmd, shell=False):
    print(f"\n{'='*60}")
    print(f"  STEP: {name}")
    if shell:
        print(f"  CMD : {cmd}")
    else:
        print(f"  CMD : {' '.join(str(c) for c in cmd)}")
    print(f"{'='*60}")
    if shell:
        result = subprocess.run(cmd, shell=True)
    else:
        result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0:
        print(f"\n  FAILED at: {name}")
        sys.exit(1)


def wait_for_files(proj, needed):
    """Pause until all required MP4 files exist in renders/."""
    missing = [n for n in needed if not (proj / "renders" / f"{n}.mp4").exists()]
    if not missing:
        return
    print(f"\n{'─'*60}")
    print(f"  MANUAL STEP: SuperSplat render video")
    print(f"  Missing MP4s in renders/:")
    for name in missing:
        print(f"    {name}.mp4  ←  Import camera → Replace Timeline → "
              f"Render → Video → save to renders/{name}.mp4")
    print(f"\n  After placing all MP4s, press ENTER to continue...")
    print(f"{'─'*60}")
    input()


# ── source discovery ───────────────────────────────────────────────────────

def auto_detect_filename_pattern(source_dir):
    """
    Scan the lowest-numbered .jpg in *source_dir*, extract filename prefix
    and zero-padding width via regex.

    Returns (prefix: str, padding_width: int).

    Example: 'DJD-2026-06-22-214307 000.jpg'
        → prefix='DJD-2026-06-22-214307 ', padding=3
    """
    jpgs = sorted(source_dir.glob("*.jpg"))
    if not jpgs:
        jpgs = sorted(source_dir.glob("*.jpeg"))
    if not jpgs:
        print(f"ERROR: No .jpg files found in {source_dir}")
        sys.exit(1)

    # lowest-numbered file (by stem) to get correct zero-padding from small indices
    lowest = min(jpgs, key=lambda f: f.stem)
    stem = lowest.stem
    m = re.match(r'^(.*?)\s*(\d+)$', stem)
    if not m:
        print(f"ERROR: Cannot parse filename pattern from '{lowest.name}'")
        print(f"       Expected '<prefix> <number>.jpg' (e.g. 'IMG 000.jpg')")
        sys.exit(1)

    prefix = m.group(1) + " "  # restore the single space before digits
    padding = len(m.group(2))
    return prefix, padding


def discover_source(proj, source_config):
    """
    Resolve flat-image source directory and filename pattern.

    1. Try *proj / source_config* for .jpg files directly.
    2. If none found, scan for first subdirectory and use that.

    Returns (source_dir: Path, prefix: str, padding: int).
    """
    root = proj / source_config
    if not root.is_dir():
        print(f"ERROR: source directory not found: {root}")
        sys.exit(1)

    # try root first
    jpgs = list(root.glob("*.jpg")) + list(root.glob("*.jpeg"))
    if jpgs:
        prefix, padding = auto_detect_filename_pattern(root)
        print(f"  Source: {root}")
        print(f"  Pattern: \"{prefix}{{idx:0{padding}d}}.jpg\"")
        return root, prefix, padding

    # no images in root — try first subdirectory
    subdirs = sorted([d for d in root.iterdir() if d.is_dir()])
    if not subdirs:
        print(f"ERROR: No images or subdirectories found in {root}")
        sys.exit(1)

    sub = subdirs[0]
    prefix, padding = auto_detect_filename_pattern(sub)
    print(f"  Source: {sub}")
    print(f"  Pattern: \"{prefix}{{idx:0{padding}d}}.jpg\"")
    return sub, prefix, padding


# ── train image extraction ──────────────────────────────────────────────────

def extract_date_from_prefix(prefix):
    """
    Extract a date-time substring from the filename prefix.

    'DJD-2026-06-23-153925 ' → '2026-06-23-153925'

    Strategy: find the first run of digits and dashes that looks like a
    timestamp (\\d{4}-\\d{2}-\\d{2}-\\d{6}), then fall back to stripping
    leading non-digit characters.
    """
    stripped = prefix.strip()
    m = re.search(r'\d{4}-\d{2}-\d{2}-\d{6}', stripped)
    if m:
        return m.group(0)
    # fallback: strip leading non-digit chars
    return re.sub(r'^[^\d]+', '', stripped)


def compute_train_ranges(segments):
    """
    For each render segment that has *replace_frames*, derive the source
    image range that the render replaces.

    train_start = end of the preceding real segment
    train_end   = train_start + replace_frames - 1

    Returns dict[seg_idx → (train_start, train_end)] inclusive, 0-indexed.
    """
    train_ranges = {}
    for i, seg in enumerate(segments):
        if seg["type"] != "render":
            continue
        if "replace_frames" not in seg:
            continue

        # find previous real segment
        prev_end = None
        for j in range(i - 1, -1, -1):
            if segments[j]["type"] == "real":
                prev_end = segments[j]["end"]
                break
        if prev_end is None:
            print(f"  WARNING: render seg[{i}] has no preceding real "
                  f"segment — skipping train extraction")
            continue

        t_start = prev_end
        t_end = t_start + seg["replace_frames"] - 1
        train_ranges[i] = (t_start, t_end)
    return train_ranges


def extract_train_images(segments, source_dir, prefix, padding, proj, force):
    """
    Copy 3DGS training source images (the ones each render segment replaces)
    into ``Train_imgs/<date_prefix>/``, renamed sequentially from ``001.jpg``.

    Reuses the same flat-image source-path pattern as
    :func:`extract_real_frames`.
    """
    train_ranges = compute_train_ranges(segments)
    if not train_ranges:
        print(f"\n  No train ranges to extract (missing replace_frames?)")
        return

    date_str = extract_date_from_prefix(prefix)
    train_base = proj / "Train_imgs"

    for seg_idx, (t_start, t_end) in train_ranges.items():
        seg = segments[seg_idx]
        train_dir = train_base / date_str
        if force and train_dir.is_dir():
            shutil.rmtree(train_dir)
        train_dir.mkdir(parents=True, exist_ok=True)

        out_idx = 1                                  # 1-indexed sequential rename
        for idx in range(t_start, t_end + 1):
            src = source_dir / f"{prefix}{idx:0{padding}d}.jpg"
            if not src.exists():
                print(f"  WARNING: {src} missing — stopping train extraction")
                break
            dst = train_dir / f"{out_idx:03d}.jpg"
            shutil.copy2(src, dst)
            out_idx += 1

        count = out_idx - 1
        print(f"  render seg[{seg_idx}] '{seg['seq']}': "
              f"{count} train images → {train_dir}")


# ── extraction ─────────────────────────────────────────────────────────────

def extract_real_frames(segments, source_dir, prefix, padding, anchor_dir):
    """
    Copy real JPGs from flat source to *anchor_dir* with 0-indexed sequential
    naming.  Stops a segment at the first missing source file (min of
    configured range vs available files).

    Returns real_ranges: dict[seg_idx → (out_start, out_end)] inclusive, 0-indexed.
    """
    out_idx = 0                                    # v4: 0-indexed (v3 was 1)
    real_ranges = {}

    for seg_idx, seg in enumerate(segments):
        if seg["type"] != "real":
            continue

        seg_start_out = out_idx
        for idx in range(seg["start"], seg["end"] + 1):
            src = source_dir / f"{prefix}{idx:0{padding}d}.jpg"
            if not src.exists():
                print(f"  WARNING: {src} missing — stopping segment")
                break
            dst = anchor_dir / f"{out_idx:04d}.jpg"
            shutil.copy2(src, dst)
            out_idx += 1

        count = out_idx - seg_start_out
        real_ranges[seg_idx] = (seg_start_out, out_idx - 1)
        print(f"  seg[{seg_idx}] src {seg['start']}-{seg['end']}: "
              f"{count} → {seg_start_out:04d}-{out_idx - 1:04d}")

    print(f"  Total real frames: {out_idx}")
    return real_ranges


# ── MP4 encoding ───────────────────────────────────────────────────────────

def real_jpgs_to_mp4(segments, real_ranges, anchor_dir, proj, fps, crf, force):
    """
    Convert consecutive real JPG groups to H.264 MP4 via ffmpeg.

    Returns real_mp4_map: dict[seg_idx → canonical_mp4_name].
    """
    real_mp4_map = {}
    i = 0
    while i < len(segments):
        if segments[i]["type"] != "real":
            i += 1
            continue

        # find consecutive real segment run
        j = i
        while j < len(segments) and segments[j]["type"] == "real":
            j += 1

        first_out = real_ranges[i][0]
        last_out = real_ranges[j - 1][1]
        first_src = segments[i]["start"]
        last_src = segments[j - 1]["end"]
        count = last_out - first_out + 1
        name = f"real_{first_src:04d}_{last_src:04d}"
        mp4_path = proj / f"{name}.mp4"

        if force or not mp4_path.exists():
            print(f"\n  Converting real group [{i}..{j-1}]: "
                  f"{count} JPGs → {name}.mp4")
            step(f"4  {name}",
                 (f'ffmpeg -y -framerate {fps} -start_number {first_out} '
                  f'-i "{anchor_dir}/%04d.jpg" -frames:v {count} '
                  f'-c:v libx264 -crf {crf} -preset slow -pix_fmt yuv420p '
                  f'-vsync cfr -r {fps} "{mp4_path}"'),
                 shell=True)
        else:
            print(f"\n  SKIP: {name}.mp4 exists")

        for g in range(i, j):
            real_mp4_map[g] = name
        i = j

    return real_mp4_map


# ── concat ─────────────────────────────────────────────────────────────────

def concat_to_output(segments, real_mp4_map, render_names, proj,
                     output_dir, fps, crf, debug):
    """Convert all MP4s to TS, order by timeline, concat → output.mp4."""
    ts_map = {}

    # real MP4s → TS
    for name in set(real_mp4_map.values()):
        mp4_path = proj / f"{name}.mp4"
        ts_path = proj / f"{name}.ts"
        step(f"5  {name} → TS",
             (f'ffmpeg -y -i "{mp4_path}" -c copy '
              f'-bsf:v h264_mp4toannexb -f mpegts "{ts_path}"'),
             shell=True)
        ts_map[name] = ts_path

    # render MP4s → TS
    for seq_name in render_names:
        mp4_path = proj / "renders" / f"{seq_name}.mp4"
        ts_path = proj / f"{seq_name}.ts"
        step(f"5  {seq_name} → TS",
             (f'ffmpeg -y -i "{mp4_path}" -c copy '
              f'-bsf:v h264_mp4toannexb -f mpegts "{ts_path}"'),
             shell=True)
        ts_map[seq_name] = ts_path

    # build ordered TS list matching timeline
    ordered_ts = []
    for seg_idx, seg in enumerate(segments):
        if seg["type"] == "real":
            ordered_ts.append(ts_map[real_mp4_map[seg_idx]])
        else:
            ordered_ts.append(ts_map[seg["seq"]])

    concat_input = "|".join(str(p) for p in ordered_ts)
    output_mp4 = output_dir / "output.mp4"
    step("5  TS concat → output.mp4",
         (f'ffmpeg -y -i "concat:{concat_input}" -c copy '
          f'-fflags +genpts "{output_mp4}"'),
         shell=True)

    # debug: keep intermediates + export PNG frames
    if debug:
        output_frames_dir = output_dir / "frames"
        output_frames_dir.mkdir(parents=True, exist_ok=True)
        step("D  output frames → PNG",
             (f'ffmpeg -y -i "{output_mp4}" '
              f'"{output_frames_dir}/frame_%04d.png"'),
             shell=True)
        print(f"\n  [DEBUG] Intermediate files kept:")
        for ts in proj.glob("*.ts"):
            print(f"    {ts}")
        for mp4 in proj.glob("real_*.mp4"):
            print(f"    {mp4}")
        print(f"  [DEBUG] Output frames: {output_frames_dir}")
        pngs = list(output_frames_dir.glob("*.png"))
        print(f"           ({len(pngs)} PNGs)")
    else:
        # cleanup intermediate files
        for ts in proj.glob("*.ts"):
            ts.unlink()
        for mp4 in proj.glob("real_*.mp4"):
            mp4.unlink()


# ── main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pipeline v4: flat images → extract → render → concat"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--timeline", required=True, help="Path to timeline JSON")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="Keep intermediate MP4/TS files and export all "
                             "output frames as PNGs")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    python = sys.executable

    # ── pre-clean: remove intermediates from prior runs ─────────────────
    for pattern in ["real_*.mp4", "*.ts"]:
        for f in proj.glob(pattern):
            f.unlink()
    output_frames_dir = proj / "output" / "frames"
    if output_frames_dir.is_dir():
        shutil.rmtree(output_frames_dir)

    # ── load & validate timeline ────────────────────────────────────────
    tl_path = Path(args.timeline)
    if not tl_path.is_absolute():
        tl_path = SCRIPT_DIR.parent / tl_path
    with open(tl_path, "r") as f:
        tl = json.load(f)

    fps = tl.get("fps", 25)
    crf = tl.get("crf", 6)
    resolution = tl.get("resolution", "3840x2160")
    segments = tl["segments"]

    for i, seg in enumerate(segments):
        t = seg["type"]
        if t not in ("real", "render"):
            print(f"ERROR: segment {i}: unknown type '{t}'")
            sys.exit(1)
        if t == "real":
            for k in ("start", "end"):
                if k not in seg:
                    print(f"ERROR: real segment {i}: missing '{k}'")
                    sys.exit(1)
            if seg["start"] > seg["end"]:
                print(f"ERROR: real segment {i}: start > end")
                sys.exit(1)
        if t == "render":
            if "seq" not in seg:
                print(f"ERROR: render segment {i}: missing 'seq'")
                sys.exit(1)
            # optional: validate replace_frames if present
            if "replace_frames" in seg:
                rf = seg["replace_frames"]
                if not isinstance(rf, int) or rf <= 0:
                    print(f"ERROR: render segment {i}: replace_frames must "
                          f"be positive int, got {rf}")
                    sys.exit(1)

    render_names = sorted(set(seg["seq"] for seg in segments
                              if seg["type"] == "render"))
    real_count = sum(1 for seg in segments if seg["type"] == "real")

    print(f"Timeline: {len(segments)} segments")
    print(f"  Real:   {real_count}")
    print(f"  Render: {len(render_names)} ({', '.join(render_names)})")
    print(f"  FPS: {fps}  CRF: {crf}  Resolution: {resolution}")

    # log replace_frames info
    for i, seg in enumerate(segments):
        if seg["type"] == "render" and "replace_frames" in seg:
            print(f"  seg[{i}] render '{seg['seq']}' replaces "
                  f"{seg['replace_frames']} source frames")

    # ── save config ─────────────────────────────────────────────────────
    config_path = proj / "config.json"
    if not config_path.exists() or args.force:
        with open(config_path, "w") as f:
            json.dump(tl, f, indent=2)
        print(f"Config saved → {config_path}")

    # ── Step 0: source discovery ────────────────────────────────────────
    source_config = tl.get("source", "raw_images")
    source_dir, prefix, padding = discover_source(proj, source_config)

    # ── Step 1.5: extract train images ───────────────────────────────────
    extract_train_images(segments, source_dir, prefix, padding,
                         proj, args.force)

    # ── Step 2: extract real frames ─────────────────────────────────────
    anchor_dir = proj / "anchor_frames"
    if args.force and anchor_dir.is_dir():
        shutil.rmtree(anchor_dir)
    anchor_dir.mkdir(parents=True, exist_ok=True)

    if real_count > 0:
        real_ranges = extract_real_frames(segments, source_dir, prefix,
                                          padding, anchor_dir)
    else:
        print(f"\n  No real segments, skipping extraction")
        real_ranges = {}

    # ── Step 3: validate + wait for render MP4s ─────────────────────────
    renders_dir = proj / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    wait_for_files(proj, render_names)

    # ── Step 4: real JPGs → MP4 ─────────────────────────────────────────
    if real_count > 0:
        real_mp4_map = real_jpgs_to_mp4(segments, real_ranges, anchor_dir,
                                        proj, fps, crf, args.force)
    else:
        real_mp4_map = {}

    # ── Step 5: TS concat → output.mp4 ──────────────────────────────────
    output_dir = proj / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    concat_to_output(segments, real_mp4_map, render_names, proj,
                     output_dir, fps, crf, args.debug)

    print(f"\n{'='*60}")
    print(f"  DONE.")
    print(f"    output:  {output_dir / 'output.mp4'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
