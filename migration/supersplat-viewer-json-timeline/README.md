# superSplat JSON 相机导入迁移包

这个迁移包把 superSplat 编辑器里“导入 cameras.json 到时间轴”的能力拆成可移植的
数据转换代码、viewer 接入代码、补丁说明和数值验证脚本，用来让基于
**superSplat-viewer** 的项目支持同一套坐标规则、同一套时间轴语义的 JSON 导入与播放。

- 来源项目：本仓库（superSplat 编辑器 fork），提交 `1a9832a`。
- 对比目标：`@playcanvas/supersplat-viewer` 1.42.1，提交 `a66496e`。
- 验证样本：`CameraData/05/cameras.json`（120 帧，带 `fx/fy/width/height`）。

先区分两个仓库，避免把编辑器的功能误当成 viewer 已有的功能：

- **superSplat 编辑器**（本仓库 `src/`）有文件导入、时间轴、相机轨道编辑、GT 相机渲染，
  JSON 导入逻辑在 `src/file-handler.ts` 的 `loadCameraPoses`。
- **superSplat-viewer** 只消费已经生成好的 `settings`，从 `settings.animTracks[0]`
  或自动生成的 rotate/figure-8 轨道里取相机动画。它没有“导入 JSON”入口，
  也没有相机关键帧编辑 UI。
- 两者加载 PLY 时都会把 splat 实体旋转 180°（绕 Z 轴），所以相机位姿的坐标转换规则可以完全复用。

---

## 最短路径

1. 对目标 viewer 应用 `patches/camera-manager-1.42.1.diff`；版本不同就按 `patches/camera-manager.patch.md` 手动插入。
2. 把 `src/camera-json-core.ts` 与 `src/camera-json-import.ts` 拷进目标项目。
3. 在 `viewer.state.loaded === true` 之后调用 `importCameraJsonFromFile(viewer, file)`。

运行时接入的完整步骤见第 7.2 节，数值验证方法见第 9 节。

---

## 1. 迁移包解决的问题

目标项目需要在不重写渲染与相机系统的前提下获得三件事：

1. 读入与编辑器相同格式的 `cameras.json`，并按相同规则挑选、排序、跳过记录；
2. 把每条记录转换成 viewer 世界坐标下的 `position / target / fov`；
3. 把结果变成 viewer 能播放的 `AnimTrack`，并让播放、暂停、拖动进度条、
   `viewer.seek(seconds)`、`captureFrame({ time })` 都作用在这条新轨道上。

迁移包用两条路径覆盖不同集成深度：

- **方案 A（不改 viewer 源码）**：在 `createViewer` 之前把 JSON 转成
  `settings.animTracks / settings.cameras / settings.startMode`，随 settings 一起传入。
  适合“每次打开页面只播一条预先确定的轨道”的场景。
- **方案 B（推荐，运行时导入）**：给 `CameraManager` 加一个替换轨道的方法和事件，
  页面加载完成后拖入 JSON 即可立即替换并播放，不需要重建 viewer，也不重新下载 PLY。

两条路径共用同一份纯数据转换核心，因此坐标结果不会分叉。

---

## 2. 编辑器里 JSON 导入链的拆解

编辑器的调用链如下，每一步的输入与输出都点名：

1. `src/file-handler.ts` 的 `loadCameraPoses(file, events, mode)` 读取 JSON 数组。
2. 它用 `id` 或 `img_name` 末尾数字对记录排序，排序后的下标作为 `frame`。
3. 对每条记录，它把 `position` 从源世界转换到 splat 世界，把 `rotation` 的第 3 列
   转成前向，再令 `target = position + forward * 10`。
4. 它把 `fov` 解析成“较大轴视场角”，并保留 `intrinsics` 供 GT 校准渲染使用。
5. 它先 `events.fire('timeline.setFrames', json.length)`，再一次性
   `events.fire('camera.setPoses', timelinePoses)`。
6. `src/camera-poses.ts` 的 `CameraAnimTrack.loadPoses` 保存位姿；
   `rebuildSpline` 用 `CubicSpline.fromPointsLooping(duration = timeline.frames, ...)`
   建样条。
7. 播放时 `timeline.ts` 的游标为 `time = (time + dt * frameRate) % frames`，
   再把 `time` 传给 `CameraAnimTrack.evaluate`，后者采样样条并
   `events.fire('camera.setPose', pose)`。

viewer 侧的播放链是另一套对象，但数学部分相同：

1. `CameraManager` 从 `settings.animTracks[0]` 取 `AnimTrack`；
2. `AnimController` 构造 `AnimState`；
3. `AnimState.fromTrack` 把 `keyframes.times` 与 `position/target/fov` 组装成点列，
   用同一份 `CubicSpline.fromPointsLooping` 建样条；
4. 每帧 `cursor.update(dt)` 后执行 `spline.evaluate(cursor.value * frameRate)`；
5. `AnimController` 用采样出的 `position` 与 `target` 调用 `camera.look()`，并设置 `fov`。

因此迁移的关键不是“把编辑器的相机类搬过去”，而是把
**位姿解析 + 坐标转换 + AnimTrack 时间单位** 这三件事对齐。

---

## 3. 坐标规则

### 3.1 源数据约定

`cameras.json` 由 `tills/gltf_to_cameras_json.py` 一类工具生成。生成端把旋转矩阵按
“列是相机轴”写入：

- 第 0 列：相机右方向 `right`
- 第 1 列：相机上方向 `up`
- 第 2 列：相机前方向 `forward`（3DGS/COLMAP 的 +Z 朝前、+Y 朝下）

对应代码在 `tills/gltf_to_cameras_json.py` 第 596-600 行，矩阵按
`[[right.x, up.x, look.x], ...]` 组装，所以读取端应把 `rotation[i][2]` 当作前向分量。

### 3.2 splat 的 180° 旋转

编辑器在加载 PLY 后把 splat 实体的局部旋转设为 splat-transform 返回的变换；
viewer 在 `src/index.ts` 第 93 行直接写死：

```ts
entity.setLocalEulerAngles(0, 0, 180);
```

对于 3DGS PLY，这两条路径等价于世界变换矩阵：

```text
S = diag(-1, -1, 1)
```

相机位姿必须左乘同一个 `S`，否则相机会和 splat 差 180°。

### 3.3 位置

对源位置 `p = (x, y, z)`：

```text
position_viewer = S · p = (-x, -y, z)
```

对应 `src/file-handler.ts` 第 161 行：

```ts
const transformPlyPosition = (p: Vec3) => new Vec3(-p.x, -p.y, p.z);
```

### 3.4 前向与注视点

对旋转矩阵 `R`，源前向是第 3 列 `(R[0][2], R[1][2], R[2][2])`。乘 `S` 后：

```text
forward_viewer = (-R[0][2], -R[1][2], R[2][2])
```

viewer 的动画相机是 look-at 模型，只接受 `position` 与 `target`，没有 roll。
所以取：

```text
target = position_viewer + forward_viewer * 10
```

这里的 `10` 是编辑器第 217 行固定使用的注视距离。距离本身不影响朝向，只影响
轨道相机绕点的深度；迁移包保留 10，使 `position/target` 数值与编辑器逐位一致。

### 3.5 视场角

`fov` 的优先级必须与编辑器一致：

1. 记录里有 `fov_x`（水平视场角，单位度）时直接用它；
2. 否则用 `fx/fy/width/height` 计算水平与垂直视场角，取较大者；
3. 都没有时用 60°。

PlayCanvas 的 `camera.fov` 表示“较大轴”的视场角，并用
`horizontalFov = 画布宽 > 画布高` 决定它作用在水平还是垂直方向。编辑器用
`camera.horizontalFov = width > height`，viewer 用画布宽高，两者约定相同。
横屏素材配横屏画布时结果一致；竖屏素材在横屏画布上会因“较大轴”换轴而产生差异，
这一点见第 8 节。

### 3.6 为什么播放不需要完整四元数

编辑器另外构造了一个四元数供 GT 校准渲染使用（`transformPlyRotation`），
但时间轴播放只消费 `position / target / fov`。viewer 的 `AnimController` 同样只用
`camera.look(position, target)` 和 `fov`。所以迁移包只输出这三样，不搬运四元数，
也不引入 `rotation` 的转置或符号约定问题。

需要 GT 校准四元数时，按 `src/file-handler.ts` 第 165-182 行原样复制即可；
它的最终相机朝向已用 Playwright 实测与 `+forward` 一致（见第 9 节），
但中间四元数的分量会随 PlayCanvas 版本变化，不能当作可独立复用的方向依据。

---

## 4. JSON 解析规则

迁移包接受一个 JSON 数组，每个元素一条相机记录。用到的字段如下：

- `position`：必需，3 个有限数字。
- `rotation`：必需，3×3 有限数字矩阵，列是 `right/up/forward`。
- `id`：可选，排序优先使用它。
- `img_name`：可选，作为关键帧名称；没有 `id` 时用其末尾数字排序。
- `fov_x`：可选，水平视场角，单位度。
- `fx`、`fy`、`width`、`height`：可选，用于计算视场角。
- 其他字段（`cx`、`cy`、畸变参数等）会被忽略。

解析行为与编辑器对齐的部分：

- 排序键优先取 `id`，否则取 `img_name` 末尾数字；两者都无法解析时保持输入顺序。
- 排序后数组下标就是 `frame`，不是 `id`，也不是 `img_name` 里的数字。
- `frameCount` 等于输入数组长度，即使中间有记录被跳过。
- 缺少合法 `position` 或 `rotation` 的记录会被跳过，并写入 `warnings`；
  后续记录的 `frame` 仍按排序后的位置递增，因此可能出现不连续的帧号。

迁移包与编辑器有一处有意不同：

- 编辑器的排序比较写作 `(avalue && bvalue) ? ... : 0`，当 `id` 为 `0` 时会被当成
  falsy，导致 `id: 0` 不参与排序。迁移包按原本意图把 `0` 当作合法键，
  其余行为不变。这样乱序且从 0 开始编号的文件也能正确排序。

---

## 5. 时间轴映射

`AnimTrack` 有两个容易混淆的时间单位：

- `keyframes.times`：单位是“帧”，取 `pose.frame`；
- `duration`：单位是“秒”。

viewer 的 `AnimState.update(dt)` 先把以秒为单位的游标乘以 `frameRate`，再拿结果去采样
样条。所以只要把 `times` 写成帧号，`duration` 就必须写成
`frameCount / frameRate` 秒。

对应关系如下：

```text
times[i]        = pose[i].frame
frameRate       = 30（默认，可配置）
duration        = frameCount / frameRate
loopMode        = 'repeat'
smoothness      = 1
interpolation   = 'spline'
```

这套参数与编辑器时间轴的对应关系：

- 编辑器 `timeline.frames = json.length`，播放时 `time % frames`；
- 迁移包 `duration * frameRate = frameCount`，viewer 游标走满后回绕到 0；
- 两边都用 `fromPointsLooping(durationFrames, times, points, smoothness)`，
  连“最后一帧到第一帧”的收尾段也一致。

播放控制沿用 viewer 已有的状态与接口：

- 播放：`viewer.state.animationPaused = false`；
- 暂停：`viewer.state.animationPaused = true`；
- 按秒定位：`viewer.seek(seconds)`；
- 按帧定位：先算 `seconds = frame / frameRate` 再 `seek`；
- 总时长：`viewer.state.animationDuration`；
- 当前时间：`viewer.state.animationTime`。

导入后 `state.hasAnimation` 会变成 `true`，viewer 自带的播放按钮与进度条随之出现。

---

## 6. 迁移包内容

```text
migration/supersplat-viewer-json-timeline/
├── README.md                         本文
├── src/
│   ├── camera-json-core.ts           纯数据转换（无依赖）
│   ├── camera-json-import.ts         viewer 运行时接入
│   └── index.ts                      入口
├── patches/
│   ├── camera-manager.patch.md       CameraManager 补丁说明（唯一需要改 viewer 的文件）
│   └── camera-manager-1.42.1.diff    针对 viewer 1.42.1 的 unified diff
├── examples/
│   └── import-camera-json.ts         文件选择 + 拖拽导入示例
└── test/
    ├── build.ps1                     编译核心与编辑器样条
    └── verify.mjs                    数值等价验证（56 项）
```

`camera-json-core.ts` 不 import `playcanvas`，也不碰 DOM。它可以在浏览器、Node、
Worker 里运行，方便做离线转换或在构建期预生成 settings。

---

## 7. 集成方式

### 7.1 方案 A：启动前注入 settings

适合“轨道在页面打开前就确定”的项目。把 JSON 解析结果合并进 v2 settings，
再交给 `createViewer`：

```ts
import { createViewer } from '@playcanvas/supersplat-viewer';
import { parseCamerasJson, withViewerCameraSettings } from './camera-json-core';

const json = await fetch('./cameras.json').then(r => r.json());
const parsed = parseCamerasJson(json);

const settings = withViewerCameraSettings(baseSettings, parsed, {
    frameRate: 30
});

const viewer = await createViewer({
    container: document.getElementById('viewer')!,
    contentUrl: './scene.ply',
    settings
});
```

`withViewerCameraSettings` 会覆盖三块字段：

- `animTracks`：只放这一条轨道；
- `cameras`：把第一帧作为 `initial`，影响启动位置和“重置视角”；
- `startMode`：设为 `'animTrack'`，让 `CameraManager` 创建 `AnimController`。

这个方案不需要改 viewer 源码，但每次换 JSON 都要重新创建 viewer 或刷新页面。
它还要求 `baseSettings` 是 v2（`version: 2`）；v1 的 `animTrack.times` 单位是秒，
迁移包会拒绝合并，避免单位混用。若希望启动时停在第一帧，给 `createViewer` 传
`noanim: true`，或在创建后设 `viewer.state.animationPaused = true`。

### 7.2 方案 B：运行时导入（推荐）

整个接入分三步完成。

**第一步，应用补丁。** 参考版本（1.42.1）可以直接执行：

```powershell
git apply migration/supersplat-viewer-json-timeline/patches/camera-manager-1.42.1.diff
```

版本不同或已有二次修改时，按 `patches/camera-manager.patch.md` 手动插入
`setAnimation` 方法与 `cameraAnimation:set` 事件。只需改 `src/camera-manager.ts`
这一个文件。

**第二步，拷贝源码。** 把 `src/camera-json-core.ts` 与 `src/camera-json-import.ts`
放进目标项目，例如 `src/camera-json/`。它们不依赖 viewer 内部路径，可以放在任意位置。

**第三步，在 viewer 加载完成后调用。** `state.loaded` 为 `true` 之后再导入：

```ts
import { importCameraJsonFromFile } from './camera-json/camera-json-import';

viewer.events.on('loaded:changed', async (loaded: boolean) => {
    if (!loaded) return;

    const file = await pickFile(); // 任意来源的 File / Blob
    const result = await importCameraJsonFromFile(viewer, file, {
        frameRate: 30,
        autoplay: true
    });

    console.log('轨道时长', result.track.duration, '秒');
    console.log('跳过的记录', result.warnings);
});
```

如果宿主能拿到 `CameraManager`，也可以直接调用：

```ts
viewer.cameraManager.setAnimation(track, true);
```

没有 `setAnimation` 时，模块会退回事件：

```ts
viewer.events.fire('cameraAnimation:set', track, true);
```

`importCameraJson` 还会返回可直接复用的 `settingsPatch`，方便把当前轨道写进
发布用的 settings。

### 7.3 已有 UI 的接入点

`examples/import-camera-json.ts` 给出一个不依赖框架的按钮 + 拖拽实现。
如果目标项目已有菜单或工具栏，只保留下面这一句即可：

```ts
await importCameraJsonFromFile(viewer, file, { frameRate: 30, autoplay: true });
```

不支持 `File` 的来源（例如后端直接给 JSON 对象）改用：

```ts
importCameraJson(viewer, jsonObject, { frameRate: 30, autoplay: true });
```

---

## 8. 限制与注意事项

- **roll 会丢失。** cameras.json 的旋转矩阵可能带 roll，viewer 的动画相机是
  `look(position, target)`，只能表达朝向，不能表达绕视线的旋转。需要 roll 时，
  目标项目得先扩展相机控制器，不能只靠 AnimTrack。
- **内参与畸变不参与播放。** 迁移包只输出 `fov`，不输出 `fx/fy/cx/cy/k1...`。
  如果目标项目要做 GT 校准渲染或畸变校正，需要另行走编辑器那条
  `rotation + intrinsics` 路径。
- **画布宽高比会改变 fov 的作用轴。** 素材是竖屏、画布是横屏时，
  “较大轴 fov”会从水平轴换到垂直轴，画面与编辑器不一致。让画布宽高比贴近素材，
  或者按目标宽高比重新换算 `fx/fy`，是两种解决办法。
- **导入不会修改“重置视角”。** 除非按补丁文档第 6 节额外更新 `resetCamera`，
  否则点重置仍回到 settings 的 `cameras[0].initial`，不是导入轨道的第一帧。
- **splat 序列与相机动画互斥。** `state.hasSequence` 为 `true` 时时间轴由序列占用，
  补丁会直接返回，导入模块也会抛错。需要先确认内容不是 `.gsaf` 序列。
- **必须在加载完成后导入。** `cameraManager` 在首帧加载链里才创建；过早触发事件会被丢弃。
  模块在 `state.loaded === false` 时直接抛错，宿主应监听 `loaded:changed` 再调用。
- **帧率没有写在 cameras.json 里。** 默认 30 与编辑器一致；如果源素材是按别的帧率抽帧，
  必须显式传 `frameRate`，否则播放速度会变。
- **导入会替换现有相机轨道。** 与编辑器先 `camera.clearPoses` 再 `camera.setPoses` 的行为一致，
  迁移包不会把新 JSON 追加到旧轨道后面。需要多个片段时，先在外部合成一条完整轨道。
- **非法记录会留下帧号空洞。** `frameCount` 仍取输入长度，所以时间轴总长不变，
  但跳过记录的位置没有关键帧，样条会在相邻有效帧之间直接过渡。
- **v1 settings 不支持直接合并。** 方案 A 只处理 v2；老项目要先升级 settings 版本。

---

## 9. 验证

验证脚本把迁移包的输出与编辑器 `src/file-handler.ts` 的参考实现逐条对比，
再用同一份 `CubicSpline` 对比 viewer 侧采样结果。

在本仓库根目录运行：

```powershell
powershell -File migration/supersplat-viewer-json-timeline/test/build.ps1
node migration/supersplat-viewer-json-timeline/test/verify.mjs
```

`build.ps1` 会编译：

- `src/index.ts` 到 `test/.build/`，包含核心转换与运行时接线；
- 本仓库 `src/anim/spline.ts` 到 `test/.build/spline.js`，作为编辑器样条的参考实现。

验证覆盖 56 项，包括：

- 真实 120 帧样本的 `frame / position / target / fov` 全部一致；
- `AnimTrack` 的长度、`duration`、`times`、数值有限性；
- 0 到 119.75 帧、步长 0.25 的样条采样一致；
- 秒到帧的定位换算；
- settings 补丁与合并、v1 与无 version 的拒绝；
- `fov_x` 优先级、`fx/fy` 取较大轴、60° 回退；
- `id: 0` 排序、单帧轨道、非法记录跳过、非数组输入报错；
- 运行时接线的两条分发路径、未加载与序列占用时报错、`File`/`Blob` 读取。

数值对比之外，还用 Playwright 打开本仓库已构建的编辑器，强制走 fallback 文件输入，
同时导入 `CameraData/05/0630-120849.ply` 与 `CameraData/05/cameras.json`，
再读取 `window.scene.camera.mainCamera.forward`。实测第一帧朝向为
`(0.07673855, 0.13431903, 0.98796234)`，与迁移包 `transformCameraForward` 的结果
点积为 `1.0`。这说明编辑器时间轴最终落到的方向就是迁移包使用的 `+forward` 方向，
不需要再从四元数中间量的符号去反推。

在目标项目里还应做三项人工检查：

1. 导入后画面立即切到 JSON 第一帧，且相机朝向与编辑器时间轴第 0 帧一致；
2. 播放按钮出现，进度条时长等于 `frameCount / frameRate` 秒；
3. 拖动进度条到中间位置，画面与编辑器同一帧一致；
   用 `viewer.seek(frame / frameRate)` 抽查若干帧即可。

---

## 10. 迁移检查表

1. 确认目标项目基于 superSplat-viewer，而不是编辑器；两者的导入入口不同。
2. 应用 `patches/camera-manager.patch.md`，并跑 `npm run type:check`。
3. 拷贝 `camera-json-core.ts` 与 `camera-json-import.ts`，按项目目录调整 import 路径。
4. 确认调用导入时 `viewer.state.loaded === true`。
5. 确认传入的 `frameRate` 与素材抽帧帧率一致。
6. 用一条真实 `cameras.json` 导入，检查第一帧、中间帧、最后一帧。
7. 检查播放、暂停、拖动进度条、`seek` 四种操作都作用于新轨道。
8. 如果项目需要发布 settings，确认 `version: 2` 且 `startMode: 'animTrack'`。
9. 如果项目以后要支持 roll 或畸变，先把相机控制器与投影矩阵的扩展点留出来。

---

## 11. 参考出处

- 编辑器的 JSON 导入：`src/file-handler.ts` 第 161-267 行。
- 编辑器的相机轨道与样条重建：`src/camera-poses.ts` 第 285-468 行。
- 编辑器的时间轴游标与循环：`src/timeline.ts` 第 87-124 行。
- 编辑器加载 PLY 时应用的旋转：`src/asset-loader.ts` 第 25-32 行、
  `src/splat.ts` 第 79-93 行，旋转值来自 splat-transform 的 `Transform`。
- viewer 加载 PLY 时写死的 180°：`src/index.ts` 第 88-100 行。
- viewer 选择动画轨道：`src/camera-manager.ts` 第 131-157 行。
- viewer 构建样条：`src/animation/anim-state.ts` 第 48-77 行。
- viewer 的循环游标：`src/animation/anim-cursor.ts`。
- viewer 的样条实现：`src/core/spline.ts`。
- 生成端写旋转矩阵：`tills/gltf_to_cameras_json.py` 第 596-600 行。
- 两个仓库的版本与提交：本仓库 `1a9832a`，viewer `a66496e`（1.42.1）。
