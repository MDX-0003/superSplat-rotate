# SuperSplat 自动化管线 (v5) — 技术文档

> 最后更新: 2026-06-27

## 1. 目标

将 SuperSplat 网页端的 PLY 加载 + 相机导入 + 视频渲染全部自动化，
同时将上游的 LiteGS 3DGS 训练也串联进管线。
使 Python 管线脚本能直接操控浏览器完成这些操作，无需人工拖拽和点击。

## 2. 核心原理

### 2.1 浏览器自动化 — Playwright

使用 [Playwright for Python](https://playwright.dev/python/) 操控 Chromium 浏览器。
Playwright 通过 Chrome DevTools Protocol (CDP) 与浏览器通信，可以：

- 启动 / 连接到 Chrome 实例
- 在页面上下文中执行任意 JavaScript (`page.evaluate()`)
- 操作 DOM 元素 (`page.click()`, `page.locator()`)
- 拦截文件下载和上传 (`set_input_files`, `wait_for_event("download")`)

**为什么不用 Selenium**：Playwright 对现代浏览器的 CDP 支持更深，文件上传 (`set_input_files`)
不会弹出系统对话框，`page.evaluate()` 的序列化性能更好。

### 2.2 关键发现：SuperSplat 的 `events` 总线

SuperSplat 所有核心操作都通过内部的 `events` 系统暴露：

```javascript
// 导入任意文件（PLY / JSON / splat 等）
window.scene.events.invoke('import', [{ filename, contents: File }])

// 渲染视频
window.scene.events.invoke('render.video', settings, writableStream)

// 查询 / 设置时间轴
window.scene.events.invoke('timeline.frames')
window.scene.events.invoke('camera.importedPoses')
```

这意味着 Playwright 可以**绕过所有 UI 交互**，直接调用 SuperSplat 的内部 API。

### 2.3 文件上传原理：动态 File Input + 零拷贝

浏览器中 `<input type="file">` 的 `onchange` 事件可以得到一个 `File` 对象——这是浏览器
原生文件句柄，指向磁盘上的文件，不会把整个文件读入 JavaScript 内存。

Playwright 的 `page.locator("input").set_input_files(path)` 把本地路径传给浏览器，
浏览器创建 File 对象，触发 `onchange`。整个过程是**零拷贝**的——文件内容不会经过 Python
进程或 CDP 传输。

**对比**：早期尝试把文件 base64 编码后通过 `page.evaluate()` 传入（见 §5.1），
对大文件会造成 Chrome OOM。

### 2.4 视频输出原理：OPFS 流式写入

SuperSplat 的 `render.video(settings, fileStream?)` 有两种模式：

| `fileStream` | 模式 | 行为 |
|-------------|------|------|
| `undefined` | `BufferTarget` | 全部帧编码完才下载 → 4K 长视频会 OOM |
| `FileSystemWritableFileStream` | `StreamTarget` | 增量写入 → 内存占用极小 |

我们使用 [Origin Private File System](https://developer.mozilla.org/en-US/docs/Web/API/File_System_API/Origin_private_file_system) (OPFS)
创建 `writableStream`，渲染完成后从 OPFS 分段读回 Python：

```
render.video(settings, opfsWritable)
  → StreamTarget.write(chunk) × N    (增量写入 OPFS)
  → writable.close()                 (flush 到磁盘)
  → file.slice(offset, 10MB) × M     (分段读回，避免 ArrayBuffer OOM)
  → FileReader → base64 → Python     (每段 10MB，内存可控)
```

## 3. 核心库

| 库 | 用途 |
|----|------|
| `playwright` (Python) | 浏览器启动 / CDP 连接 / 页面操控 |
| `asyncio` | 渲染进度轮询、下载等待、超时控制 |
| `subprocess` | 调用 clip_ply / ffmpeg / LiteGS batch_run |
| `pathlib` | 跨平台路径处理 |
| `json` | pipeline.json / presets.json 读写 |

## 4. Config 设计

### 4.1 pipeline.json

每个项目一个 `CameraData/<project>/pipeline.json`。
v5 不再内联 PLY 处理参数——全部委托给 `presets.json`。
pipeline.json 只保留项目级别的元数据：

```json
{
  "project": "01",
  "preset": "01-0625测试-3人",
  "jsons_path": "E:/Programs/UE Project/JustATest/Content/JSON_Out",
  "litegs_path": "E:/work/26.7_SKNJ/LiteGSWin",
  "output": {
    "fps": 25,
    "crf": 6,
    "resolution": "3840x2160",
    "source": "raw_images",
    "segments": [
      { "type": "real",   "start": 0,   "end": 74 },
      { "type": "render", "replace_frames": 114 },
      { "type": "real",   "start": 188, "end": 262 }
    ]
  }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `project` | 是 | 项目名，对应 `CameraData/<name>/` |
| `preset` | 是 | 指向 `tills_ply/presets.json` 中的 preset 名。v5 从中读取 `path`、`max_index`、`clip` 参数 |
| `jsons_path` | 否 | 存放相机 JSON 文件的文件夹绝对路径。不存在时跳过 JSON 导入 |
| `litegs_path` | 否 | LiteGSWin 仓库的绝对路径。`--steps train` 必需 |
| `output` | 是 | 视频参数（fps/crf/resolution/source/segments） |

**关键设计**：
- `segments` 中 `render` 类型不再需要 `seq` 字段——从前一个 `real` segment 的 `end+1` 自动推导文件名（如 `74+1=75` → `seq_f075.mp4`）
- 所有旧文件（`presets.json`, `tl_*.json`, `ply_pipeline.py`, `run_pipeline_v4.py`）保持不变，向后兼容
- `preset` 引用的 `presets.json` 中只需 `clip` 段参数有效；`interpolate` 和 `fuse` 段虽保留但 v5 不读取

### 4.2 向后兼容

v5 同时支持旧格式（内联 `max_index`/`clip`/`fuse`），避免 08 等旧项目需要迁移。
新旧格式由 `validate_config()` 自动识别：有 `preset` → 新格式；有 `max_index` + `clip` → 旧格式。

## 5. 完整流程

### 5.1 步骤总览

```
--steps train:
  T1  extract_train_images     → Train_imgs/<date_str>/
  T2  复制 Train_imgs/<date_str>/ → LiteGSWin/data/<sub_dir>/<date_str>/
  T3  subprocess: uv run python batch_run.py --sub_dir <sub_dir>
      (已有结果 PLY 的帧自动跳过)
  T4  复制 results/<sub_dir>/<sub_dir>-<frame_id>.ply → CameraData/<project>/
  T5  subprocess: clip_ply.py (参数来自 preset 的 clip 段) → XX-clip/*.ply

--steps clip:
  对 CameraData/<project>/ 下所有 PLY 跑 clip_ply (参数来自 preset)

--steps render:
  Step 4a  [交互] 列出 XX-clip/*.ply → 用户输入 idx
  Step 4b  [交互] 列出 cameras_folder/*.json → 用户输入 idx (仅当 jsons_path 存在)
  Step 5a  [auto] Playwright 启动 Chrome + 上传 PLY
  Step 5b  [auto] 上传 JSON → 自动导入 GT+Timeline
  Step 6   [auto] 验证时间轴就绪
  Step 7   [auto] 视频渲染 (OPFS 流式) + 分段读回下载
  Step 8   [auto] 提取实拍帧 → 编码 MP4 → TS concat → output.mp4

默认 (无 --steps):
  train → clip → render  (全流程)
```

### 5.2 `--steps train` 详细流程

**关键变量推导**（从源图片文件名前缀中提取 `date_str`）：

| 变量 | 来源 | 示例值 |
|------|------|--------|
| `date_str` | prefix 中的时间戳 | `2026-06-25-162636` |
| `sub_dir` | date_str 的 MMDD 部分 | `0625` |
| `frame_id` | date_str 的 HHMMSS 部分 | `162636` |
| `ply_src` | LiteGS results 命名公式 | `results/0625/0625-162636.ply` |
| `ply_dst` | 拷贝目标 | `CameraData/01/0625-162636.ply` |

推导逻辑：
```python
def parse_train_vars(date_str):
    """'2026-06-25-162636' → ('0625', '162636')"""
    parts = date_str.split("-")   # ['2026', '06', '25', '162636']
    return parts[1] + parts[2], parts[3]   # MMDD, HHMMSS
```

**幂等性**：
- T2：目标帧目录已存在 → 跳过
- T3：result PLY 已存在（batch_run 自带检查）→ 跳过
- T4：目标 PLY 已存在 → 跳过
- T5：clip 产出目录已存在 → 跳过（`--force` 可强制重跑）
- `--force`：全局强制覆盖所有中间产物

**LiteGSWin 数据流**：v5 先将 Train_imgs 中的图像拷贝到 LiteGSWin 的 `data/<sub_dir>/<date_str>/` 目录（作为 loose files），
然后调用 `uv run python batch_run.py --sub_dir <sub_dir>`。
`batch_run.py` 的 `ensure_raw_images()` 会自动将 loose files 移入 `raw_imgs/` 子目录并启动训练流程。

### 5.3 PLY 上传细节

```python
async def upload_ply(page, ply_path):
    # 1. 注入隐藏 <input type="file"> 到页面
    await page.evaluate("""
        const input = document.createElement('input');
        input.type = 'file';
        input.id = '__v5_ply_input';
        input.onchange = () => {
            const file = input.files[0];
            window.scene.events.invoke('import', [{
                filename: file.name,
                contents: file
            }]);
        };
        document.body.appendChild(input);
    """)

    # 2. Playwright 设文件 → Chrome 读磁盘 → onchange 触发
    await page.locator("#__v5_ply_input").set_input_files(str(ply_path))

    # 3. 轮询等待 import 完成
    for i in range(120):
        count = await page.evaluate("window.scene.events.invoke('scene.splats').length")
        if count > 0:
            return   # 成功
        await asyncio.sleep(1)
```

**关键**：`set_input_files()` 不会弹出文件选择对话框——Playwright 通过 CDP 直接设值。
Chrome 从磁盘读取文件，创建 `File` 对象，然后 SuperSplat 的 `importFiles()` 处理该文件。
整个过程不经过 Python 内存。

### 5.4 JSON 导入细节

SuperSplat 源码修改（`src/file-handler.ts:163`）：

```typescript
// 默认模式从 null 改为 'both'——不再弹对话框
let cameraImportSessionMode: 'gt' | 'timeline' | 'both' | null = 'both';
```

导入流程与 PLY 相同（动态 input + `set_input_files`），SuperSplat 内部的
`importFiles()` 检测到 `.json` 文件 → 直接调用 `loadCameraPoses(file, events, 'both')`
→ 同时添加到 GT 相机列表和时间轴关键帧。对话框永不出现。

### 5.5 视频渲染细节

```python
async def render_video(page, total_frames, renders_dir, expected_filename, fps):
    # 1. 创建 OPFS writableStream
    await page.evaluate("""
        const root = await navigator.storage.getDirectory();
        window.__opfsHandle = await root.getFileHandle('v5_render.mp4', {create:true});
        window.__opfsWritable = await window.__opfsHandle.createWritable();
    """)

    # 2. 用 writableStream 启动渲染（StreamTarget 模式，逐帧写 OPFS）
    settings = { "startFrame": 0, "endFrame": total_frames - 1,
                 "frameRate": fps, "width": 3840, "height": 2160,
                 "bitrate": 41472000, "format": "mp4", "codec": "h264" }
    await page.evaluate("""
        window.scene.events.invoke('render.video', settings, window.__opfsWritable)
    """)

    # 3. 轮询进度，等待渲染完成

    # 4. 从 OPFS 分段读回 (10 MB/chunk)
    with open(target_path, "wb") as out:
        while total_read < file_size:
            chunk_b64 = await page.evaluate("""
                async ([start, end]) => {
                    const file = await handle.getFile();
                    const blob = file.slice(start, end);
                    return new Promise(resolve => {
                        const r = new FileReader();
                        r.onload = () => resolve(r.result.split(',')[1]);
                        r.readAsDataURL(blob);
                    });
                }
            """, [total_read, min(total_read + CHUNK_SIZE, file_size)])
            out.write(base64.b64decode(chunk_b64))
```

**为什么分段读回**：OPFS 中的完整文件可能有几百 MB。一次性 `Array.from(new Uint8Array(ab))`
会把整个文件复制到 JS 堆中，导致 Chrome OOM。分段 `file.slice()` + `FileReader` 限制
每段最多 10 MB 在 JS 内存中。

### 5.6 视频拼接

两个关键修复保证 TS concat 无误：

1. **色彩范围统一**：实拍 JPEG 输入经 ffmpeg 编码为 H.264 时加 `-vf "scale=iw:ih:out_range=tv"`，输出 TV 限制范围（16-235），与 SuperSplat 渲染的 MP4 一致。

2. **Concat demuxer 替代 concat protocol**：

```bash
# 旧方案（有 DTS 乱序问题）
ffmpeg -i "concat:a.ts|b.ts|c.ts" -c copy -fflags +genpts out.mp4

# 新方案（自动调整时间戳）
echo "file 'a.ts'" > list.txt
echo "file 'b.ts'" >> list.txt
ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp4
```

`concat` demuxer 逐文件读入并自动重算 PTS/DTS，不会有 `DTS out of order` 警告。

## 6. 废弃方案与 Bug 分析

以下是实现过程中尝试过、最终被淘汰的方案及其根因分析。

### 6.1 Base64 方式上传大文件 → Chrome OOM

**做法**：Python 读取 PLY → `base64.b64encode()` → 108 MB 字符串 → `page.evaluate(js_code, [b64_str])` → JS 中 `atob()` + `new Uint8Array()` → 创建 `File` → `events.invoke('import')`

**现象**：小于 40 MB 的 PLY 正常，81 MB PLY 时报 `Page.evaluate: Target page, context or browser has been closed`。

**根因**：108 MB base64 字符串在 CDP JSON 序列化时占用大量内存，进入 JS 后 `atob()` + `Uint8Array` 又创建两份副本。81 MB 原始文件 → 108 MB base64 → ~300+ MB JS 堆占用 → 超过 Chrome 默认 4 GB 限制 → 页面崩溃。

**正确做法**：动态 `<input type="file">` + `set_input_files`，Chrome 直接从磁盘读取 File 对象，零 JS 内存占用（见 §2.3）。

### 6.2 `page.wait_for_event("download")` 不触发

**做法**：`render.video()` 完成后内部调用 `downloadFile()` 创建一个 blob URL 并点击 `<a download>` 链接，期待 Playwright 截获这次下载。

**现象**：下载事件 10 秒内不触发，必须等待 fallback。

**根因**：Playwright 的 `wait_for_event("download")` 监听的是浏览器下载管理器的事件。
`downloadFile()` 通过 `URL.createObjectURL(blob)` + `<a>.click()` 触发的是**程序化下载**，
不经过浏览器的下载管理器，因此 Playwright 无法截获。

**正确做法**：OPFS 流式写入 + 分段读回（见 §2.4, §5.5），完全绕过下载机制。

### 6.3 `render.video` 使用 BufferTarget → 4K 视频 OOM

**做法**：不传 `fileStream`，SuperSplat 使用 `BufferTarget` 缓冲全部编码数据 → `downloadFile()` 下载。

**现象**：368 帧 4K 视频渲染到 96% 时 Chrome 崩溃：
```
FATAL ERROR: Ineffective mark-compacts near heap limit Allocation failed -
JavaScript heap out of memory
```

**根因**：`BufferTarget` 在 JS 堆中累积全部编码帧。4K H.264 高码率时 BufferTarget 可达数 GB。
后续的 `Array.from(new Uint8Array(ab))` 复制了整个 buffer，触发 4 GB V8 堆限制。

**正确做法**：OPFS `StreamTarget`，编码数据逐帧写入磁盘，不在 JS 内存中累积（见 §2.4）。

### 6.4 `events.function()` 重复注册 → 页面报错

**做法**：在 Playwright 中调用 `events.function('show.cameraImportDialog', () => ({mode:'both'}))` 覆盖对话框函数。

**现象**：`Error: function show.cameraImportDialog already exists`

**根因**：SuperSplat 的 `events.function()` 在注册前检查 `functions.has(name)`，已存在时直接 throw。

**正确做法**：直接修改 SuperSplat 源码——把 `cameraImportSessionMode` 默认值从 `null` 改为 `'both'`（一行改动，`src/file-handler.ts:163`），从根源上消除对话框。

### 6.5 浏览器生命周期问题

**做法（迭代过程）**：
1. 最初：Playwright 启动新 Chrome → 用户已有窗口被忽略
2. 然后：尝试 `connect_over_cdp("http://127.0.0.1:9222")` 连接已有 Chrome → 端口不通
3. 排查：用户手动 `chrome.exe --remote-debugging-port=9222` → 端口仍无响应 → 旧 Chrome 进程未完全关闭

**最终做法**：v5 首次运行时自己启动 Chrome（`channel="chrome"`, `executable_path` 明确指定,
`args=["--remote-debugging-port=9222"]`）。Chrome 窗口保持打开，后续 v5 运行通过 CDP
自动连接到已存在的 Chrome 实例。

### 6.6 其他小问题

| 问题 | 根因 | 修复 |
|------|------|------|
| `step()` 缺 `shell` 参数 | 从 ply_pipeline 复制时签名不一致 | 补上 `shell=False`, `cwd=None` |
| `timeline.frames` 默认 180 | SuperSplat 新建场景就有 180 帧空时间轴 | 改用 `camera.importedPoses().length` 判断是否已导入相机 |
| Concat `DTS out of order` | 实拍 `yuvj420p(pc)` vs 渲染 `yuv420p(tv)` 色彩范围不同 + `concat:` 协议不调时间戳 | 编码实拍帧时加 `-vf "scale=iw:ih:out_range=tv"` + concat demuxer |
| clip 参数 `KeyError: height_up` | preset 新旧格式差异（voxel vs cylinder denoise） | `build_clip_args` 按参数存在性动态构建 |

## 7. 命令行参考

```bash
# 全流程（需要先 npm run serve 启动 dev server）
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json

# 仅训练（提取素材 → LiteGS → clip）
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --steps train

# 仅 clip 处理
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --steps clip

# 仅渲染 + concat
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --steps render

# 训练 + 渲染全流程（跳过已完成的训练）
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --steps train,clip,render

# 强制重新生成所有中间产物
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --force

# 调试模式（保留中间 TS/MP4 文件）
python tills/run_pipeline_v5.py --config CameraData/01/pipeline.json --debug
```

## 8. 文件清单

| 文件 | 说明 |
|------|------|
| `tills/run_pipeline_v5.py` | v5 主控脚本 |
| `CameraData/<project>/pipeline.json` | 统一配置文件（每项目一个） |
| `tills_ply/presets.json` | PLY 处理参数预设（v5 从中读取 clip 参数） |
| `src/file-handler.ts` | SuperSplat 源码，`cameraImportSessionMode` 默认改为 `'both'` |
| `tills_ply/clip_ply.py` | PLY 裁剪 / 去噪子脚本（被 v5 的 `--steps train` 和 `--steps clip` 调用） |
| `tills/run_pipeline_v4.py` | 旧版管线（保持独立可用） |
| `tills_ply/ply_pipeline.py` | PLY 全流程工具（interpolate→fuse→clip，保持独立可用） |
