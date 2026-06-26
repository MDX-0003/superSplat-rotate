# 迁移文档：cameras.json 完整内参与畸变校正

> **源**：`supersplat_origin` commit `b8c2032` (lidaping, 2026-06-06)  
> **目标**：`supersplat` (当前 main 分支)  
> **目标功能**：导入 cameras.json 时携带畸变参数 (k1-k6, p1-p2)，渲染管线中加入畸变校正 pass

---

## 架构差异速览

两套内参系统不能直接合并——它们在**相机投影的实现方式**上选择了不同方案。

| 维度 | supersplat_origin (daping) | supersplat (当前目标) |
|------|---------------------------|----------------------|
| 投影控制 | 修改 `camera.fov` + `horizontalFov` + `aspectRatio` | 自定义 `camera.calculateProjection` 回调 |
| 内参存储 | `Camera._intrinsics` + `Camera._useIntrinsics` | `Camera.poseIntrinsics` (直接 public) |
| 内参范围 | 完整：fx/fy/cx/cy/width/height + k1-k6/p1-p2 | 简化：width/height/fx/fy 四项 |
| 畸变校正 | WebGL distortion pass | 无 |
| 应用入口 | `camera.intrinsics` setter → `updateFovFromIntrinsics()` | `setCalibratedPose()` → `applyTargetSizeForCurrentMode()` |

**迁移策略**：保持目标仓库现有的 `setCalibratedPose` + `calculateProjection` 架构不动，**仅追加畸变参数传递链 + distortion render pass**。不引入 `updateFovFromIntrinsics` 方案（会和现有 `calculateProjection` 冲突）。

---

## 文件级别变更地图

```
新增文件：
  src/shaders/distortion-shader.ts     ← 从 origin 拷贝

修改文件（按依赖序）：
  src/camera-poses.ts     ← 扩展 Pose.intrinsics 类型，保留现有逻辑
  src/file-handler.ts     ← 扩展 cameras.json 解析，提取畸变参数
  src/camera.ts           ← 新增 distortionPass，补全 intrinsics 传递
  src/editor.ts           ← 转交畸变参数（最小改动）
```

---

## 阶段 1：扩展内参类型定义

### 1a. `src/camera-poses.ts` — Pose.intrinsics 类型扩展

**为什么这样做？**  
现有 Pose 的 `intrinsics` 字段只存储 `{width, height, fx, fy}`，缺少畸变参数和主点偏移。扩类型是第一步——后面的文件传递链、shader 消费都依赖这个新结构。

**怎么做：**

1. 新增 `CameraIntrinsics` 接口（文件顶部，Pose 类型之前）
2. 将 Pose.intrinsics 的类型从内联改为引用新接口

```diff
// src/camera-poses.ts (约第7行附近，Pose 类型之前)

+export interface CameraIntrinsics {
+    width: number;
+    height: number;
+    fx: number;
+    fy: number;
+    cx: number;
+    cy: number;
+    k1: number;
+    k2: number;
+    k3: number;
+    p1: number;
+    p2: number;
+}

 type Pose = {
     name: string,
@@ -14,8 +26,8 @@ type Pose = {
     rotation?: Quat,
-    intrinsics?: {
-        width: number,
-        height: number,
-        fx: number,
-        fy: number
-    }
+    intrinsics?: CameraIntrinsics
 };
```

> **Pitfall**：`cx`/`cy` 是从 origin 引入的新字段。目标仓库现有的 `setCalibratedPose` 不消费 cx/cy（它通过 `calculateProjection` 直接设投影矩阵），但畸变 shader 需要它们。所以必须加上，且默认值应是图像中心。

3. 在 `getPoseIntrinsics` 辅助函数中补全默认值（`rebuildSpline` 附近，约 262 行）：

```diff
 const getPoseIntrinsics = (pose: Pose) => {
     if (pose.intrinsics) {
-        return pose.intrinsics;
+        return {
+            width: pose.intrinsics.width,
+            height: pose.intrinsics.height,
+            fx: pose.intrinsics.fx,
+            fy: pose.intrinsics.fy,
+            cx: pose.intrinsics.cx ?? pose.intrinsics.width / 2,
+            cy: pose.intrinsics.cy ?? pose.intrinsics.height / 2,
+            k1: pose.intrinsics.k1 ?? 0,
+            k2: pose.intrinsics.k2 ?? 0,
+            k3: pose.intrinsics.k3 ?? 0,
+            p1: pose.intrinsics.p1 ?? 0,
+            p2: pose.intrinsics.p2 ?? 0,
+        };
     }

     const fov = pose.fov ?? fallbackFov;
     const focal = Math.max(fallbackTargetSize.width, fallbackTargetSize.height) /
         (2 * Math.tan(fov * Math.PI / 360));

     return {
         width: fallbackTargetSize.width,
         height: fallbackTargetSize.height,
         fx: focal,
-        fy: focal
+        fy: focal,
+        cx: fallbackTargetSize.width / 2,
+        cy: fallbackTargetSize.height / 2,
+        k1: 0,
+        k2: 0,
+        k3: 0,
+        p1: 0,
+        p2: 0,
     };
 };
```

> **为什么要补全回退值？** spline 路径在 camera 未移动时不经过 `camera.setPose` 事件，而是走 spline interpolate → `camera.setPose`。回退值保证下游代码不崩溃。`k1-k3/p1-p2` 全部为 0 意味着"无畸变"，shader 渲染结果等同于 passthrough。

4. 检查 spline evaluate 的 segment 查找逻辑（约 303 行附近）——当前代码已通过 `findSegment` 返回 `a`/`b` 两个关键帧，然后在 `onTimelineChange` 中根据 `t` 选取最近的那个传递 intrinsics。**现有逻辑无需修改**，因为 intrinsics 跟随 pose 对象走，只需要确认 `pose.intrinsics` 的赋值路径完整。

> **代码位置验证**：[camera-poses.ts:303-340](src/camera-poses.ts#L303-L340) 中 `findSegment` 和 `onTimelineChange` 已经通过 `segment.a.intrinsics` / `segment.b.intrinsics` 传递内参。

---

## 阶段 2：扩展 cameras.json 导入逻辑

### 2a. `src/file-handler.ts` — 提取畸变参数

**为什么这样做？**  
当前导入只取 fx/fy/width/height，畸变参数被丢弃。需要补上提取逻辑，同时兼容两种 JSON 格式。

**怎么做：** 在 `loadCameraPoses` 函数的内参提取部分（约 222 行），扩展 intrinsics 对象：

```diff
// src/file-handler.ts, loadCameraPoses 函数 (~L222)

                 intrinsics: pose.fx && pose.fy && pose.width && pose.height ? {
                     width: pose.width,
                     height: pose.height,
                     fx: pose.fx,
-                    fy: pose.fy
+                    fy: pose.fy,
+                    cx: pose.cx ?? pose.width / 2,
+                    cy: pose.cy ?? pose.height / 2,
+                    k1: pose.k1 ?? pose.distortion?.[0] ?? 0,
+                    k2: pose.k2 ?? pose.distortion?.[1] ?? 0,
+                    k3: pose.k3 ?? 0,
+                    p1: pose.p1 ?? pose.distortion?.[3] ?? 0,
+                    p2: pose.p2 ?? pose.distortion?.[4] ?? 0,
                 } : undefined
```

> **关于 `pose.distortion` 兼容**：COLMAP 的 cameras.json 畸变参数在 `distortion` 数组中，索引 0-4 分别对应 k1/k2/k3/p1/p2。但我们优先取直接字段（如果有），数组格式作为 fallback。`k3` 在 COLMAP SIMPLE_RADIAL 模型中不存在，默认为 0。

---

## 阶段 3：添加畸变校正渲染通道

### 3a. `src/shaders/distortion-shader.ts` — 新建文件

**为什么这样做？**  
畸变校正是全屏后处理——将渲染结果作为纹理，每个像素根据畸变模型重新采样。这需要一个 WebGL 全屏四边形 shader。

**怎么做：** 直接从 origin 拷贝文件内容。这个 shader 实现了 OpenCV 标准的畸变模型：

- **径向畸变**：`x' = x * (1 + k1*r² + k2*r⁴ + k3*r⁶)` （枕形/桶形）
- **切向畸变**：`x' += 2*p1*x*y + p2*(r² + 2x²)` （镜头与传感器不平行）
- **边界外像素**：返回透明黑色

> **Pitfall**：shader 中的采样方向是 **校正方向**（从校正后坐标反推原始像素）。如果渲染结果是"已畸变"的，那么 shader 的效果就是去畸变。确认：3D Gaussian Splatting 渲染的结果是针孔相机投影（无畸变），而 cameras.json 记录的是真实相机的畸变参数。所以我们要做的是**对渲染结果施加畸变**以匹配照片，还是**校正渲染结果**？答案取决于使用场景。当前 origin 代码做的是校正方向（从 `(fx, fy, cx, cy)` 计算校正后 UV → 反推畸变前 UV → 采样），这是为了让 3DGS 渲染匹配已校正的照片。**保持和 origin 一致。**

### 3b. `src/camera.ts` — 添加 distortionPass

**为什么这样做？**  
畸变 shader 需要一个渲染 pass 来执行。它在 main pass（3D 场景渲染）之后、final pass（输出到屏幕）之前，读取 mainTarget 的颜色缓冲，输出畸变校正后的画面到 workTarget。

**怎么做：** 分 5 小步。

**Step 1** — 导入 distortion shader（文件顶部 import 区域，约 35 行附近）：

```diff
 import { vertexShader, fragmentShader } from './shaders/camera-blit-shader';
+import { distortionVertexShader, distortionFragmentShader } from './shaders/distortion-shader';
```

**Step 2** — 声明 distortionPass 属性（约 103 行，`finalPass` 声明之后）：

```diff
     finalPass: SimpleRenderPass;
+    distortionPass: SimpleRenderPass;
```

**Step 3** — 创建 distortionPass（在 `add()` 方法中，finalPass 创建之前，约 351 行）：

```diff
+        this.distortionPass = new SimpleRenderPass(device,
+            new ShaderQuad(device, distortionVertexShader, distortionFragmentShader, 'distortion'), {
+                vars: () => {
+                    const intrinsics = this.poseIntrinsics;
+                    if (!intrinsics) {
+                        return {
+                            srcTexture: this.mainTarget.colorBuffer,
+                            resolution: [this.targetSize.width, this.targetSize.height],
+                            fx: 1,
+                            fy: 1,
+                            cx: this.targetSize.width / 2,
+                            cy: this.targetSize.height / 2,
+                            k1: 0, k2: 0, k3: 0, p1: 0, p2: 0,
+                        };
+                    }
+                    const hasDistortion = intrinsics.k1 !== 0 || intrinsics.k2 !== 0 || intrinsics.k3 !== 0 ||
+                        intrinsics.p1 !== 0 || intrinsics.p2 !== 0;
+                    return {
+                        srcTexture: this.mainTarget.colorBuffer,
+                        resolution: [intrinsics.width, intrinsics.height],
+                        fx: intrinsics.fx,
+                        fy: intrinsics.fy,
+                        cx: intrinsics.cx,
+                        cy: intrinsics.cy,
+                        k1: hasDistortion ? intrinsics.k1 : 0,
+                        k2: hasDistortion ? intrinsics.k2 : 0,
+                        k3: hasDistortion ? intrinsics.k3 : 0,
+                        p1: hasDistortion ? intrinsics.p1 : 0,
+                        p2: hasDistortion ? intrinsics.p2 : 0,
+                    };
+                }
+            });

         this.finalPass = new SimpleRenderPass(device,
             new ShaderQuad(device, vertexShader, fragmentShader, 'final-blit'), {
```

> **为什么 distortionPass 在 finalPass 之前创建？** PlayCanvas 的 render passes 按数组顺序执行。distortionPass 读取 mainTarget，输出到 workTarget，然后 finalPass 可以从 workTarget 读取。所以管线是：`clear → main(3D) → splat → gizmo → distortion → final(输出到屏幕)`。

**Step 4** — 修改 finalPass 的 srcTexture，使其能读取 distortion 输出：

```diff
         this.finalPass = new SimpleRenderPass(device,
             new ShaderQuad(device, vertexShader, fragmentShader, 'final-blit'), {
                 vars: () => {
+                    const intrinsics = this.poseIntrinsics;
+                    const hasDistortion = intrinsics &&
+                        (intrinsics.k1 !== 0 || intrinsics.k2 !== 0 || intrinsics.k3 !== 0 ||
+                         intrinsics.p1 !== 0 || intrinsics.p2 !== 0);
                     return {
-                        srcTexture: this.mainTarget.colorBuffer,
+                        srcTexture: hasDistortion && this.distortionPass?.enabled
+                            ? this.workTarget.colorBuffer
+                            : this.mainTarget.colorBuffer,
                         dstSize: [device.width, device.height]
                     };
                 }
             });
```

> **Pitfall**：distortionPass 使用 `workTarget` 作为输出目标。这和 `setTargetSizeOverride`/offscreen 模式使用的 `workTarget` 是同一个。确认没有冲突：offscreen 模式中 `finalPass.enabled = false`（见 `startOffscreenMode`），所以 distortion 的结果不会被 finalPass 覆盖。

**Step 5** — 在 init/render target 设置中配置 distortionPass（`setTargetSizeOverride` 方��附近，约 600 行）：

在现有的 render target 初始化流程中，找到 `this.finalPass.init(null)` 调用（约 620 行），在其附近添加：

```diff
+            this.distortionPass.init(this.workTarget);
             this.finalPass.init(null);
```

以及在 framePasses 数组中插入：

```diff
-            this.camera.framePasses = [this.clearPass, this.mainPass, this.splatPass, this.gizmoPass, this.finalPass];
+            this.camera.framePasses = [this.clearPass, this.mainPass, this.splatPass, this.gizmoPass, this.distortionPass, this.finalPass];
```

**Step 6** — 在 `destroy()` 方法中添加清理：

```diff
     destroy() {
         this.mainPass?.destroy();
         this.splatPass?.destroy();
         this.gizmoPass?.destroy();
+        this.distortionPass?.destroy();
         this.finalPass?.destroy();
```

---

## 阶段 4：补全数据传递链

### 4a. `src/editor.ts` — 确认 intrinsics 传递完整

**为什么不需要大改？** 目标仓库的 `camera.getPose` 和 `camera.setPose` 事件已经传递 intrinsics，且 `setCalibratedPose` 已消费它们。只需要确认畸变参数不会中途丢失。

检查清单：

| 传递环节 | 文件位置 | 状态 |
|---------|---------|------|
| cameras.json → `camera.addImportedPose` | file-handler.ts:215-228 | **需改**（阶段 2a 补畸变参数） |
| addImportedPose → CameraAnimTrack.addPose | camera-poses.ts:207 | ✅ 现有代码已 spread `pose.intrinsics` |
| CameraAnimTrack → spline evaluate → `camera.setPose` | camera-poses.ts:295-340 | ✅ 现有逻辑已传递 `segment.a.intrinsics` |
| `camera.setPose` → `camera.setCalibratedPose` | editor.ts:768-779 | ✅ 现有逻辑已判断 + 调用 |
| `camera.setCalibratedPose` → `Camera.poseIntrinsics` | camera.ts:277-278 | **需改**（见下方） |

### 4b. `src/camera.ts` — 扩展 `setCalibratedPose` 签名

`setCalibratedPose` 当前的 intrinsics 参数类型是 `{width, height, fx, fy}`。需要改为使用 `CameraIntrinsics` 接口：

```diff
-    setCalibratedPose(position: Vec3, target: Vec3, rotation: Quat, intrinsics: { width: number, height: number, fx: number, fy: number }, dampingFactorFactor: number = 1) {
+    setCalibratedPose(position: Vec3, target: Vec3, rotation: Quat, intrinsics: import('./camera-poses').CameraIntrinsics, dampingFactorFactor: number = 1) {
         this.poseIntrinsics = { ...intrinsics };
```

> **Pitfall**：用 `import('./camera-poses')` 的动态类型引用避免循环依赖。更好的做法是从 `camera-poses.ts` 导出 `CameraIntrinsics`，在 `camera.ts` 顶部 import。两者都可行。

---

## 阶段 5：verify 检查清单

完成迁移后，逐项验证：

- [ ] 导入带畸变参数的 cameras.json，View Panel 的 GT Camera 列表能正确显示位姿
- [ ] 点击某个 GT camera 位姿，画面视角正确切换（相机位置 + 方向 + FOV）
- [ ] 有畸变参数的位姿，画面应有可见的畸变校正效果（边缘像素向内/外偏移）
- [ ] 无畸变参数的位姿，画面与迁移前一致（passthrough）
- [ ] "导出全部" 功能正常（每个位姿导出的 PNG 包含畸变校正）
- [ ] "渲染 → 视频" 功能正常
- [ ] 播放动画时畸变参数跟随关键帧切换
- [ ] `timeline.time` 在关键帧之间插值时，distortion pass 使用的是最近关键帧的内参（切换不闪烁）

---

## 附录 A：两套投影方案的深度对比

两个仓库都需要把 cameras.json 里的针孔相机参数（fx, fy, cx, cy）变成屏幕上正确的画面。它们选择了完全不同的路线来实现这个目标。

### 先理解 PlayCanvas 默认的投影管线

正常情况下，PlayCanvas 透视相机的投影矩阵由 3 个参数决定：

```
perspectiveMatrix = f(fov, aspectRatio, horizontalFov)
```

- `fov`：竖直方向视场角（度），放在 `camera.fov` 字段
- `aspectRatio`：宽高比，从 `targetSize` 自动计算或手动指定
- `horizontalFov`：bool 标志——为 true 时 `fov` 表示**水平**视场角而非竖直

当你修改 `camera.fov = 50` 时，PlayCanvas 内部调用 `perspectiveMatrix.setFromPerspective(fov, aspectRatio)` 算出投影矩阵。修改 `horizontalFov` 会交换宽高的计算方向。

**关键限制**：PlayCanvas 的 `fov` 字段只有一个值。它假定 **水平 FOV 和竖直 FOV 由同一个角度 + aspectRatio 推导**。但真实的相机内参 fx 和 fy 往往不相等（非正方形像素、变形镜头），对应的水平 FOV 和竖直 FOV 是两个独立值——这和 PlayCanvas 的 "一个 FOV 管两个方向" 的设计根本矛盾。

---

### Origin 方案：偷梁换柱（修改 FOV 参数）

origin 的做法是**骗过 PlayCanvas**——用内参算出 FOV，填入 PlayCanvas 的标准字段，让它按默认逻辑继续算投影矩阵。

**做法**（`camera.intrinsics` setter → `updateFovFromIntrinsics()`）：

```typescript
// 从 fx/fy 算出两个方向的 FOV
const fovX = 2 * Math.atan(width / 2 / fx) * (180 / PI);   // 水平 FOV
const fovY = 2 * Math.atan(height / 2 / fy) * (180 / PI);  // 竖直 FOV

// 取大的那个作为 camera.fov
this.fov = Math.max(fovX, fovY);

// 如果水平 FOV 更大，把 fov 标记为 "水平方向"
this.camera.horizontalFov = fovX > fovY;

// 锁定宽高比为原始图像的宽高比
this.camera.aspectRatio = width / height;
```

**它做了什么**：比较 fx 对应的水平 FOV 和 fy 对应的竖直 FOV，**取较大的那个**赋给 `camera.fov`。如果水平 FOV 更大，就设置 `horizontalFov = true`（告诉 PlayCanvas "这个角度是水平方向的，竖直方向从 aspectRatio 反推"），反之用默认的竖直方向模式。

**问题**：

```
假设内参：fx=1200, fy=1000, width=1920, height=1080

fovX = 2 * atan(1920 / 2400) = 77.3°
fovY = 2 * atan(1080 / 2000) = 56.7°

max = 77.3° (fovX 更大)
horizontalFov = true
aspectRatio = 1920/1080 = 1.778

PlayCanvas 用 "水平 FOV = 77.3°" + "16:9" 反推竖直 FOV =
  竖直 FOV = 2 * atan(tan(77.3°/2) / 1.778) = 49.8°
```

实际竖直 FOV 应该是 56.7°，PlayCanvas 算出来的是 49.8°。**竖直方向被压缩了约 12%**。这就是 `Math.max()` 取最大值的代价——另一个轴向的 FOV 是错的。

只有 `fx === fy`（正方形像素）时这个方案才精确。真实相机几乎不会出现这种情况。

**其他问题**：

| 问题 | 说明 |
|------|------|
| **无 cx/cy 支持** | `updateFovFromIntrinsics` 完全忽略 cx/cy。如果主点不在图像中心（相机标定后常有偏移），画面会整体平移——这个方案无法表达。 |
| **分辨率不独立** | aspectRatio 被锁死为原始图像宽高比。offscreen 渲染到不同分辨率（如导出 4K）时宽高比必不匹配，画面会被拉伸。 |
| **退不出** | `_useIntrinsics = false` 后 FOV/horizontalFov/aspectRatio 的修改值仍残留，下一个 orbit 操作基于错误的参数继续。 |

---

### Target 方案：另起炉灶（接管投影矩阵）

目标仓库的做法是**绕过 PlayCanvas 的 FOV 体系**——用 `camera.calculateProjection` 回调直接算出投影矩阵，PlayCanvas 每帧调用这个回调来获取矩阵，不再用 `fov` / `aspectRatio` / `horizontalFov` 来推导。

**做法**（`camera.onUpdate()` → `calculateProjection`）：

```typescript
if (this.poseIntrinsics) {
    // 将 fx/fy 缩放到当前渲染目标尺寸
    const fx = source.fx * targetWidth / source.width;
    const fy = source.fy * targetHeight / source.height;

    camera.calculateProjection = (matrix: Mat4) => {
        // 用针孔相机模型直接构建视锥体
        const right = width * 0.5 / fx * near;
        const left = -right;
        const top = height * 0.5 / fy * near;
        const bottom = -top;
        matrix.setFrustum(left, right, bottom, top, near, far);
    };
}
```

**它做了什么**：把 fx/fy 按比例缩放到当前 render target 的大小（`fx * targetWidth / sourceWidth`），然后以**对称视锥体**的方式构建透视矩阵——`left = -right`, `top = -bottom`。

**优势**：

| 优势 | 说明 |
|------|------|
| **水平/竖直 FOV 同时精确** | `left/right` 由 fx 独立决定，`top/bottom` 由 fy 独立决定——两个方向完全解耦。不会出现 origin 的"取 max 牺牲另一个"问题。 |
| **分辨率无关** | fx/fy 随 target size 等比缩放。导出 4K、渲染 720p 视频，投影始终正确。 |
| **干净的退出** | `calculateProjection = null` → PlayCanvas 恢复默认行为，不留残留状态。 |
| **FOV 字段不受污染** | orbit 控制的 `camera.fov` 不受影响，用户退出标定视角后可正常操作。 |

**当前限制**：

| 限制 | 说明 |
|------|------|
| **cx/cy 未使用** | `setFrustum(left, right, bottom, top)` 构建的是对称视锥体。形式上可以改成非对称视锥体来支持主点偏移：`left = -cx * near / fx`, `right = (width - cx) * near / fx`。但目前没做。 |
| **origin 也未使用 cx/cy** | 这不是方案劣势——两个方案都还没在投影矩阵中消费 cx/cy。cx/cy 目前仅用于畸变 shader。 |

---

### 同一例子，两种方案的实际数值

```
输入：fx=1200, fy=1000, width=1920, height=1080, near=0.1

真实视锥体（针孔模型）：
  right  = 1920 * 0.5 / 1200 * 0.1 = 0.080
  left   = -0.080
  top    = 1080 * 0.5 / 1000 * 0.1 = 0.054
  bottom = -0.054
  → 水平 FOV = 2*atan(0.080/0.1) = 77.3°
  → 竖直 FOV = 2*atan(0.054/0.1) = 56.7°
  ✅ Target 方案：直接用 (0.080, -0.080, 0.054, -0.054) 构建视锥体 → 完全准确

Origin 方案：
  → fov = max(77.3°, 56.7°) = 77.3°, horizontalFov = true
  → PlayCanvas 反推竖直：2*atan(tan(38.65°)/1.778) ≈ 49.8°
  → 有效视锥体：(0.080, -0.080, 0.046, -0.046)
  ❌ top/bottom 应该是 ±0.054，实际是 ±0.046（竖直方向缩小 15%）
```

---

### 总结对比

```
┌─────────────────────┬──────────────────────────┬───────────────────────────┐
│                     │  Origin (修改FOV)         │  Target (接管投影矩阵)      │
├─────────────────────┼──────────────────────────┼───────────────────────────┤
│ 机制               │ 修改 fov/horizontalFov/   │ camera.calculateProjection │
│                     │ aspectRatio → 欺骗引擎    │ 回调 → 直接构建矩阵        │
├─────────────────────┼──────────────────────────┼───────────────────────────┤
│ fx ≠ fy 时精度      │ ❌ 一个轴被压缩/拉伸      │ ✅ 两个轴独立精确           │
│ fx = fy 时精度      │ ✅                       │ ✅                        │
├─────────────────────┼──────────────────────────┼───────────────────────────┤
│ 分辨率切换          │ ❌ aspectRatio 锁定       │ ✅ fx/fy 等比缩放           │
│                     │   需手动调整               │   自动适配                  │
├─────────────────────┼──────────────────────────┼───────────────────────────┤
│ 退出恢复            │ ❌ 状态残留                │ ✅ calculateProjection=null │
│                     │                           │   即恢复                   │
├─────────────────────┼──────────────────────────┼───────────────────────────┤
│ cx/cy 投影支持      │ ❌ 不支持                 │ ⚠️ 可扩展但未实现           │
├─────────────────────┼──────────────────────────┼───────────────────────────┤
│ 代码侵入度          │ 低（只设3个字段）          │ 低（只替换1个回调）         │
├─────────────────────┼──────────────────────────┼───────────────────────────┤
│ 与畸变 shader 关系   │ 独立，无耦合              │ 独立，无耦合               │
└─────────────────────┴──────────────────────────┴───────────────────────────┘
```

**结论**：两个方案与畸变 pass 的关系都是正交的——畸变 shader 只关心"当前的 fx/fy/cx/cy/k1-k3/p1-p2 是什么"，不关心投影矩阵是怎么算出来的。因此**迁移时保持 Target 的 `calculateProjection` 方案不动，仅扩展内参结构 + 追加 distortion pass**，既获得 Target 方案在投影精度上的优势，又补齐畸变校正能力。
