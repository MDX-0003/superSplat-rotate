"""
Convert all image sequences (PNG/JPG) in blended_frames/ to MP4 videos.
JPG files are auto-converted to PNG before encoding.
Usage: python pngs_to_mp4.py [fps] [quality]

Requires: ffmpeg (or ffmpeg.exe) on PATH or in same directory.
"""
import os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_DIR = SCRIPT_DIR/"blended_frames"
OUTPUT_DIR = SCRIPT_DIR / "MQR_output"

DEFAULT_FPS = 60
DEFAULT_CRF = 6  # 0=lossless, 12=near-lossless, 18=good, 23=default


def find_ffmpeg():
    """Try to find ffmpeg executable."""
    candidates = []
    # Same dir as script
    candidates.append(SCRIPT_DIR / "ffmpeg.exe")
    # Check PATH
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
    return "ffmpeg"  # fallback, let it fail with clear error


def group_image_sequences(directory: Path):
    """
    Scan directory for PNG/JPG matching '<name>.<frame>.<ext>' or '<name>_<frame>.<ext>'.
    Returns dict: {prefix: [(frame_number, full_path), ...]} sorted by frame.
    """
    sequences = {}
    pattern = re.compile(r"^(.+?)[._](\d{4,})\.(png|jpg|jpeg)$", re.IGNORECASE)
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
    """
    If any frames are JPG, convert all to PNG in a temp directory.
    Returns (frames_with_png_paths, temp_dir_or_None).
    """
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
    """Find next available filename: prefix.mp4, prefix_2.mp4, prefix_3.mp4, ..."""
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
    """Build ffmpeg command for a PNG sequence."""
    out_path = find_next_output_path(prefix, output_dir)

    first = frames[0][1]
    m = re.search(r"(\d+)\.png$", first.name, re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse frame number from: {first.name}")
    digit_str = m.group(1)
    frame_width = len(digit_str)
    name_no_frame = first.name[: m.start(1)]
    input_pattern = f"{name_no_frame}%0{frame_width}d.png"

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
    fps = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FPS
    crf = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_CRF

    ffmpeg = find_ffmpeg()
    print(f"Using ffmpeg: {ffmpeg}")

    if not INPUT_DIR.is_dir():
        print(f"ERROR: Input directory not found: {INPUT_DIR}")
        sys.exit(1)

    sequences = group_image_sequences(INPUT_DIR)
    if not sequences:
        print(f"No image sequences found in {INPUT_DIR}")
        sys.exit(1)

    print(f"Found {len(sequences)} sequence(s) in {INPUT_DIR}")
    for prefix, frames in sequences.items():
        print(f"  {prefix}: {len(frames)} frames "
              f"({frames[0][1].name} ... {frames[-1][1].name})")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for prefix, frames in sequences.items():
        frames, tmpdir = normalize_to_png(ffmpeg, frames)
        if tmpdir:
            print(f"  (normalized {len(frames)} frames to PNG)")

        cmd, out_path = build_ffmpeg_cmd(ffmpeg, prefix, frames, fps, crf, OUTPUT_DIR)
        print(f"\nEncoding: {out_path.name} ({len(frames)} frames @ {fps}fps)...")

        # Run ffmpeg with real-time progress on stderr
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        last_line = ""
        for line in proc.stderr:
            # ffmpeg progress lines end with \r (carriage return)
            line = line.strip()
            if not line:
                continue
            if line.startswith("frame="):
                # Print progress on same line
                print(f"\r  {line}", end="", flush=True)
                last_line = line
            elif "error" in line.lower() or "failed" in line.lower():
                print(f"\n  {line}")
        proc.wait()
        print()  # newline after progress

        if proc.returncode != 0:
            print(f"  FAILED (exit code {proc.returncode})")
        else:
            size_mb = out_path.stat().st_size / (1024 * 1024) if out_path.exists() else 0
            print(f"  OK -> {out_path} ({size_mb:.1f} MB)")

        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
