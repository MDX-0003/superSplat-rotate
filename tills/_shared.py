#!/usr/bin/env python3
"""Shared helpers for v5 / v6 pipeline scripts.

All Playwright automation, file selection, preset loading, and utility
functions live here so both pipeline variants can import them.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# ── constants ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
TILLS_PLY_DIR = SCRIPT_DIR.parent / "tills_ply"
ROOT = SCRIPT_DIR.parent

VIDEO_WIDTH = 3840
VIDEO_HEIGHT = 2160
VIDEO_FRAMERATE = 25
VIDEO_FORMAT = "mp4"
VIDEO_CODEC = "h264"
VIDEO_BITRATE = 41_472_000   # high quality, 4K: 10 * 3840 * 2160 * 25 * 0.02


# ── generic helpers ────────────────────────────────────────────────────────────

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


def unique_path(filepath):
    """Return *filepath* if it doesn't exist, else 'stem-1.ext', 'stem-2.ext', ..."""
    if not filepath.exists():
        return filepath
    stem = filepath.stem
    suffix = filepath.suffix
    parent = filepath.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def check_dev_server(url="http://127.0.0.1:3000/"):
    """Return True if the SuperSplat dev server is reachable."""
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except Exception:
        return False


# ── preset helpers ─────────────────────────────────────────────────────────────

def load_preset(name):
    """Load a named preset from tills_ply/presets.json."""
    presets_file = ROOT / "tills_ply" / "presets.json"
    with open(presets_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if name not in data.get("presets", {}):
        print(f"ERROR: preset '{name}' not found in {presets_file}")
        sys.exit(1)
    return data["presets"][name]


def build_clip_args(preset, path_override=None):
    """Build CLI args for tills_ply/clip_ply.py from a preset dict.

    If *path_override* is given, it replaces ``preset['path']`` (used by v6 to
    redirect output to a different project without modifying presets.json).
    """
    c = preset["clip"]
    proj_path = path_override if path_override else preset["path"]
    args = [
        sys.executable, str(TILLS_PLY_DIR / "clip_ply.py"),
        "--path", proj_path,
        "--clip-percent", str(c.get("clip_percent", 10.0)),
    ]
    has_circle = c.get("denoise") or c.get("ring_delete")
    max_index = preset.get("max_index") or c.get("max_index")
    if has_circle and max_index is not None:
        args.extend(["--max-index", str(max_index)])
        args.extend(["--radius-scale", str(c.get("radius_scale", 1.0))])
    if c.get("denoise"):
        args.append("--denoise")
        if "denoise_voxel_size" in c:
            args.extend(["--denoise-voxel-size", str(c["denoise_voxel_size"])])
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


def parse_train_vars(date_str):
    """'2026-06-25-162636' → ('0625', '162636')"""
    parts = date_str.split("-")
    if len(parts) != 4:
        print(f"ERROR: cannot parse date_str '{date_str}' (expected YYYY-MM-DD-HHMMSS)")
        sys.exit(1)
    return parts[1] + parts[2], parts[3]


def parse_frame_dirname(dirname):
    """Parse a frame directory name to (sub_dir, frame_id).

    Handles both naming conventions:
      YYYY-MM-DD-HHmmss            → sub_dir=MMDD, frame_id=HHmmss
      prefix-YYYY-MM-DD-HHmmss    → same, prefix ignored

    Uses the same heuristic as LiteGSWin's auto_detect_frame_id.
    """
    parts = dirname.split("-")
    # find which part looks like a 4-digit year
    year_idx = None
    for i, p in enumerate(parts):
        if len(p) == 4 and p.isdigit() and (p.startswith("20") or p.startswith("19")):
            year_idx = i
            break
    if year_idx is None or year_idx + 3 >= len(parts):
        raise ValueError(f"Cannot parse frame dirname: {dirname}")

    mm = parts[year_idx + 1]
    dd = parts[year_idx + 2]
    hhmmss = parts[year_idx + 3]
    if len(hhmmss) != 6 or not hhmmss.isdigit():
        raise ValueError(f"Cannot parse frame_id from: {dirname}")

    return mm + dd, hhmmss


# ── file selection ─────────────────────────────────────────────────────────────

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


def select_ply(proj_dir):
    """List PLY files in XX-clip/, let user pick by index."""
    clip_dir = proj_dir.parent / f"{proj_dir.name}-clip"
    if not clip_dir.is_dir():
        print(f"ERROR: clip directory not found: {clip_dir}")
        sys.exit(1)
    ply_files = sorted(clip_dir.glob("*.ply"))
    if not ply_files:
        print(f"ERROR: no .ply files found in {clip_dir}")
        sys.exit(1)
    return _select_from_list(ply_files, "PLY",
                             lambda f: f"{f.stat().st_size / 1024**2:.1f} MB")


def select_json(cameras_folder):
    """List JSON files in cameras_folder, let user pick by index."""
    cf = Path(cameras_folder)
    if not cf.is_dir():
        print(f"ERROR: cameras folder not found: {cf}")
        sys.exit(1)
    json_files = sorted(cf.glob("*.json"))
    if not json_files:
        print(f"ERROR: no .json files found in {cf}")
        sys.exit(1)
    return _select_from_list(json_files, "JSON",
                             lambda f: f"{f.stat().st_size / 1024:.0f} KB")


# ── Playwright / SuperSplat automation ─────────────────────────────────────────

async def ensure_browser(page_url="http://127.0.0.1:3000/"):
    """Connect to an existing Chrome (CDP) or launch a new one and self-connect."""
    if not check_dev_server(page_url):
        print(f"\nERROR: SuperSplat dev server 未运行")
        print(f"  请在另一个终端执行: npm run serve")
        print(f"  然后确认 {page_url} 可访问后重试")
        sys.exit(1)

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()

    def _cdp_ready(cdp_url):
        try:
            req = urllib.request.Request(f"{cdp_url}/json/version")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                return data.get("webSocketDebuggerUrl", "")
        except Exception:
            return None

    async def _find_or_create_page(browser, url):
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "localhost:3000" in (p.url or "") or "127.0.0.1:3000" in (p.url or ""):
                    return p
        for ctx in browser.contexts:
            if ctx.pages:
                page = ctx.pages[0]
                await page.goto(url, wait_until="domcontentloaded")
                return page
        return await browser.new_page()

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

    print(f"  启动 Chrome (调试端口 9222)...")
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
    """Upload a PLY file to SuperSplat via a dynamically created file input."""
    abs_path = ply_path.resolve()
    file_size_mb = abs_path.stat().st_size / 1024 ** 2
    filename = abs_path.name
    print(f"  正在上传 PLY: {filename} ({file_size_mb:.0f} MB) ...")

    await page.evaluate("""
        () => {
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
                window.__v5_importStarted = true;
                window.scene.events.invoke('import', [{
                    filename: file.name, contents: file
                }]).then(() => { window.__v5_importDone = true; })
                  .catch(e => { window.__v5_importError = String(e);
                                window.__v5_importDone = true; });
            };
            document.body.appendChild(input);
            window.__v5_importStarted = false;
            window.__v5_importDone = false;
            window.__v5_importError = null;
        }
    """)

    file_input = page.locator("#__v5_ply_input")
    await file_input.set_input_files(str(abs_path))
    print(f"  文件已注入，等待 SuperSplat 加载 ...")

    for i in range(120):
        await asyncio.sleep(1)
        try:
            done = await page.evaluate("window.__v5_importDone")
            if done:
                error = await page.evaluate("window.__v5_importError")
                if error:
                    print(f"  ERROR: import 失败 — {error}")
                    return
                count = await page.evaluate(
                    "window.scene.events.invoke('scene.splats').length")
                print(f"  PLY 已加载 ({count} splat(s)) — 耗时约 {i+1}s")
                return
        except Exception:
            pass
        if i > 0 and i % 10 == 0:
            started = await page.evaluate("window.__v5_importStarted")
            print(f"  等待中 ({i}s) ... {'import 已触发' if started else 'import 尚未触发'}")

    print(f"  WARNING: 120s 后仍未检测到 splat，PLY 可能加载失败")


async def upload_json_file(page, json_path):
    """Upload a camera JSON file and auto-import as GT + Timeline."""
    abs_path = json_path.resolve()
    print(f"  正在导入 JSON: {abs_path.name} "
          f"({abs_path.stat().st_size / 1024:.0f} KB) ...")

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
                    filename: file.name, contents: file
                }]).then(() => { window.__v5_importDone = true; })
                  .catch(e => { window.__v5_importError = String(e);
                                window.__v5_importDone = true; });
            };
            document.body.appendChild(input);
            window.__v5_importStarted = false;
            window.__v5_importDone = false;
            window.__v5_importError = null;
        }
    """)

    file_input = page.locator("#__v5_json_input")
    await file_input.set_input_files(str(abs_path))
    print(f"  JSON 已注入，等待导入完成 ...")

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
    """Auto-verify the timeline has frames."""
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
    """Render video via OPFS streaming → chunked readback."""
    await page.evaluate("""async () => {
        window.__renderStatus = null;
        window.__renderProgress = -1;
        window.__renderError = null;
        const root = await navigator.storage.getDirectory();
        window.__opfsHandle = await root.getFileHandle('v5_render.mp4', {create:true});
        window.__opfsWritable = await window.__opfsHandle.createWritable();
        window.__opfsWritableClosed = false;
        window.scene.events.on('progressUpdate', (opts) => {
            if (opts.progress !== undefined) window.__renderProgress = opts.progress;
        });
    }""")

    settings = {
        "startFrame": 0, "endFrame": total_frames - 1,
        "frameRate": fps, "width": VIDEO_WIDTH, "height": VIDEO_HEIGHT,
        "bitrate": VIDEO_BITRATE, "transparentBg": False, "showDebug": False,
        "format": VIDEO_FORMAT, "codec": VIDEO_CODEC,
    }

    print(f"\n  视频设置: {VIDEO_WIDTH}x{VIDEO_HEIGHT}, {fps}fps, "
          f"{VIDEO_FORMAT}/{VIDEO_CODEC}, high quality")
    print(f"  帧范围: 0 - {total_frames - 1}  (共 {total_frames} 帧)")
    print(f"\n  开始渲染 (OPFS 流式输出)...")

    await page.evaluate("""(settings) => {
        window.__renderStatus = 'running';
        window.__renderProgress = 0;
        window.scene.events.invoke('render.video', settings, window.__opfsWritable)
            .then(ok => { window.__renderStatus = ok ? 'done' : 'failed'; })
            .catch(e => { window.__renderStatus = 'error';
                          window.__renderError = String(e); })
            .finally(async () => {
                if (!window.__opfsWritableClosed) {
                    try { await window.__opfsWritable.close();
                          window.__opfsWritableClosed = true; } catch (_) {}
                }
            });
    }""", settings)

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
    print()

    status = await page.evaluate("window.__renderStatus")
    if status == "error":
        error_msg = await page.evaluate("window.__renderError || 'unknown'")
        print(f"  ERROR: 渲染失败 — {error_msg}")
        return False
    if status == "failed":
        print(f"  WARNING: render.video 返回 false (可能被取消)")
        return False

    await page.evaluate("""async () => {
        if (!window.__opfsWritableClosed) {
            try { await window.__opfsWritable.close(); } catch (_) {}
            window.__opfsWritableClosed = true;
        }
    }""")

    print(f"  渲染完成，从 OPFS 读取文件...")

    renders_dir.mkdir(parents=True, exist_ok=True)
    target_path = unique_path(renders_dir / expected_filename)
    if target_path.name != expected_filename:
        print(f"  (输出重命名: {expected_filename} → {target_path.name})")

    try:
        file_size = await page.evaluate("""async () => {
            const root = await navigator.storage.getDirectory();
            const handle = await root.getFileHandle('v5_render.mp4');
            const file = await handle.getFile();
            return file.size;
        }""")
        print(f"  OPFS 文件大小: {file_size / 1024**2:.1f} MB")

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
                        return new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onload = () => {
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
        print()
        print(f"  OPFS 读回完成 → {target_path} ({file_size / 1024**2:.1f} MB)")

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
