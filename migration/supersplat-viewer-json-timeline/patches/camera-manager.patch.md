# CameraManager 运行时替换动画轨道补丁

这个补丁只改 `src/camera-manager.ts` 一个文件，给 `CameraManager` 增加：

- 一个公开方法 `setAnimation(track, autoplay?)`；
- 一个全局事件 `cameraAnimation:set`，参数 `(track, autoplay?)`。

`camera-json-import.ts` 会优先调用 `viewer.cameraManager.setAnimation`，不存在时再
`viewer.events.fire('cameraAnimation:set', track, autoplay)`。因此只要应用本补丁，
宿主不需要拿到 `CameraManager` 实例。

参考版本：`@playcanvas/supersplat-viewer` 1.42.1
（commit `a66496e647c58f34bdded4e104adbe4ac52fc423`）。其他 fork 只要
`camera-manager.ts` 的结构相近，按下面的位置插入即可。

如果目标就是 1.42.1，可以直接用同目录的 `camera-manager-1.42.1.diff`：

```powershell
git apply migration/supersplat-viewer-json-timeline/patches/camera-manager-1.42.1.diff
```

---

## 1. 引入 AnimTrack 类型

文件顶部原有：

```ts
import type { Annotation } from './settings';
```

改成：

```ts
import type { Annotation, AnimTrack } from './settings';
```

## 2. 在类字段区声明方法

在 `CameraManager` 的字段声明区（`setCollision` 附近）加：

```ts
    /**
     * 运行时替换相机动画轨道（例如导入 cameras.json 之后），并可选立即播放。
     * 传 null 表示移除动画。要求 viewer 已加载；splat 序列占用时间轴时不做任何事。
     */
    setAnimation: (track: AnimTrack | null, autoplay?: boolean) => void;
```

## 3. 在构造函数里实现

把下面这段放在 `this.seek = (time) => { ... };` 之后、`events.on('pick', ...)` 之前。
它要位于 `controllers`、`target`、`transitionTimer`、`walkAllowed` 都已完成初始化的位置。

```ts
        // 运行时替换相机动画（cameras.json 导入）。
        // 放在 seek 之后：这里要用到 controllers / target / transitionTimer。
        this.setAnimation = (track: AnimTrack | null, autoplay = true) => {
            // 可播放的 splat 序列独占时间轴，相机动画不能同时存在
            if (state.hasSequence) {
                return;
            }

            controllers.anim = track ? new AnimController(track) : null;
            const anim = controllers.anim;

            state.hasAnimation = !!anim;
            state.animationDuration = anim ? anim.animState.cursor.duration : 0;
            state.animationTime = 0;

            if (!anim) {
                // 移除动画：把 anim 模式退回一个可用的相机模式
                if (state.cameraMode === 'anim') {
                    const fallback = isObjectExperience ? 'orbit' : walkAllowed ? 'walk' : 'fly';
                    state.cameraMode = fallback;
                    // cameraMode:changed 会因 prev 的 anim 控制器已被清空而提前返回，
                    // 所以这里手动进入回退控制器并直接落位
                    getController(fallback).onEnter(this.camera);
                    target.copy(this.camera);
                    transitionTimer = 1;
                }
                global.app.renderNextFrame = true;
                return;
            }

            // 把新轨道停在第一帧
            anim.animState.cursor.value = 0;
            anim.animState.update(0);

            if (state.cameraMode !== 'anim') {
                // 这一行会触发 cameraMode:changed，进入 AnimController 并启动一次过渡；
                // 下面再把过渡时长清零，直接落在新轨道的第一帧。
                state.cameraMode = 'anim';
            }

            anim.onEnter(this.camera);
            target.copy(this.camera);
            transitionTimer = 1;

            state.animationPaused = !autoplay;
            global.app.renderNextFrame = true;
        };

        events.on('cameraAnimation:set', (track: AnimTrack | null, autoplay = true) => {
            this.setAnimation(track, autoplay);
        });
```

## 4. 为什么这样写

- `state.hasAnimation` 决定播放按钮和进度条是否出现；改它会自动触发
  `hasAnimation:changed`，`ui/playback.ts` 会刷新控件。
- `state.animationDuration` 是秒，直接取 `AnimState` 的 cursor duration。
- `state.animationPaused = !autoplay` 让导入后自动播放；viewer 原本的
  播放/暂停按钮、`viewer.seek(seconds)`、`captureFrame({ time })` 都会作用到新轨道。
- `transitionTimer = 1` 跳过 1 秒的相机过渡，避免导入后从旧视角慢慢飞过去。
- `anim.animState.update(0)` 先把 position/target/fov 从样条取出来，保证第一帧就正确。
- 没有改 `resetCamera`：按“重置视角”仍会回到 settings 里的初始相机，而不是导入轨道的第一帧。
  如果需要让重置也回到导入轨道，追加见第 6 节。

## 5. 不改 viewer.ts 也能用

`ViewerHandle.events` 是公开的，所以宿主可以直接：

```ts
viewer.events.fire('cameraAnimation:set', track, true);
```

如果希望宿主 API 更直接，可以再给 `ViewerHandle` 加一个方法（可选）：

```ts
// src/types.ts
importCameraTrack(track: AnimTrack | null, autoplay?: boolean): void;

// src/viewer.ts
importCameraTrack(track: AnimTrack | null, autoplay = true): void {
    this.requireLoaded('importCameraTrack');
    this.cameraManager.setAnimation(track, autoplay);
}
```

## 6. 可选：让“重置视角”回到导入轨道第一帧

`resetCamera` 在构造函数里是 `const`，要替换需要把它改成 `let`，并让
`setAnimation` 同时更新它。不是必需项；不做也不影响导入和播放。

```ts
// 构造函数里
let resetCamera = camera0
    ? createCamera(new Vec3(camera0.position), new Vec3(camera0.target), camera0.fov)
    : frameCamera;

// setAnimation 里，更新 initial 后
const first = new Camera();
first.look(new Vec3(track.keyframes.values.position[0], ...),
           new Vec3(track.keyframes.values.target[0], ...));
first.fov = track.keyframes.values.fov[0];
resetCamera = first;
```

---

## 应用检查

1. `npm run type:check` 通过；
2. 页面加载后 `viewer.events.fire('cameraAnimation:set', track, true)` 能立即看到相机切到轨道第一帧；
3. 播放按钮出现，进度条时长等于 `track.duration`；
4. `viewer.seek(2)` 后画面与第 `2 * track.frameRate` 帧一致。
