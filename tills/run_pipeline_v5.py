#!/usr/bin/env python3
"""
Pipeline v5 — unified PLY processing + SuperSplat rendering automation.

Combines ply_pipeline (interpolate → fuse → clip) and video pipeline
(real → render → concat) into a single script with one config file.

Usage:
  python tills/run_pipeline_v5.py --config CameraData/08/pipeline.json
  python tills/run_pipeline_v5.py --config CameraData/08/pipeline.json --steps ply
  python tills/run_pipeline_v5.py --config CameraData/08/pipeline.json --steps render
  python tills/run_pipeline_v5.py --config CameraData/08/pipeline.json --steps clip,render
  python tills/run_pipeline_v5.py --config CameraData/08/pipeline.json --force

Config: CameraData/<project>/pipeline.json  (unified, see plan for schema)
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


# ── constants ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
TILLS_PLY_DIR = SCRIPT_DIR.parent / "tills_ply"
ROOT = SCRIPT_DIR.parent

# hardcoded video settings  (from plan: 3840x2160, 25fps, high quality, mp4/h264)
VIDEO_WIDTH = 3840
VIDEO_HEIGHT = 2160
VIDEO_FRAMERATE = 25
VIDEO_FORMAT = "mp4"
VIDEO_CODEC = "h264"
# bitrate: 10 * width * height * frameRate * (0.1 * 1/5) = 10 * 3840 * 2160 * 25 * 0.02
VIDEO_BITRATE = 41_472_000


# ── helpers ────────────────────────────────────────────────────────────────────

def step(name, cmd, shell=False, force_clean=None, cwd=None):
    """Print a step header, optionally clean a previous output, then run a subprocess."""
    print(f"\n{'='*60}")
    print(f"  STEP: {name}")
    if isinstance(cmd, list):
        print(f"  CMD : {' '.join(str(c) for c in cmd)}")
    else:
        print(f"  CMD : {cmd}")
    if cwd:
        print(f"  CWD : {cwd}")
    print(f"{'='*60}")

    if force_clean and os.path.exists(force_clean):
        if os.path.isdir(force_clean):
            shutil.rmtree(force_clean)
        else:
            os.remove(force_clean)
        print(f"  (force) cleaned: {force_clean}")

    kwargs = {}
    if cwd:
        kwargs["cwd"] = cwd
    if shell:
        result = subprocess.run(cmd, shell=True, **kwargs)
    elif isinstance(cmd, list):
        result = subprocess.run([str(c) for c in cmd], **kwargs)
    else:
        result = subprocess.run(cmd, shell=False, **kwargs)

    if result.returncode != 0:
        print(f"\n  FAILED at: {name}")
        sys.exit(1)


def check_dev_server(url="http://127.0.0.1:3000/"):
    """Return True if the SuperSplat dev server is reachable."""
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except Exception:
        return False


def derive_seq_name(segments, seg_idx):
    """Derive the render MP4 filename from the preceding real segment's end frame.

    Example: real end=74 → 'seq_f075'
    """
    # find the previous real segment
    for j in range(seg_idx - 1, -1, -1):
        if segments[j]["type"] == "real":
            return f"seq_f{segments[j]['end'] + 1:03d}"
    # fallback: no preceding real segment
    return "render"


def validate_config(cfg):
    """Validate pipeline.json structure.

    Supports two formats:
      - New:  {project, preset, output, jsons_path?, litegs_path?}
      - Old:  {project, max_index, interpolate, fuse, clip, output}
    """
    errors = []
    if "project" not in cfg:
        errors.append("Missing 'project'")
    if "output" not in cfg or "segments" not in cfg.get("output", {}):
        print("ERROR: Missing 'output.segments' — v5 requires this for concat.")
        print("If this is a v6 project, use: python tills/run_pipeline_v6.py --config ...")
        sys.exit(1)

    # new format requires 'preset'; old format requires max_index+fuse+clip
    has_new = "preset" in cfg
    has_old = "max_index" in cfg or "clip" in cfg
    if not has_new and not has_old:
        errors.append("Missing 'preset' (new format) or 'max_index'/'clip' (old format)")

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        sys.exit(1)


# ── preset loading & clip args ──────────────────────────────────────────────────

def load_preset(name):
    """Load a named preset from tills_ply/presets.json."""
    presets_file = ROOT / "tills_ply" / "presets.json"
    with open(presets_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if name not in data.get("presets", {}):
        print(f"ERROR: preset '{name}' not found in {presets_file}")
        sys.exit(1)
    return data["presets"][name]


def build_clip_args(preset):
    """Build CLI args for tills_ply/clip_ply.py from a preset dict."""
    c = preset["clip"]
    args = [
        sys.executable, str(TILLS_PLY_DIR / "clip_ply.py"),
        "--path", preset["path"],
        "--clip-percent", str(c.get("clip_percent", 10.0)),
    ]
    has_circle = c.get("denoise") or c.get("ring_delete")
    max_index = preset.get("max_index") or c.get("max_index")
    if has_circle and max_index is not None:
        args.extend(["--max-index", str(max_index)])
        args.extend(["--radius-scale", str(c.get("radius_scale", 1.0))])
    if c.get("denoise"):
        args.append("--denoise")
        # voxel-based denoise (new style)
        if "denoise_voxel_size" in c:
            args.extend(["--denoise-voxel-size", str(c["denoise_voxel_size"])])
        # cylinder-based denoise (old style)
        if "height_up" in c:
            args.extend(["--height-up", str(c["height_up"])])
        if "height_down" in c:
            args.extend(["--height-down", str(c["height_down"])])
        args.extend(["--denoise-min-points", str(c.get("denoise_min_points", 30))])
    if c.get("ring_delete"):
        args.append("--ring-delete")
        args.extend(["--ring-outer-delta", str(c.get("ring_outer_delta", 0.5))])
        args.extend(["--ring-inner-delta", str(c.get("ring_inner_delta", 0.3))])
        if "ring_height_up" in c:
            args.extend(["--ring-height-up", str(c["ring_height_up"])])
        if "ring_height_down" in c:
            args.extend(["--ring-height-down", str(c["ring_height_down"])])
    return args


# ── LiteGS training step ────────────────────────────────────────────────────────

def parse_train_vars(date_str):
    """'2026-06-25-162636' → ('0625', '162636')"""
    parts = date_str.split("-")
    if len(parts) != 4:
        print(f"ERROR: cannot parse date_str '{date_str}' (expected YYYY-MM-DD-HHMMSS)")
        sys.exit(1)
    return parts[1] + parts[2], parts[3]


def run_litegs_train(cfg, preset, segments, force):
    """T1-T5: extract train images → copy to LiteGS → batch_run → copy PLY → clip."""
    litegs_path = Path(cfg["litegs_path"])
    if not litegs_path.is_dir():
        print(f"ERROR: litegs_path not found: {litegs_path}")
        sys.exit(1)

    proj_dir = (ROOT / f"CameraData/{cfg['project']}").resolve()

    # source discovery (needed for extract_train_images)
    source_config = cfg["output"].get("source", "raw_images")
    source_dir, prefix, padding = discover_source(proj_dir, source_config)

    # T1: extract train images
    extract_train_images(segments, source_dir, prefix, padding, proj_dir, force)

    # derive date_strs from train_ranges
    date_str = extract_date_from_prefix(prefix)
    sub_dir, frame_id = parse_train_vars(date_str)

    src_train = proj_dir / "Train_imgs" / date_str
    if not src_train.is_dir():
        print(f"  No train images at {src_train} — skipping training")
        return

    # T2: copy Train_imgs → LiteGSWin/data/<sub_dir>/<date_str>
    dst_frame = litegs_path / "data" / sub_dir / date_str
    if force or not dst_frame.exists():
        print(f"\n  T2: 复制训练素材 → {dst_frame}")
        shutil.copytree(src_train, dst_frame, dirs_exist_ok=True)
    else:
        print(f"\n  T2: SKIP — {dst_frame} already exists")

    # T3: run batch_run.py
    ply_src = litegs_path / "results" / sub_dir / f"{sub_dir}-{frame_id}.ply"
    if force or not ply_src.exists():
        step(f"T3  LiteGS batch_run --sub_dir {sub_dir}",
             f'uv run python batch_run.py --sub_dir {sub_dir}',
             shell=True, cwd=str(litegs_path))
    else:
        print(f"\n  T3: SKIP — {ply_src} already exists")

    # T4: copy result PLY → CameraData/<project>/
    ply_dst = proj_dir / f"{sub_dir}-{frame_id}.ply"
    if force or not ply_dst.exists():
        if ply_src.exists():
            shutil.copy2(ply_src, ply_dst)
            print(f"  T4: 复制 PLY → {ply_dst}")
        else:
            print(f"  T4: ERROR — source PLY not found: {ply_src}")
            sys.exit(1)
    else:
        print(f"  T4: SKIP — {ply_dst} already exists")

    # T5: clip
    clip_out = proj_dir.parent / f"{proj_dir.name}-clip"
    if force and clip_out.is_dir():
        shutil.rmtree(clip_out)
    step(f"T5  clip (preset: {cfg['preset']})", build_clip_args(preset))


# ── PLY selection ──────────────────────────────────────────────────────────────

def select_ply(proj_dir):
    """List PLY files in XX-clip/, let user pick by index. Returns Path or None."""
    clip_dir = proj_dir.parent / f"{proj_dir.name}-clip"
    if not clip_dir.is_dir():
        print(f"ERROR: clip directory not found: {clip_dir}")
        sys.exit(1)

    ply_files = sorted(clip_dir.glob("*.ply"))
    if not ply_files:
        print(f"ERROR: no .ply files found in {clip_dir}")
        sys.exit(1)

    return _select_from_list(ply_files, "PLY", lambda f: f"{f.stat().st_size / 1024**2:.1f} MB")


def select_json(cameras_folder):
    """List camera JSON files, let user pick by index. Returns Path or None."""
    cf = Path(cameras_folder)
    if not cf.is_dir():
        print(f"ERROR: cameras folder not found: {cf}")
        sys.exit(1)

    json_files = sorted(cf.glob("*.json"))
    if not json_files:
        print(f"ERROR: no .json files found in {cf}")
        sys.exit(1)

    return _select_from_list(json_files, "JSON", lambda f: f"{f.stat().st_size / 1024:.0f} KB")


def _select_from_list(files, label, size_fn):
    """Show a numbered list and let user pick by index."""
    print(f"\n{'─'*60}")
    print(f"  {label} files:")
    for i, f in enumerate(files):
        print(f"    [{i}]  {f.name}  ({size_fn(f)})")
    print(f"{'─'*60}")

    while True:
        choice = input(f"  输入 {label} 编号 (idx): ").strip()
        try:
            idx = int(choice)
            if 0 <= idx < len(files):
                return files[idx]
            print(f"  无效编号，请输入 0-{len(files) - 1}")
        except ValueError:
            print(f"  请输入数字")


# ── Playwright / SuperSplat automation ─────────────────────────────────────────

async def ensure_browser(page_url="http://127.0.0.1:3000/"):
    """Connect to an existing Chrome (CDP) or launch a new one and self-connect.

    Priority:
      1. Try to connect to Chrome already listening on port 9222.  This happens
         when the user manually started Chrome with --remote-debugging-port=9222,
         OR when a previous v5 run launched it (v5's own Chrome stays alive).
      2. If nothing is listening, launch Chrome directly with the debug port,
         then WAIT for the port to become ready and connect to it.  The same
         Chrome instance can be reused across multiple v5 runs.
    """
    if not check_dev_server(page_url):
        print(f"\nERROR: SuperSplat dev server 未运行")
        print(f"  请在另一个终端执行: npm run serve")
        print(f"  然后确认 {page_url} 可访问后重试")
        sys.exit(1)

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()

    def _cdp_ready(cdp_url):
        """Check if the CDP endpoint is reachable and returns a valid response."""
        try:
            req = urllib.request.Request(f"{cdp_url}/json/version")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                return data.get("webSocketDebuggerUrl", "")
        except Exception:
            return None

    async def _find_or_create_page(browser, page_url):
        """Find an existing SuperSplat tab or create one."""
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "localhost:3000" in (p.url or "") or "127.0.0.1:3000" in (p.url or ""):
                    return p
        # no matching page — use first available or create new
        for ctx in browser.contexts:
            if ctx.pages:
                page = ctx.pages[0]
                await page.goto(page_url, wait_until="domcontentloaded")
                return page
        return await browser.new_page()

    # ── 1. try existing Chrome on port 9222 ─────────────────────────
    cdp_urls = ["http://127.0.0.1:9222", "http://localhost:9222"]
    for cdp_url in cdp_urls:
        ws = _cdp_ready(cdp_url)
        if ws:
            print(f"  检测到 Chrome CDP: {cdp_url}")
            try:
                browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=10000)
                page = await _find_or_create_page(browser, page_url)
                print(f"  已连接到现有 Chrome")
                await page.wait_for_function(
                    "() => window.scene && window.scene.events", timeout=30000)
                print("  SuperSplat 页面就绪")
                return pw, browser, page
            except Exception as e:
                print(f"  CDP 连接失败: {e}")

    # ── 2. launch Chrome ourselves (with debug port) ─────────────────
    print(f"  启动 Chrome (调试端口 9222)...")

    # build the Chrome executable path
    chrome_paths = [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ]
    chrome_exe = None
    for cp in chrome_paths:
        if Path(cp).exists():
            chrome_exe = cp
            break

    launch_kwargs = dict(headless=False, channel="chrome")
    if chrome_exe:
        launch_kwargs["executable_path"] = chrome_exe

    browser = await pw.chromium.launch(
        **launch_kwargs,
        args=["--remote-debugging-port=9222",
              "--js-flags=--max-old-space-size=8192"],
    )
    page = await browser.new_page()
    await page.goto(page_url, wait_until="domcontentloaded")
    await page.wait_for_function(
        "() => window.scene && window.scene.events", timeout=30000)
    print("  SuperSplat 页面就绪 (v5 自启动 Chrome)")
    print("  提示: 此 Chrome 窗口可复用 — 下次运行 v5 会自动连接")
    return pw, browser, page


async def upload_ply(page, ply_path):
    """Upload a PLY file to SuperSplat by dynamically creating a file input.

    Why not base64 via page.evaluate?  For large PLYs (>50 MB) the base64
    string + CDP transfer + JS decoding causes Chrome OOM crashes and is
    orders of magnitude slower than native file input.

    Instead, we inject a hidden ``<input type="file">`` into the page, use
    Playwright's ``set_input_files`` (which tells the browser to read the
    file from disk — zero-copy), and let the ``onchange`` handler call
    SuperSplat's ``events.invoke('import', ...)`` with the native File object.
    """
    abs_path = ply_path.resolve()
    file_size_mb = abs_path.stat().st_size / 1024 ** 2
    filename = abs_path.name
    print(f"  正在上传 PLY: {filename} ({file_size_mb:.0f} MB) ...")

    # 1. inject a hidden file input + onchange handler into the page
    await page.evaluate("""
        () => {
            // remove any previous v5 input
            const old = document.getElementById('__v5_ply_input');
            if (old) old.remove();

            const input = document.createElement('input');
            input.type = 'file';
            input.id = '__v5_ply_input';
            input.style.display = 'none';
            input.accept = '.ply';

            input.onchange = () => {
                const file = input.files[0];
                if (!file) return;
                // set a flag so Python can poll for completion
                window.__v5_importStarted = true;
                window.scene.events.invoke('import', [{
                    filename: file.name,
                    contents: file
                }]).then(() => {
                    window.__v5_importDone = true;
                }).catch(e => {
                    window.__v5_importError = String(e);
                    window.__v5_importDone = true;
                });
            };

            document.body.appendChild(input);
            window.__v5_importStarted = false;
            window.__v5_importDone = false;
            window.__v5_importError = null;
        }
    """)

    # 2. set the file on our input (tells Chrome to read from disk)
    file_input = page.locator("#__v5_ply_input")
    await file_input.set_input_files(str(abs_path))
    print(f"  文件已注入，等待 SuperSplat 加载 ...")

    # 3. wait for import to finish (up to 2 min for very large PLYs)
    for i in range(120):
        await asyncio.sleep(1)
        try:
            done = await page.evaluate("window.__v5_importDone")
            if done:
                error = await page.evaluate("window.__v5_importError")
                if error:
                    print(f"  ERROR: import 失败 — {error}")
                    return
                # verify splats
                count = await page.evaluate(
                    "window.scene.events.invoke('scene.splats').length")
                print(f"  PLY 已加载 ({count} splat(s)) — 耗时约 {i+1}s")
                return
        except Exception:
            pass
        # show progress every 10s
        if i > 0 and i % 10 == 0:
            started = await page.evaluate("window.__v5_importStarted")
            print(f"  等待中 ({i}s) ... {'import 已触发' if started else 'import 尚未触发'}")

    print(f"  WARNING: 120s 后仍未检测到 splat，PLY 可能加载失败")
    print(f"  请手动检查 SuperSplat 页面中的模型是否已显示")


async def upload_json_file(page, json_path):
    """Upload a camera JSON file and auto-import as GT + Timeline.

    SuperSplat source has been modified so that ``cameraImportSessionMode``
    defaults to ``'both'`` — no dialog ever appears.  JSON is imported via
    the same dynamic-file-input pattern used for PLY.
    """
    abs_path = json_path.resolve()
    print(f"  正在导入 JSON: {abs_path.name} ({abs_path.stat().st_size / 1024:.0f} KB) ...")

    # 1. inject a hidden file input for JSON
    await page.evaluate("""
        () => {
            const old = document.getElementById('__v5_json_input');
            if (old) old.remove();

            const input = document.createElement('input');
            input.type = 'file';
            input.id = '__v5_json_input';
            input.style.display = 'none';
            input.accept = '.json';

            input.onchange = () => {
                const file = input.files[0];
                if (!file) return;
                window.__v5_importStarted = true;
                window.scene.events.invoke('import', [{
                    filename: file.name,
                    contents: file
                }]).then(() => {
                    window.__v5_importDone = true;
                }).catch(e => {
                    window.__v5_importError = String(e);
                    window.__v5_importDone = true;
                });
            };

            document.body.appendChild(input);
            window.__v5_importStarted = false;
            window.__v5_importDone = false;
            window.__v5_importError = null;
        }
    """)

    # 2. set the file on our input
    file_input = page.locator("#__v5_json_input")
    await file_input.set_input_files(str(abs_path))
    print(f"  JSON 已注入，等待导入完成 ...")

    # 3. wait for import + verify poses
    for i in range(60):
        await asyncio.sleep(1)
        try:
            done = await page.evaluate("window.__v5_importDone")
            if done:
                error = await page.evaluate("window.__v5_importError")
                if error:
                    print(f"  ERROR: JSON import 失败 — {error}")
                    return 0
                poses = await page.evaluate(
                    "window.scene.events.invoke('camera.importedPoses').length")
                frames = await page.evaluate(
                    "window.scene.events.invoke('timeline.frames')")
                print(f"  JSON 已导入: {poses} 位姿, 时间轴 {frames} 帧 — 耗时约 {i+1}s")
                return frames
        except Exception:
            pass

    print(f"  WARNING: 60s 后仍未检测到导入的位姿")
    return 0


async def verify_timeline(page):
    """Auto-verify the timeline has frames.  Errors out if it doesn't."""
    poses = await page.evaluate(
        "window.scene.events.invoke('camera.importedPoses').length")
    frames = await page.evaluate(
        "window.scene.events.invoke('timeline.frames')")
    if frames == 0:
        print("ERROR: 时间轴为空，无法渲染")
        sys.exit(1)
    print(f"  时间轴就绪: {poses} 位姿, {frames} 帧")
    return frames


async def render_video(page, total_frames, renders_dir, expected_filename, fps):
    """Configure video settings and start render in SuperSplat.

    Strategy (avoids Chrome OOM with 4K video):
      1. Open an OPFS (Origin Private File System) writable file handle.
      2. Pass it as ``fileStream`` to ``render.video`` — this streams encoded
         data directly to the sandboxed filesystem instead of buffering
         everything in a BufferTarget.
      3. After render completes, read the file back from OPFS in 10 MB chunks
         (avoids ``Array.from`` on a multi-GB ArrayBuffer).
      4. Assemble chunks on the Python side and write the final MP4.
    """
    # ── create OPFS writable + inject progress listener ──────────────────
    await page.evaluate("""async () => {
        window.__renderStatus = null;
        window.__renderProgress = -1;
        window.__renderError = null;

        // open OPFS file for streaming output
        const root = await navigator.storage.getDirectory();
        window.__opfsHandle = await root.getFileHandle('v5_render.mp4', { create: true });
        window.__opfsWritable = await window.__opfsHandle.createWritable();
        window.__opfsWritableClosed = false;

        window.scene.events.on('progressUpdate', (opts) => {
            if (opts.progress !== undefined) {
                window.__renderProgress = opts.progress;
            }
        });
    }""")

    settings = {
        "startFrame": 0,
        "endFrame": total_frames - 1,
        "frameRate": fps,
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "bitrate": VIDEO_BITRATE,
        "transparentBg": False,
        "showDebug": False,
        "format": VIDEO_FORMAT,
        "codec": VIDEO_CODEC,
    }

    print(f"\n  视频设置: {VIDEO_WIDTH}x{VIDEO_HEIGHT}, {fps}fps, "
          f"{VIDEO_FORMAT}/{VIDEO_CODEC}, high quality")
    print(f"  帧范围: 0 - {total_frames - 1}  (共 {total_frames} 帧)")
    print(f"  预计输出: {renders_dir / expected_filename}")
    print(f"\n  开始渲染 (OPFS 流式输出)...")

    # ── fire render.video WITH the OPFS writable as fileStream ───────────
    await page.evaluate("""(settings) => {
        window.__renderStatus = 'running';
        window.__renderProgress = 0;
        window.scene.events.invoke('render.video', settings, window.__opfsWritable)
            .then(ok => {
                window.__renderStatus = ok ? 'done' : 'failed';
            })
            .catch(e => {
                window.__renderStatus = 'error';
                window.__renderError = String(e);
            })
            .finally(async () => {
                // close the OPFS writable so data is flushed to disk
                if (!window.__opfsWritableClosed) {
                    try {
                        await window.__opfsWritable.close();
                        window.__opfsWritableClosed = true;
                    } catch (_) {}
                }
            });
    }""", settings)

    # ── poll progress ────────────────────────────────────────────────────
    last_progress = -1
    while True:
        status = await page.evaluate("window.__renderStatus || 'running'")
        if status != "running":
            break

        progress = await page.evaluate("window.__renderProgress")
        if progress is not None and progress != last_progress:
            print(f"\r  渲染进度: {progress:.0f}%", end="", flush=True)
            last_progress = progress

        await asyncio.sleep(3)

    print()  # newline after progress

    # ── check render result ──────────────────────────────────────────────
    status = await page.evaluate("window.__renderStatus")
    if status == "error":
        error_msg = await page.evaluate("window.__renderError || 'unknown'")
        print(f"  ERROR: 渲染失败 — {error_msg}")
        return False
    if status == "failed":
        print(f"  WARNING: render.video 返回 false (可能被取消)")
        return False

    # ensure writable is closed
    await page.evaluate("""async () => {
        if (!window.__opfsWritableClosed) {
            try { await window.__opfsWritable.close(); } catch (_) {}
            window.__opfsWritableClosed = true;
        }
    }""")

    print(f"  渲染完成，从 OPFS 读取文件...")

    # ── read file back from OPFS in chunks ───────────────────────────────
    target_path = renders_dir / expected_filename
    renders_dir.mkdir(parents=True, exist_ok=True)

    try:
        # get total file size first
        file_size = await page.evaluate("""async () => {
            const root = await navigator.storage.getDirectory();
            const handle = await root.getFileHandle('v5_render.mp4');
            const file = await handle.getFile();
            return file.size;
        }""")
        print(f"  OPFS 文件大小: {file_size / 1024**2:.1f} MB")

        # read in 10 MB chunks
        CHUNK_SIZE = 10 * 1024 * 1024
        total_read = 0
        with open(target_path, "wb") as out:
            while total_read < file_size:
                end = min(total_read + CHUNK_SIZE, file_size)
                chunk_b64 = await page.evaluate("""
                    async ([start, end]) => {
                        const root = await navigator.storage.getDirectory();
                        const handle = await root.getFileHandle('v5_render.mp4');
                        const file = await handle.getFile();
                        const blob = file.slice(start, end);
                        // use FileReader for chunked base64 (avoids ArrayBuffer copy)
                        return new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onload = () => {
                                // strip data:...;base64, prefix
                                const b64 = reader.result.split(',')[1];
                                resolve(b64);
                            };
                            reader.onerror = reject;
                            reader.readAsDataURL(blob);
                        });
                    }
                """, [total_read, end])
                import base64
                chunk = base64.b64decode(chunk_b64)
                out.write(chunk)
                total_read = end
                pct = 100 * total_read / file_size
                print(f"\r  读取进度: {pct:.0f}%", end="", flush=True)

        print()  # newline
        print(f"  OPFS 读回完成 → {target_path} ({file_size / 1024**2:.1f} MB)")

        # clean up the OPFS file
        try:
            await page.evaluate("""async () => {
                const root = await navigator.storage.getDirectory();
                await root.removeEntry('v5_render.mp4');
            }""")
        except Exception:
            pass

        return True

    except Exception as e:
        print(f"  ERROR: OPFS 读取失败 — {e}")
        print(f"  请手动保存视频到 {target_path}")
        return False


# ── video pipeline (from v4) ───────────────────────────────────────────────────

def auto_detect_filename_pattern(source_dir):
    """Scan .jpg files in source_dir, extract filename prefix and zero-padding width.

    Returns (prefix: str, padding_width: int).
    """
    jpgs = sorted(source_dir.glob("*.jpg"))
    if not jpgs:
        jpgs = sorted(source_dir.glob("*.jpeg"))
    if not jpgs:
        print(f"ERROR: No .jpg files found in {source_dir}")
        sys.exit(1)

    lowest = min(jpgs, key=lambda f: f.stem)
    stem = lowest.stem
    m = re.match(r'^(.*?)\s*(\d+)$', stem)
    if not m:
        print(f"ERROR: Cannot parse filename pattern from '{lowest.name}'")
        print(f"       Expected '<prefix> <number>.jpg' (e.g. 'IMG 000.jpg')")
        sys.exit(1)

    prefix = m.group(1) + " "
    padding = len(m.group(2))
    return prefix, padding


def discover_source(proj_dir, source_config):
    """Resolve flat-image source directory and filename pattern.

    Returns (source_dir: Path, prefix: str, padding: int).
    """
    root = proj_dir / source_config
    if not root.is_dir():
        print(f"ERROR: source directory not found: {root}")
        sys.exit(1)

    jpgs = list(root.glob("*.jpg")) + list(root.glob("*.jpeg"))
    if jpgs:
        prefix, padding = auto_detect_filename_pattern(root)
        print(f"  Source: {root}")
        print(f"  Pattern: \"{prefix}{{idx:0{padding}d}}.jpg\"")
        return root, prefix, padding

    subdirs = sorted([d for d in root.iterdir() if d.is_dir()])
    if not subdirs:
        print(f"ERROR: No images or subdirectories found in {root}")
        sys.exit(1)

    sub = subdirs[0]
    prefix, padding = auto_detect_filename_pattern(sub)
    print(f"  Source: {sub}")
    print(f"  Pattern: \"{prefix}{{idx:0{padding}d}}.jpg\"")
    return sub, prefix, padding


def extract_date_from_prefix(prefix):
    """Extract a date-time substring from the filename prefix."""
    stripped = prefix.strip()
    m = re.search(r'\d{4}-\d{2}-\d{2}-\d{6}', stripped)
    if m:
        return m.group(0)
    return re.sub(r'^[^\d]+', '', stripped)


def compute_train_ranges(segments):
    """For render segments with replace_frames, derive source image ranges."""
    train_ranges = {}
    for i, seg in enumerate(segments):
        if seg["type"] != "render" or "replace_frames" not in seg:
            continue
        prev_end = None
        for j in range(i - 1, -1, -1):
            if segments[j]["type"] == "real":
                prev_end = segments[j]["end"]
                break
        if prev_end is None:
            continue
        t_start = prev_end
        t_end = t_start + seg["replace_frames"] - 1
        train_ranges[i] = (t_start, t_end)
    return train_ranges


def extract_train_images(segments, source_dir, prefix, padding, proj_dir, force):
    """Copy 3DGS training source images into Train_imgs/<date>/."""
    train_ranges = compute_train_ranges(segments)
    if not train_ranges:
        return

    date_str = extract_date_from_prefix(prefix)
    train_base = proj_dir / "Train_imgs"

    for seg_idx, (t_start, t_end) in train_ranges.items():
        seg = segments[seg_idx]
        train_dir = train_base / date_str
        if force and train_dir.is_dir():
            shutil.rmtree(train_dir)
        train_dir.mkdir(parents=True, exist_ok=True)

        out_idx = 1
        for idx in range(t_start, t_end + 1):
            src = source_dir / f"{prefix}{idx:0{padding}d}.jpg"
            if not src.exists():
                print(f"  WARNING: {src} missing — stopping train extraction")
                break
            dst = train_dir / f"{out_idx:03d}.jpg"
            shutil.copy2(src, dst)
            out_idx += 1

        count = out_idx - 1
        seq_name = derive_seq_name(segments, seg_idx)
        print(f"  render seg[{seg_idx}] '{seq_name}': "
              f"{count} train images → {train_dir}")


def extract_real_frames(segments, source_dir, prefix, padding, anchor_dir):
    """Copy real JPGs to anchor_dir with 0-indexed sequential naming.

    Returns real_ranges: dict[seg_idx → (out_start, out_end)].
    """
    out_idx = 0
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


def real_jpgs_to_mp4(segments, real_ranges, anchor_dir, proj_dir, fps, crf, force):
    """Convert consecutive real JPG groups to H.264 MP4.

    Returns real_mp4_map: dict[seg_idx → canonical_mp4_name].
    """
    real_mp4_map = {}
    i = 0
    while i < len(segments):
        if segments[i]["type"] != "real":
            i += 1
            continue
        j = i
        while j < len(segments) and segments[j]["type"] == "real":
            j += 1

        first_out = real_ranges[i][0]
        last_out = real_ranges[j - 1][1]
        first_src = segments[i]["start"]
        last_src = segments[j - 1]["end"]
        count = last_out - first_out + 1
        name = f"real_{first_src:04d}_{last_src:04d}"
        mp4_path = proj_dir / f"{name}.mp4"

        if force or not mp4_path.exists():
            print(f"\n  Converting real group [{i}..{j-1}]: "
                  f"{count} JPGs → {name}.mp4")
            ffmpeg_cmd = (
                f'ffmpeg -y -framerate {fps} -start_number {first_out} '
                f'-i "{anchor_dir}/%04d.jpg" -frames:v {count} '
                f'-c:v libx264 -crf {crf} -preset slow '
                f'-pix_fmt yuv420p -vf "scale=iw:ih:out_range=tv" '
                f'-r {fps} "{mp4_path}"'
            )
            step(f"Real JPGs → {name}.mp4", ffmpeg_cmd, shell=True)
        else:
            print(f"\n  SKIP: {name}.mp4 exists")

        for g in range(i, j):
            real_mp4_map[g] = name
        i = j

    return real_mp4_map


def concat_to_output(segments, real_mp4_map, render_names, proj_dir,
                     output_dir, fps, crf, debug):
    """Convert all MP4s to TS, order by timeline, concat → output.mp4."""
    ts_map = {}

    # real MP4s → TS
    for name in set(real_mp4_map.values()):
        mp4_path = proj_dir / f"{name}.mp4"
        ts_path = proj_dir / f"{name}.ts"
        ts_cmd = (f'ffmpeg -y -i "{mp4_path}" -c copy '
                  f'-bsf:v h264_mp4toannexb -f mpegts "{ts_path}"')
        step(f"{name} → TS", ts_cmd, shell=True)
        ts_map[name] = ts_path

    # render MP4s → TS
    for seq_name in render_names:
        mp4_path = proj_dir / "renders" / f"{seq_name}.mp4"
        ts_path = proj_dir / f"{seq_name}.ts"
        ts_cmd = (f'ffmpeg -y -i "{mp4_path}" -c copy '
                  f'-bsf:v h264_mp4toannexb -f mpegts "{ts_path}"')
        step(f"{seq_name} → TS", ts_cmd, shell=True)
        ts_map[seq_name] = ts_path

    # build ordered TS list matching timeline
    ordered_ts = []
    for seg_idx, seg in enumerate(segments):
        if seg["type"] == "real":
            ordered_ts.append(ts_map[real_mp4_map[seg_idx]])
        else:
            seq_name = derive_seq_name(segments, seg_idx)
            ordered_ts.append(ts_map[seq_name])

    # write concat demuxer file list (more reliable than concat: protocol —
    # properly adjusts timestamps, avoids DTS-out-of-order warnings)
    concat_list = proj_dir / "_concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for ts_path in ordered_ts:
            # concat demuxer needs forward-slash paths or quoted backslashes
            f.write(f"file '{ts_path.as_posix()}'\n")

    output_mp4 = output_dir / "output.mp4"
    step("TS concat → output.mp4",
         (f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
          f'-c copy -fflags +genpts "{output_mp4}"'),
         shell=True)

    # debug: keep intermediates + export PNG frames
    if debug:
        output_frames_dir = output_dir / "frames"
        output_frames_dir.mkdir(parents=True, exist_ok=True)
        step("Output frames → PNG",
             (f'ffmpeg -y -i "{output_mp4}" '
              f'"{output_frames_dir}/frame_%04d.png"'),
             shell=True)
        print(f"\n  [DEBUG] Intermediate files kept:")
        for ts in proj_dir.glob("*.ts"):
            print(f"    {ts}")
        for mp4 in proj_dir.glob("real_*.mp4"):
            print(f"    {mp4}")
        print(f"  [DEBUG] Output frames: {output_frames_dir}")
        pngs = list(output_frames_dir.glob("*.png"))
        print(f"           ({len(pngs)} PNGs)")
    else:
        for ts in proj_dir.glob("*.ts"):
            ts.unlink()
        for mp4 in proj_dir.glob("real_*.mp4"):
            mp4.unlink()


# ── main ───────────────────────────────────────────────────────────────────────

async def async_main(args, cfg):
    """Run the Playwright-dependent steps (Steps 5-7)."""
    proj_name = cfg["project"]
    proj_dir = (ROOT / f"CameraData/{proj_name}").resolve()

    # derive render names
    segments = cfg["output"]["segments"]
    render_names = []
    for i, seg in enumerate(segments):
        if seg["type"] == "render":
            render_names.append(derive_seq_name(segments, i))

    fps = cfg["output"].get("fps", 25)

    # Step 4a: select PLY
    ply_path = select_ply(proj_dir)

    # Step 4b: select JSON (only when jsons_path is in config)
    json_path = None
    if "jsons_path" in cfg:
        json_path = select_json(cfg["jsons_path"])

    # Step 5: launch browser + upload PLY + upload JSON
    pw, browser, page = await ensure_browser()
    try:
        await upload_ply(page, ply_path)

        if json_path:
            total_frames = await upload_json_file(page, json_path)
            if total_frames == 0:
                print("ERROR: JSON 导入失败")
                sys.exit(1)
        else:
            # fallback: verify timeline already has frames
            total_frames = await verify_timeline(page)

        # Step 7: render video for each render segment
        renders_dir = proj_dir / "renders"
        renders_dir.mkdir(parents=True, exist_ok=True)
        for seq_name in render_names:
            expected_filename = f"{seq_name}.mp4"
            success = await render_video(page, total_frames, renders_dir,
                                         expected_filename, fps)
            if not success:
                print(f"\n  渲染可能未完成，请检查 SuperSplat 页面")
    finally:
        # close Playwright resources cleanly to avoid asyncio transport warnings
        try:
            await page.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass
        print(f"\n  浏览器已关闭")

    return render_names


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline v5: LiteGS training + SuperSplat automation"
    )
    parser.add_argument("--config", required=True,
                        help="Path to pipeline.json (e.g. CameraData/01/pipeline.json)")
    parser.add_argument("--steps", type=str, default=None,
                        help="Comma-separated steps: train,clip,render (default: all)")
    parser.add_argument("--force", action="store_true",
                        help="Re-generate all intermediate outputs")
    parser.add_argument("--debug", action="store_true",
                        help="Keep intermediate MP4/TS files and export output frames as PNGs")
    args = parser.parse_args()

    # ── load config ────────────────────────────────────────────────────────
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    validate_config(cfg)

    proj_name = cfg["project"]
    proj_dir = (ROOT / f"CameraData/{proj_name}").resolve()

    # load preset (new format) or build compatibility dict (old format)
    if "preset" in cfg:
        preset = load_preset(cfg["preset"])
    else:
        # old format — wrap inline params as a mock preset for build_clip_args
        preset = {
            "path": f"CameraData/{proj_name}",
            "max_index": cfg["max_index"],
            "clip": cfg["clip"],
        }

    # determine which steps to run
    valid_steps = {"train", "clip", "render"}
    if args.steps:
        step_filter = set(s.strip() for s in args.steps.split(","))
        unknown = step_filter - valid_steps
        if unknown:
            print(f"ERROR: unknown steps: {unknown}  (valid: {', '.join(sorted(valid_steps))})")
            sys.exit(1)
    else:
        step_filter = valid_steps  # run all

    def should_run(name):
        return name in step_filter

    # ── print summary ──────────────────────────────────────────────────────
    segments = cfg["output"]["segments"]
    render_names = []
    for i, seg in enumerate(segments):
        if seg["type"] == "render":
            render_names.append(derive_seq_name(segments, i))

    real_count = sum(1 for s in segments if s["type"] == "real")
    fps = cfg["output"].get("fps", 25)
    crf = cfg["output"].get("crf", 6)

    print(f"Pipeline v5 — project: {proj_name}")
    print(f"  Steps: {step_filter}")
    if "preset" in cfg:
        print(f"  Preset: {cfg['preset']}")
    print(f"  Timeline: {len(segments)} segments  "
          f"({real_count} real, {len(render_names)} render)")
    print(f"  Render MP4s: {', '.join(render_names)}")
    print(f"  FPS: {fps}  CRF: {crf}  Resolution: {cfg['output']['resolution']}")

    # ── pre-clean intermediates from prior runs ────────────────────────────
    for pattern in ["real_*.mp4", "*.ts"]:
        for f in proj_dir.glob(pattern):
            f.unlink()
    output_frames_dir = proj_dir / "output" / "frames"
    if output_frames_dir.is_dir():
        shutil.rmtree(output_frames_dir)

    # ── Step: train ────────────────────────────────────────────────────────
    if should_run("train"):
        if "litegs_path" not in cfg:
            print("ERROR: --steps train requires 'litegs_path' in pipeline.json")
            sys.exit(1)
        run_litegs_train(cfg, preset, segments, args.force)

    # ── Step: clip ─────────────────────────────────────────────────────────
    if should_run("clip"):
        clip_out = proj_dir.parent / f"{proj_name}-clip"
        clean = str(clip_out) if (args.force and clip_out.exists()) else None
        step("clip → XX-clip/*.ply", build_clip_args(preset), force_clean=clean)

    # ── Step: render (Playwright automation) ───────────────────────────────
    if should_run("render"):
        render_names = asyncio.run(async_main(args, cfg))
    else:
        # still compute render names
        segments = cfg["output"]["segments"]
        render_names = []
        for i, seg in enumerate(segments):
            if seg["type"] == "render":
                render_names.append(derive_seq_name(segments, i))

    # ── concat ─────────────────────────────────────────────────────────────
    if not should_run("render") and not should_run("train"):
        # only doing standalone clip → done
        if step_filter == {"clip"}:
            print(f"\n  Clip complete.")
            return

    # source discovery for concat
    source_config = cfg["output"].get("source", "raw_images")
    source_dir, prefix, padding = discover_source(proj_dir, source_config)

    # extract real frames
    anchor_dir = proj_dir / "anchor_frames"
    if args.force and anchor_dir.is_dir():
        shutil.rmtree(anchor_dir)
    anchor_dir.mkdir(parents=True, exist_ok=True)

    if real_count > 0:
        real_ranges = extract_real_frames(segments, source_dir, prefix,
                                          padding, anchor_dir)
    else:
        print(f"\n  No real segments, skipping extraction")
        real_ranges = {}

    # validate render MP4s exist
    for seq_name in render_names:
        mp4_path = proj_dir / "renders" / f"{seq_name}.mp4"
        if not mp4_path.exists():
            print(f"\n  WARNING: {mp4_path} not found")
            print(f"  Run with --steps render first, or place the file manually")

    # real JPGs → MP4
    if real_count > 0:
        real_mp4_map = real_jpgs_to_mp4(segments, real_ranges, anchor_dir,
                                        proj_dir, fps, crf, args.force)
    else:
        real_mp4_map = {}

    # TS concat → output.mp4
    output_dir = proj_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    concat_to_output(segments, real_mp4_map, render_names, proj_dir,
                     output_dir, fps, crf, args.debug)

    print(f"\n{'='*60}")
    print(f"  DONE.")
    print(f"    output:  {output_dir / 'output.mp4'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
