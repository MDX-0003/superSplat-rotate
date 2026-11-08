/**
 * 数值等价验证：
 *
 *   1. 把本迁移包的核心输出，与 superSplat 编辑器 src/file-handler.ts 里
 *      `loadCameraPoses` 的逻辑逐条对比（使用真实 CameraData/05/cameras.json）；
 *   2. 用编辑器同一份 CubicSpline，对比 viewer 侧 AnimTrack 的采样结果；
 *   3. 覆盖 fov 优先级、排序、单帧、非法记录等边界。
 *
 * 运行：
 *   node migration/supersplat-viewer-json-timeline/test/verify.mjs
 *
 * 前置：test/.build 里已有编译后的 camera-json-core.js 和 spline.js
 * （见同目录 build.ps1 或 README）。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { Mat4, Quat, Vec3 } from 'playcanvas';

import {
    buildCameraAnimTrack,
    buildViewerCameraSettingsPatch,
    makeCameraTarget,
    parseCamerasJson,
    resolveCameraFov,
    transformCameraForward,
    transformCameraPosition,
    withViewerCameraSettings
} from './.build/camera-json-core.js';
import { CubicSpline } from './.build/spline.js';
import { importCameraJson, importCameraJsonFromFile } from './.build/camera-json-import.js';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '../../..');
const samplePath = resolve(repoRoot, 'CameraData/05/cameras.json');

let failures = 0;
let checks = 0;

const fail = (message) => {
    failures++;
    console.error(`  ✗ ${message}`);
};

const ok = (message) => {
    checks++;
    console.log(`  ✓ ${message}`);
};

const assert = (condition, message) => {
    checks++;
    if (!condition) {
        failures++;
        console.error(`  ✗ ${message}`);
    }
};

const close = (a, b, eps = 1e-12) => Math.abs(a - b) <= eps;
const closeVec = (a, b, eps = 1e-12) => a.every((v, i) => close(v, b[i], eps));

// ---------------------------------------------------------------------------
// 编辑器参考实现（逐字照搬 src/file-handler.ts 的相关部分）
// ---------------------------------------------------------------------------

const rotationMat = new Mat4();
const rotationQuat = new Quat();

const transformPlyPosition = p => new Vec3(-p.x, -p.y, p.z);

const transformPlyRotation = (rotation) => {
    const xAxis = new Vec3(-rotation[0][0], -rotation[1][0], rotation[2][0]);
    const yAxis = new Vec3(rotation[0][1], rotation[1][1], -rotation[2][1]);
    const zAxis = new Vec3(-rotation[0][2], -rotation[1][2], rotation[2][2]);

    rotationMat.data.set([
        xAxis.x, xAxis.y, xAxis.z, 0,
        yAxis.x, yAxis.y, yAxis.z, 0,
        zAxis.x, zAxis.y, zAxis.z, 0,
        0, 0, 0, 1
    ]);

    return rotationQuat.setFromMat4(rotationMat).clone();
};

const editorLoadCameraPoses = (json) => {
    const sorter = (a, b) => {
        const avalue = a.id ?? a.img_name?.match(/\d*$/)?.[0];
        const bvalue = b.id ?? b.img_name?.match(/\d*$/)?.[0];
        return (avalue && bvalue) ? parseInt(avalue, 10) - parseInt(bvalue, 10) : 0;
    };

    const poses = [];
    json.sort(sorter).forEach((pose, i) => {
        if (Object.prototype.hasOwnProperty.call(pose, 'position') &&
            Object.prototype.hasOwnProperty.call(pose, 'rotation')) {
            const p = new Vec3(pose.position);
            const position = transformPlyPosition(p);
            const rotation = transformPlyRotation(pose.rotation);
            const z = new Vec3(-pose.rotation[0][2], -pose.rotation[1][2], pose.rotation[2][2]);

            const vec = z.clone().mulScalar(10).add(position);

            let fov = pose.fov_x ?? 60;
            if (pose.fx && pose.fy && pose.width && pose.height) {
                if (pose.fov_x) {
                    fov = pose.fov_x;
                } else {
                    const fovX = 2 * Math.atan(pose.width / (2 * pose.fx)) * (180 / Math.PI);
                    const fovY = 2 * Math.atan(pose.height / (2 * pose.fy)) * (180 / Math.PI);
                    fov = Math.max(fovX, fovY);
                }
            }

            poses.push({
                name: pose.img_name ?? `cameras.json_${i}`,
                frame: i,
                position,
                target: vec,
                fov,
                rotation
            });
        }
    });

    return poses;
};

// ---------------------------------------------------------------------------
// 1. 真实 cameras.json：逐条对比
// ---------------------------------------------------------------------------

console.log('\n[1] 真实 cameras.json 逐条对比（编辑器参考实现 vs 迁移包）');
const raw = JSON.parse(readFileSync(samplePath, 'utf8'));
const referenceInput = structuredClone(raw);
const reference = editorLoadCameraPoses(referenceInput);
const parsed = parseCamerasJson(structuredClone(raw));

assert(parsed.poses.length === reference.length, `位姿数量一致（${parsed.poses.length}）`);
assert(parsed.frameCount === raw.length, `frameCount === 输入长度（${parsed.frameCount}）`);
assert(reference.length === 120, '样本包含 120 帧');

let poseMismatch = 0;
for (let i = 0; i < Math.min(parsed.poses.length, reference.length); i++) {
    const a = parsed.poses[i];
    const b = reference[i];
    const positionB = [b.position.x, b.position.y, b.position.z];
    const targetB = [b.target.x, b.target.y, b.target.z];

    if (a.frame !== b.frame ||
        !closeVec(a.position, positionB) ||
        !closeVec(a.target, targetB) ||
        !close(a.fov, b.fov)) {
        poseMismatch++;
        if (poseMismatch <= 3) {
            console.error('    mismatch', i, a, positionB, targetB, b.fov);
        }
    }
}
assert(poseMismatch === 0, `120 帧的 frame/position/target/fov 全部一致（mismatch=${poseMismatch}）`);

// 抽查第一帧的具体数值
const p0 = parsed.poses[0];
const fwd0 = transformCameraForward(raw[0].rotation);
assert(closeVec(p0.position, transformCameraPosition(raw[0].position)), '第一帧 position = (-x, -y, z)');
assert(closeVec(p0.target, makeCameraTarget(p0.position, fwd0)), '第一帧 target = position + forward * 10');
assert(close(p0.fov, reference[0].fov), `第一帧 fov 一致（${p0.fov.toFixed(6)}°）`);

// ---------------------------------------------------------------------------
// 2. AnimTrack 结构 + 采样等价
// ---------------------------------------------------------------------------

console.log('\n[2] AnimTrack 结构与 viewer 采样等价');
const track = buildCameraAnimTrack(parsed, { frameRate: 30 });
assert(track.frameRate === 30, 'frameRate = 30');
assert(track.loopMode === 'repeat', "loopMode = 'repeat'");
assert(track.interpolation === 'spline', "interpolation = 'spline'");
assert(track.smoothness === 1, 'smoothness = 1');
assert(close(track.duration, 120 / 30), `duration = frameCount / fps = ${track.duration}s`);
assert(track.keyframes.times.length === 120, 'times 长度 = 120');
assert(track.keyframes.values.position.length === 360, 'position 长度 = 360');
assert(track.keyframes.values.target.length === 360, 'target 长度 = 360');
assert(track.keyframes.values.fov.length === 120, 'fov 长度 = 120');
assert(track.keyframes.times.every((t, i) => t === i), 'times = 0..119');
assert(track.keyframes.values.position.every(Number.isFinite), 'position 无 NaN/Infinity');
assert(track.keyframes.values.target.every(Number.isFinite), 'target 无 NaN/Infinity');
assert(track.keyframes.values.fov.every(Number.isFinite), 'fov 无 NaN/Infinity');

// viewer AnimState.fromTrack 的 points 组装方式
const points = [];
for (let i = 0; i < track.keyframes.times.length; i++) {
    points.push(
        track.keyframes.values.position[i * 3],
        track.keyframes.values.position[i * 3 + 1],
        track.keyframes.values.position[i * 3 + 2],
        track.keyframes.values.target[i * 3],
        track.keyframes.values.target[i * 3 + 1],
        track.keyframes.values.target[i * 3 + 2],
        track.keyframes.values.fov[i]
    );
}
const extra = track.duration === track.keyframes.times.at(-1) / track.frameRate ? 1 : 0;
const viewerSpline = CubicSpline.fromPointsLooping(
    (track.duration + extra) * track.frameRate,
    track.keyframes.times,
    points,
    track.smoothness
);

// 编辑器 CameraAnimTrack.rebuildSpline 用同样的 times/points 和 timeline.frames=120
const editorSpline = CubicSpline.fromPointsLooping(
    parsed.frameCount,
    track.keyframes.times,
    points,
    track.smoothness
);

let sampleMismatch = 0;
for (let frame = 0; frame <= 119.75; frame += 0.25) {
    const a = [];
    const b = [];
    viewerSpline.evaluate(frame, a);
    editorSpline.evaluate(frame, b);
    if (!closeVec(a, b, 1e-9)) {
        sampleMismatch++;
    }
}
assert(sampleMismatch === 0, '0..119.75 帧采样结果与编辑器样条一致');

// 秒 -> 帧的 seek 映射
const seekResults = [];
for (const seconds of [0, 0.5, 1.25, 2, 3.99]) {
    const result = [];
    viewerSpline.evaluate(seconds * track.frameRate, result);
    seekResults.push({ seconds, frame: seconds * track.frameRate, position: result.slice(0, 3) });
}
assert(seekResults[2].frame === 37.5, 'seek(1.25s) 映射到第 37.5 帧');
assert(seekResults.every(r => r.position.every(Number.isFinite)), 'seek 采样无非法值');

// ---------------------------------------------------------------------------
// 3. settings patch
// ---------------------------------------------------------------------------

console.log('\n[3] settings 注入补丁');
const patch = buildViewerCameraSettingsPatch(parsed, { frameRate: 30 });
assert(patch.startMode === 'animTrack', "startMode = 'animTrack'");
assert(
    patch.animTracks.length === 1 &&
    JSON.stringify(patch.animTracks[0]) === JSON.stringify(track),
    'patch 生成与 track 等价的内容'
);
assert(patch.cameras[0].initial.position.length === 3, 'initial.position 是三元组');
assert(close(patch.cameras[0].initial.fov, parsed.poses[0].fov), 'initial.fov 取自第一帧');

const merged = withViewerCameraSettings({ version: 2, background: { color: [0, 0, 0] } }, parsed, { frameRate: 30 });
assert(merged.version === 2 && merged.animTracks.length === 1, '合并 v2 settings 成功');
assert(merged.background.color[0] === 0, '保留原有字段');

let threw = false;
try {
    withViewerCameraSettings({ version: 1 }, parsed);
} catch {
    threw = true;
}
assert(threw, '拒绝 v1 settings（避免 times 秒/帧混用）');

let missingVersionThrew = false;
try {
    withViewerCameraSettings({ background: { color: [0, 0, 0] } }, parsed);
} catch {
    missingVersionThrew = true;
}
assert(missingVersionThrew, '拒绝没有 version 的 v1 settings');

// ---------------------------------------------------------------------------
// 4. 边界
// ---------------------------------------------------------------------------

console.log('\n[4] 边界与解析规则');

const identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];

// fov_x 优先
assert(resolveCameraFov({ fov_x: 90, fx: 1000, fy: 1000, width: 1920, height: 1080 }) === 90,
    'fov_x 优先于 fx/fy');

// fx/fy 取较大轴
const fovFromIntrinsics = resolveCameraFov({ fx: 1000, fy: 1000, width: 1920, height: 1080 });
const expectedFovX = 2 * Math.atan(1920 / 2000) * 180 / Math.PI;
assert(close(fovFromIntrinsics, expectedFovX, 1e-12), 'fx/fy 计算取水平较大 fov');

// 无内参 -> 60
assert(resolveCameraFov({}) === 60, '无内参回退 60°');

// id=0 排序
const unsorted = [
    { id: 2, position: [0, 0, 0], rotation: identity },
    { id: 0, position: [0, 0, 0], rotation: identity },
    { id: 1, position: [0, 0, 0], rotation: identity }
];
const sorted = parseCamerasJson(unsorted);
assert(sorted.poses.map(p => p.frame).join(',') === '0,1,2', 'id=0 参与排序');
assert(sorted.poses.map(p => p.name).join(',') === 'camera_0,camera_1,camera_2', '无 img_name 时生成名字');
assert(sorted.poses[0].target.every(Number.isFinite), '单位旋转生成合法 target');

// 单帧
const single = parseCamerasJson([{ id: 0, img_name: 'only', position: [1, 2, 3], rotation: identity }]);
const singleTrack = buildCameraAnimTrack(single, { frameRate: 30 });
assert(singleTrack.keyframes.times.length === 1, '单帧也能构建 track');
assert(close(singleTrack.duration, 1 / 30), '单帧 duration = 1/fps');

// 非法记录
const mixed = parseCamerasJson([
    { id: 0, position: [0, 0, 0], rotation: identity },
    { id: 1, position: 'bad', rotation: identity },
    { id: 2, position: [1, 2, 3], rotation: [[1, 0], [0, 1]] },
    { id: 3, position: [4, 5, 6], rotation: identity }
]);
assert(mixed.poses.length === 2, '跳过 2 条非法记录');
assert(mixed.frameCount === 4, 'frameCount 仍为输入长度');
assert(mixed.warnings.length === 2, '产生 2 条 warning');
assert(mixed.poses.map(p => p.frame).join(',') === '0,3', '跳过记录仍占用 frame 下标');

// 非对象记录
const withHole = parseCamerasJson([
    null,
    { id: 1, position: [0, 0, 0], rotation: identity },
    { id: 2, position: [1, 1, 1], rotation: identity }
]);
assert(withHole.poses.map(p => p.frame).join(',') === '1,2', '非对象记录占用 frame 下标');
assert(withHole.warnings.length === 1, '非对象记录产生 warning');

// 非数组
let typeThrew = false;
try {
    parseCamerasJson({});
} catch (error) {
    typeThrew = error instanceof TypeError;
}
assert(typeThrew, '非数组输入抛 TypeError');

// ---------------------------------------------------------------------------
// 5. import glue 分发路径
// ---------------------------------------------------------------------------

console.log('\n[5] import glue 分发路径');

const makeFakeViewer = ({ loaded = true, hasSequence = false, manager = false } = {}) => {
    const calls = [];
    const viewer = {
        state: { loaded, hasSequence },
        events: {
            fire: (...args) => {
                calls.push(args);
            }
        }
    };
    if (manager) {
        viewer.cameraManager = {
            setAnimation: (track, autoplay) => {
                calls.push(['setAnimation', track, autoplay]);
            }
        };
    }
    return { viewer, calls };
};

const eventFake = makeFakeViewer();
const eventResult = importCameraJson(eventFake.viewer, structuredClone(raw), { frameRate: 30, autoplay: false });
assert(eventResult.dispatchedVia === 'events:cameraAnimation:set', '无 cameraManager 时走事件');
assert(eventFake.calls.length === 1 && eventFake.calls[0][0] === 'cameraAnimation:set', '事件名正确');
assert(eventFake.calls[0][1] === eventResult.track, '事件收到同一个 track 实例');
assert(eventFake.calls[0][2] === false, 'autoplay=false 透传');

const managerFake = makeFakeViewer({ manager: true });
const managerResult = importCameraJson(managerFake.viewer, structuredClone(raw), { frameRate: 30 });
assert(managerResult.dispatchedVia === 'cameraManager.setAnimation', '有 cameraManager 时直接调用方法');
assert(managerFake.calls[0][0] === 'setAnimation' && managerFake.calls[0][2] === true, '方法路径默认自动播放');

let notLoadedThrew = false;
try {
    importCameraJson(makeFakeViewer({ loaded: false }).viewer, structuredClone(raw));
} catch {
    notLoadedThrew = true;
}
assert(notLoadedThrew, '未 loaded 时抛错');

let sequenceThrew = false;
try {
    importCameraJson(makeFakeViewer({ hasSequence: true }).viewer, structuredClone(raw));
} catch {
    sequenceThrew = true;
}
assert(sequenceThrew, 'hasSequence 时抛错');

let noDispatchThrew = false;
try {
    importCameraJson({ state: { loaded: true } }, structuredClone(raw));
} catch {
    noDispatchThrew = true;
}
assert(noDispatchThrew, '没有任何分发通道时抛错');

const blobResult = await importCameraJsonFromFile(
    makeFakeViewer().viewer,
    new Blob([JSON.stringify(raw)], { type: 'application/json' }),
    { frameRate: 30 }
);
assert(blobResult.track.keyframes.times.length === 120, 'File/Blob 路径可导入');

// ---------------------------------------------------------------------------
// 汇总
// ---------------------------------------------------------------------------

console.log(`\n结果：${checks - failures}/${checks} 项通过，${failures} 项失败`);
if (failures > 0) {
    process.exit(1);
}
