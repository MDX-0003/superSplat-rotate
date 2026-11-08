# 导出侧中心点渲染路径改造计划

## 目标

导出时可以在两种渲染结果之间选择：

- 正常渲染：现有的高斯渲染。
- 中心点叠加渲染：在高斯画面上叠加“颜色中心”路径绘制的点，点的颜色取高斯自身颜色与 SH 结果。

范围限定为单 PLY、单条 JSON 相机轨迹，不处理 multi-PLY 分段播放。

## 排查结论

1. “颜色中心”不是一条独立渲染路径。
   - UI 开关在 `src/ui/view-panel.ts:460-477`，事件为 `view.setCentersUseGaussianColor`。
   - 状态在 `src/editor.ts:738-744`。
   - 它最终变成 `SplatOverlay` 的 `useGaussianColor` uniform（`src/splat-overlay.ts:164`）。
   - shader 分支在 `src/shaders/splat-overlay-shader.ts:129-148`：为 1 时用 `splatColor` 与 SH，否则用 `unselectedClr`（默认蓝色）。
   - 因此它和蓝色中心共用同一条渲染路径，差别只是点颜色。

2. 实时 JSON 播放可以生效，但“颜色中心”自己不会激活这条路径。
   - 生效条件是 `splatSize > 0`、`camera.overlay`、`camera.mode === 'centers'`、`renderOverlays` 同时成立（`src/splat-overlay.ts:183-187`）。
   - `centersColorToggle` 的 change 只触发 `view.setCentersUseGaussianColor`，不打开叠加层，也不切换模式（`src/ui/view-panel.ts:666-668`）。
   - 对比“中心大小”滑块会额外触发 `camera.setOverlay(true)` 与 `camera.setMode('centers')`（`src/ui/view-panel.ts:655-659`）。

3. 导出时一定不生效。
   - `render.image`、`render.imageSequence`、`render.video`、`render.batchGtCameras` 都会设置 `scene.gizmoLayer.enabled = false`（`src/render.ts:137`、`301`、`489`、`207`）。
   - `SplatOverlay` 的实体挂在 gizmo 层（`src/splat-overlay.ts` 的 `layers: [scene.gizmoLayer.id]`），层被关掉就不会绘制。
   - `renderOverlays` 被设成 `showDebug`，默认 `false`；即使勾选调试覆盖，层也已经关闭。

4. `centersUseGaussianColor` 没有写进文档序列化。
   - `docSerialize.view` 与 `docDeserialize.view` 都没有这个字段（`src/editor.ts:797-828`），保存再打开会回到默认 `false`。

## 改动方案

### 1. 让“颜色中心”在播放时自激活

文件：`src/ui/view-panel.ts:666-668`。

`centersColorToggle.on('change')` 除了触发 `view.setCentersUseGaussianColor`，再触发：

- `camera.setOverlay(true)`
- `camera.setMode('centers')`

这样打开“颜色中心”就等于显式选择这条渲染路径，播放时立即可见，不再依赖用户先动“中心大小”或按 Tab。

### 2. 把中心点渲染放到独立的 overlay 层

文件：`src/scene.ts`、`src/camera.ts`、`src/splat-overlay.ts`。

- `src/scene.ts` 新增 `overlayLayer`（`new Layer({ name: 'SplatOverlay' })`），在 `gizmoLayer` 之后 push。
- `src/camera.ts:332-336` 的 `mainCamera.camera.layers` 加入 `scene.overlayLayer.id`。
- `src/splat-overlay.ts` 的实体层从 `gizmoLayer` 改为 `overlayLayer`。
- `src/camera.ts` 新增独立的 `overlayPass`。渲染通道是显式枚举图层的（`mainPass`、`splatPass`、`gizmoPass` 各自 `addLayer`），仅把实体移到新层不足以让它被绘制；`overlayPass` 在 `gizmoPass` 之后执行，只包含 `overlayLayer`。
- 新增一个独立的可见性标志 `Camera.renderCenters`，让 `SplatOverlay.enabled` 用它替代 `scene.camera.renderOverlays`。
- 这样导出时关闭 gizmo 层不会影响中心点，而网格与相机位姿 gizmo 仍然只受 `renderOverlays` 控制。

### 3. 导出参数与 UI

只在“左上角 渲染 → 视频”这条路径增加选项，其余导出路径只屏蔽 overlay 层，不增加 UI。

新增 `VideoSettings.centersOverlay: boolean`，默认 `false`。

- `src/render.ts` 的 `VideoSettings` 增加该字段。
- `src/ui/video-settings-dialog.ts` 增加一个 `BooleanInput`，标签是 `popup.render-video.centers-overlay`，中文为“中心点叠加（高斯颜色）”。
- `static/locales/en.json` 与 `static/locales/zh-CN.json` 增加对应文案，其余语言回退到英文。

导出开始时：

- 保存当前 `camera.mode`、`camera.overlay`、`view.centersUseGaussianColor`。
- 若 `centersOverlay` 为真，临时设置为 `centers`、`true`、`true`。
- 设置 `scene.overlayLayer.enabled = centersOverlay`。
- 在 `finally` 中恢复所有状态与层开关。

`renderOverlays` 仍按 `showDebug` 处理，确保不把网格和相机 gizmo 带进导出画面。

### 3.1 其它导出路径

`render.offscreen`、`render.image`、`render.imageSequence`、`render.batchGtCameras` 保持原有输出，只新增 `scene.overlayLayer.enabled = false`，并在 `finally` 中恢复为 `true`，避免中心点因为换层而意外进入这些导出。

### 4. 持久化“颜色中心”状态

文件：`src/editor.ts:797-828`。

- `docSerialize.view` 增加 `centersUseGaussianColor`。
- `docDeserialize.view` 增加对应的 `view.setCentersUseGaussianColor` 回填。

## 验收标准

1. 实时预览：导入 JSON 轨迹并播放，打开“颜色中心”，画面上出现带高斯颜色的点云；关闭后点云消失。
2. 单帧图片导出：开关关闭时与现有输出一致；打开时出现中心点，且没有网格、边界框、相机 gizmo。
3. PNG 序列与视频导出：行为与单帧一致，逐帧都有中心点。
4. GT 相机批量导出：行为与单帧一致。
5. 保存并重新打开文档后，“颜色中心”状态保持。
6. 不回归：蓝色中心模式（颜色中心关闭）在实时预览中仍然按现有逻辑工作。

## 风险与待确认

- 导出开关只提供“高斯颜色 + overlay”这一种叠加；不导出蓝色中心。如果也要蓝色中心，需要把导出开关改成三态。
- 中心点大小保持现有逻辑 `camera.splatSize * devicePixelRatio`（`src/splat-overlay.ts:166`），不随导出分辨率缩放，已确认按此实现。
- 导出期间临时修改 `camera.mode` 等全局状态，必须严格在 `finally` 中恢复，否则退出导出后视口状态会被污染。
- 中心点只挂在当前选中的 splat 上。单 PLY 场景默认加载后即被选中，若用户手动取消选择，导出不会有中心点。

## 验证记录

- `npx tsc --noEmit -p tsconfig.json` 通过。
- `npm run build:raw` 通过。
- 实测导入 `CameraData/05/cameras.json` 与 `CameraData/05-clip/0630-120849.ply`，打开“颜色中心”后自动切到 centers 模式并打开叠加层。
- 实测导出 320x180、3 帧视频：`centersOverlay: true` 时画面出现高斯颜色点，`centersOverlay: false` 时与正常渲染一致；两帧 PSNR 约 20.6 dB，确认输出不同。
- 实测导出结束后 `camera.mode`、`camera.overlay`、`centersUseGaussianColor` 与层开关均恢复。
- 实测 `docSerialize.view` 包含 `centersUseGaussianColor`，`docDeserialize.view` 能回填。
