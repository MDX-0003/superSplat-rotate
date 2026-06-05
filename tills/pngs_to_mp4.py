"""
Convert image sequences in a project's blended/ folder to MP4 videos.

Usage:
  python tills/pngs_to_mp4.py --project 02
  python tills/pngs_to_mp4.py --project 02 --fps 30 --crf 12

Requires: ffmpeg on PATH or in tills/ directory.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

from paths import project as proj_dir

DEFAULT_FPS = 60
DEFAULT_CRF = 6  # 0=lossless, 12=near-lossless, 18=good, 23=default


def find_ffmpeg():
    candidates = [SCRIPT_DIR / "ffmpeg.exe"]
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidates.append(Path(p) / "ffmpeg.exe")
        candidates.append(Path(p) / "ffmpeg")
    for c in candidates:
        try:
            if c.is_file():
                result = subprocess.run([str(c), "-version"], capture_output=True)
                if result.returncode == 0:
                    return str(c)
        except Exception:
            continue
    return "ffmpeg"


def group_image_sequences(directory: Path):
    pattern = re.compile(r"^(.+?)[._](\d{4,})\.(png|jpg|jpeg)$", re.IGNORECASE)
    sequences = {}
    for f in sorted(directory.iterdir()):
        if not f.is_file():
            continue
        m = pattern.match(f.name)
        if not m:
            continue
        prefix = m.group(1)
        frame = int(m.group(2))
        sequences.setdefault(prefix, []).append((frame, f))
    for prefix in sequences:
        sequences[prefix].sort(key=lambda x: x[0])
    return sequences


def normalize_to_png(ffmpeg_path, frames):
    has_jpg = any(f.suffix.lower() in ('.jpg', '.jpeg') for _, f in frames)
    if not has_jpg:
        return frames, None
    tmpdir = Path(tempfile.mkdtemp(prefix="png_normalized_"))
    new_frames = []
    for num, f in frames:
        out = tmpdir / f"{f.stem}.png"
        if f.suffix.lower() in ('.jpg', '.jpeg'):
            subprocess.run(
                [ffmpeg_path, "-y", "-i", str(f), str(out)],
                capture_output=True, check=True)
        else:
            shutil.copy2(f, out)
        new_frames.append((num, out))
    return new_frames, tmpdir


def find_next_output_path(prefix, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = output_dir / f"{prefix}.mp4"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = output_dir / f"{prefix}_{n}.mp4"
        if not candidate.exists():
            return candidate
        n += 1


def build_ffmpeg_cmd(ffmpeg_path, prefix, frames, fps, crf, output_dir):
    out_path = find_next_output_path(prefix, output_dir)
    first = frames[0][1]
    m = re.search(r"(\d+)\.(png|jpg)$", first.name, re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse frame number from: {first.name}")
    digit_str = m.group(1)
    frame_width = len(digit_str)
    name_no_frame = first.name[: m.start(1)]
    input_pattern = f"{name_no_frame}%0{frame_width}d.{m.group(2)}"
    cmd = [
        ffmpeg_path, "-y",
        "-framerate", str(fps),
        "-start_number", str(frames[0][0]),
        "-i", str(first.parent / input_pattern),
        "-frames:v", str(len(frames)),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", str(crf),
        "-preset", "medium",
        str(out_path),
    ]
    return cmd, out_path


def main():
    parser = argparse.ArgumentParser(
        description="Convert blended frame sequence to MP4"
    )
    parser.add_argument("--project", required=True,
                        help="Project name under CameraData/ (e.g. '02')")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--crf", type=int, default=DEFAULT_CRF)
    parser.add_argument("--input-dir", default=None,
                        help="Override input dir (default: <project>/blended)")
    parser.add_argument("--output-dir", default=None,
                        help="Override output dir (default: <project>/output)")
    args = parser.parse_args()

    proj = proj_dir(args.project)
    input_dir = Path(args.input_dir) if args.input_dir else proj / "blended"
    output_dir = Path(args.output_dir) if args.output_dir else proj / "output"

    ffmpeg = find_ffmpeg()
    print(f"Using ffmpeg: {ffmpeg}")

    if not input_dir.is_dir():
        print(f"ERROR: Input directory not found: {input_dir}")
        sys.exit(1)

    sequences = group_image_sequences(input_dir)
    if not sequences:
        print(f"No image sequences found in {input_dir}")
        sys.exit(1)

    print(f"Found {len(sequences)} sequence(s) in {input_dir}")
    for prefix, frames in sequences.items():
        print(f"  {prefix}: {len(frames)} frames "
              f"({frames[0][1].name} ... {frames[-1][1].name})")

    output_dir.mkdir(parents=True, exist_ok=True)

    for prefix, frames in sequences.items():
        fps = args.fps
        crf = args.crf
        frames_norm, tmpdir = normalize_to_png(ffmpeg, frames)
        if tmpdir:
            print(f"  (normalized {len(frames_norm)} frames to PNG)")

        cmd, out_path = build_ffmpeg_cmd(ffmpeg, prefix, frames_norm, fps, crf, output_dir)
        print(f"\nEncoding: {out_path.name} ({len(frames_norm)} frames @ {fps}fps)...")

        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            if line.startswith("frame="):
                print(f"\r  {line}", end="", flush=True)
            elif "error" in line.lower() or "failed" in line.lower():
                print(f"\n  {line}")
        proc.wait()
        print()

        if proc.returncode != 0:
            print(f"  FAILED (exit code {proc.returncode})")
        else:
            size_mb = out_path.stat().st_size / (1024 * 1024) if out_path.exists() else 0
            print(f"  OK -> {out_path} ({size_mb:.1f} MB)")

        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
