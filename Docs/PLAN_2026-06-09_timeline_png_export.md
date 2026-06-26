# PLAN — Timeline PNG Sequence Export

## 现状

SuperSplat 有三条渲染输出路径，全部共用同一段 GPU 读回管线：

| 路径 | 触发 | 帧来源 | 输出 |
|------|------|--------|------|
| `render.batchGtCameras` | View Panel → Export All | GT 相机 poses | 逐帧 PNG 压缩 → 逐个下载 |
| `render.video` | Menu → Render → Video | Timeline 帧迭代 | 逐帧 RGBA → WebCodecs → MP4/WebM |
| `render.image` | Menu → Render → Image | 当前帧 | 单帧 PNG → 下载 |

**Video 路径从不在磁盘生成 PNG。** RGBA 像素从 GPU 读回后直接喂给 `VideoEncoder.encode()`。

## 目标

新增一条路径：**Timeline → PNG Sequence**，从时间轴帧范围渲染出全部 PNG，不编码为视频。用于管线自动化——替换目前"手动 Export All"获取渲染结果的步骤。

## 设计

### 方案：新增 `render.exportTimelinePngs` 事件

复用 video 路径的帧迭代逻辑（timeline 帧 + PLY sequence + camera 插值），替换视频编码为 PNG 压缩。

**渲染核心（三路径共用，不变）：**
```
startOffscreenMode → setPose/timeline.time → sort splats → forceRender
  → postRender → copyRt → colorBuffer.read → Y-flip
```

**差异仅在后处理：**

| 路径 | 后处理 |
|------|--------|
| GT batch | `pngCompressor.compress()` → `downloadFile()` 逐个 |
| Video | `new VideoFrame(data)` → `encoder.encode()` |
| **PNG seq (新)** | `pngCompressor.compress()` → 累积或逐个下载 |

### UI

在视频设置对话框中增加一个选项，或更简单的方案——在 Image Settings 对话框中增加"All Timeline Frames"勾选。但最干净的方式是：

**新增菜单项：Menu → Render → Image Sequence**

点击后弹出 `ImageSequenceDialog`（或复用/扩展 `ImageSettingsDialog`），提供：
- Resolution: viewport / HD / QHD / 4K / custom
- Frame range: start / end（默认全部 timeline 帧）
- FPS override（默认 timeline 帧率）
- Transparent background
- Show debug overlays

确认后调用 `render.exportTimelinePngs`。

### 下载策略

两选一：

**A) 逐文件下载（简单，类比 GT Export All）：**
每个 PNG 独立触发浏览器下载，N 帧 = N 个文件。浏览器会在下载栏堆积 N 个文件。小项目可用，大项目（300+ 帧）体验差。

**B) 全部渲染完 → 打包 ZIP → 单次下载（推荐）：**
渲染循环内累积 `{name, ArrayBuffer}`，全部完成后用 JSZip 打包为单个 ZIP 下载。管线用户拿到一个 ZIP，解压到 `renders/` 即可。

选择 **B**——需要引入 JSZip（npm 包或轻量实现）。

### 涉及文件

| 文件 | 动作 | 说明 |
|------|------|------|
| `src/render.ts` | **修改** | 新增 `render.exportTimelinePngs` 事件处理器，组合 video 的帧迭代 + PNG 压缩 + ZIP 打包 |
| `src/ui/editor.ts` | **修改** | 注册 `show.imageSequenceDialog` → 触发 `render.exportTimelinePngs` |
| `src/ui/menu.ts` | **修改** | 新增 "Image Sequence" 菜单项 |
| `src/ui/image-settings-dialog.ts` | **修改** | 加一个 checkbox "All Timeline Frames" 和 frame range 输入；或者新建独立 dialog |
| `static/locales/en.json` | **修改** | 新增相关字符串 |

### `render.exportTimelinePngs` 伪代码

```typescript
events.on('render.exportTimelinePngs', async (settings) => {
    const { width, height, startFrame, endFrame, transparentBg, showDebug } = settings;
    const timelineFrames = events.invoke('timeline.frames');
    const fps = events.invoke('timeline.frameRate');

    // lock to prevent throttling
    await navigator.locks.request('supersplat-png-render', async () => {
        scene.camera.startOffscreenMode(width, height);
        // setup transparentBg, debug, etc.

        const pngFiles = [];
        for (let f = startFrame; f <= endFrame; f++) {
            // same frame prep as video path:
            const frameTime = f / fps;
            events.fire('timeline.time', frameTime);
            await plysequence.setFrameAsync(f);  // if PLY sequence
            scene.camera.onUpdate(0);
            // ... sort splats ...
            scene.forceRender = true;
            await postRender();

            // same readback as GT batch:
            scene.dataProcessor.copyRt(mainTarget, workTarget);
            const data = new Uint8Array(width * height * 4);
            workTarget.colorBuffer.read(0, 0, width, height, { data });
            // Y-flip
            const rgba = new Uint32Array(data.buffer);
            yFlipInPlace(rgba, width, height);

            // compress to PNG
            const pngBuffer = await pngCompressor.compress(rgba, width, height);
            pngFiles.push({ name: `circle_${String(f).padStart(4, '0')}.png`, data: pngBuffer });
        }

        scene.camera.endOffscreenMode();

        // package as ZIP and download
        const zip = await createZip(pngFiles);
        downloadFile(zip, 'renders.zip');
    });
});
```

### JSZip 方案

引入 `jszip` npm 包（~10KB gzipped），在 `postRender` 中已经有 lodepng 等依赖先例。

```typescript
import JSZip from 'jszip';

const zip = new JSZip();
for (const {name, data} of pngFiles) {
    zip.file(name, data, { binary: true });
}
const zipBlob = await zip.generateAsync({ type: 'blob' });
downloadBlob(zipBlob, 'renders.zip');
```

### 替代方案：自建 ZIP（免依赖）

浏览器 `CompressionStream` API 不支持 ZIP 格式。最小化方案：不打包，渲染完所有帧后逐个触发下载（跟 GT Export All 一样）。管线用户攒 300 个 PNG 也可以接受，但在浏览器默认下载路径里会比较分散。

**建议：先用逐文件下载实现，ZIP 打包作为后续优化。**

## 实施步骤

1. 在 `src/render.ts` 中抽取出共享的帧渲染函数 `renderFrame(width, height): Promise<Uint32Array>`
2. 新增 `render.exportTimelinePngs` 事件处理器
3. 在菜单中加 "Image Sequence" 项
4. 创建 `ImageSequenceDialog`（或扩展现有 dialog）
5. 测试：导出 timeline 全部帧 → 验证 PNG 数量和内容正确

## 与管线的衔接

管线 v1/v2 的 Step 4 从"手动 Export All"改为："SuperSplat → Render → Image Sequence → 下载 renders.zip → 解压到 `renders/`"。

后续可以进一步自动化（通过 iframe-api 或浏览器扩展触发渲染），但不在本次范围。
