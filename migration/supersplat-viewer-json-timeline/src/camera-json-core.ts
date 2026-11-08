/**
 * cameras.json -> superSplat-viewer 相机时间轴。
 *
 * 这个文件是整份迁移包的核心：只做纯数据转换，不 import playcanvas、不碰 DOM、
 * 不依赖 viewer 的任何内部对象。浏览器、Node、Worker 都能直接运行。
 *
 * 坐标规则与 superSplat 编辑器 `src/file-handler.ts` 的 `loadCameraPoses` 完全一致：
 *
 *   1. cameras.json 里的 position / rotation 来自 3DGS/COLMAP 世界；
 *   2. superSplat 与 superSplat-viewer 在加载 PLY 后都会给 splat 实体加
 *      Rz(180°)（viewer 里是 `entity.setLocalEulerAngles(0, 0, 180)`）；
 *   3. 因此相机位姿也要左乘同一个 S = diag(-1, -1, 1)：
 *        position_viewer = (-x, -y, z)
 *        forward_viewer  = (-R[0][2], -R[1][2], R[2][2])
 *   4. 时间轴用 look-at 相机，只消费 position / target / fov，所以
 *        target = position_viewer + forward_viewer * 10
 *      （10 只是任意的注视距离，方向才是关键；编辑器同样固定取 10）。
 *
 * 本文件不处理 rotation 的完整四元数（viewer 的动画相机没有 roll），
 * 只取旋转矩阵第 3 列作为前向。需要 GT 校准四元数时见 README 的“可选”章节。
 */

/** 三维向量（元组形式，方便直接塞进 AnimTrack）。 */
export type Vec3Tuple = [number, number, number];

/** 解析后的相机关键帧。 */
export type CameraPose = {
    /** 关键帧名称，来自 img_name，缺省为 `${namePrefix}_${frame}`。 */
    name: string;
    /** 时间轴帧号，等于该条记录在排序后数组里的下标。 */
    frame: number;
    /** 已转换到 viewer 世界坐标的相机位置。 */
    position: Vec3Tuple;
    /** 已转换到 viewer 世界坐标的注视点。 */
    target: Vec3Tuple;
    /** 较大轴视场角（度），与 superSplat 编辑器的 fov 语义一致。 */
    fov: number;
};

/** superSplat-viewer settings.animTracks[n] 的形状（v2 schema）。 */
export type ViewerAnimTrack = {
    name: string;
    /** 秒。播放游标走完一圈的时长。 */
    duration: number;
    /** 每秒帧数。keyframes.times 的单位是“帧”。 */
    frameRate: number;
    loopMode: 'none' | 'repeat' | 'pingpong';
    interpolation: 'step' | 'spline';
    smoothness: number;
    keyframes: {
        times: number[];
        values: {
            position: number[];
            target: number[];
            fov: number[];
        };
    };
};

/** viewer settings 中与相机动画有关的三块内容（v2）。 */
export type ViewerCameraSettingsPatch = {
    animTracks: ViewerAnimTrack[];
    cameras: {
        initial: {
            position: Vec3Tuple;
            target: Vec3Tuple;
            fov: number;
        };
    }[];
    startMode: 'animTrack';
};

export type ParseCamerasJsonOptions = {
    /** img_name 缺失时生成名字的前缀。默认 'camera'。 */
    namePrefix?: string;
};

export type ParseCamerasJsonResult = {
    /** 按帧号升序排列的关键帧。 */
    poses: CameraPose[];
    /**
     * 时间轴总帧数。等于输入数组长度（与编辑器 `timeline.setFrames(json.length)` 一致），
     * 即使其中有被跳过的非法记录。
     */
    frameCount: number;
    /** 非法记录等可诊断信息。 */
    warnings: string[];
};

export type BuildAnimTrackOptions = {
    /** 时间轴帧率。默认 30，与编辑器 timeline 默认值一致。 */
    frameRate?: number;
    /** 循环方式。默认 'repeat'，与编辑器时间轴自动回绕一致。 */
    loopMode?: 'none' | 'repeat' | 'pingpong';
    /** 样条平滑度。默认 1，与编辑器 timeline.smoothness 默认值一致。 */
    smoothness?: number;
    /** 轨道名称，只用于诊断。默认 'cameras.json'。 */
    name?: string;
};

/** cameras.json 单条记录里我们关心的字段。 */
type RawCameraPose = Record<string, unknown>;

const DEFAULT_FRAME_RATE = 30;
const DEFAULT_FOV = 60;
const DEFAULT_SMOOTHNESS = 1;

/** 与编辑器 `vec.copy(z).mulScalar(10).add(position)` 中的 10 一致。 */
export const CAMERA_JSON_TARGET_DISTANCE = 10;

const RAD_TO_DEG = 180 / Math.PI;

const isFiniteNumber = (value: unknown): value is number =>
    typeof value === 'number' && Number.isFinite(value);

const isVec3 = (value: unknown): value is [number, number, number] =>
    Array.isArray(value) && value.length >= 3 && value.slice(0, 3).every(isFiniteNumber);

const isRotation3x3 = (value: unknown): value is number[][] =>
    Array.isArray(value) &&
    value.length >= 3 &&
    value.slice(0, 3).every(row =>
        Array.isArray(row) && row.length >= 3 && row.slice(0, 3).every(isFiniteNumber));

/**
 * 把 3DGS/COLMAP 世界坐标的相机位置转换到 viewer 世界坐标。
 * 等价于对位置左乘 Rz(180°)：S = diag(-1, -1, 1)。
 */
export const transformCameraPosition = (position: readonly number[]): Vec3Tuple => [
    -position[0],
    -position[1],
    position[2]
];

/**
 * 取旋转矩阵的第 3 列作为相机前向，并转换到 viewer 世界坐标。
 *
 * 这里刻意不转置矩阵，也不取负号：编辑器的 `loadCameraPoses` 用的就是
 * `(-R[0][2], -R[1][2], R[2][2])`。本项目的生成端
 * `tills/gltf_to_cameras_json.py` 把旋转矩阵的列写成相机的 right / up / forward。
 */
export const transformCameraForward = (rotation: readonly (readonly number[])[]): Vec3Tuple => [
    -rotation[0][2],
    -rotation[1][2],
    rotation[2][2]
];

/** 由位置和前向生成 look-at 注视点。 */
export const makeCameraTarget = (
    position: readonly number[],
    forward: readonly number[]
): Vec3Tuple => [
    position[0] + forward[0] * CAMERA_JSON_TARGET_DISTANCE,
    position[1] + forward[1] * CAMERA_JSON_TARGET_DISTANCE,
    position[2] + forward[2] * CAMERA_JSON_TARGET_DISTANCE
];

/**
 * 计算 fov，优先级与编辑器一致：
 *
 *   1. 有 `fov_x`（水平视场角，度）就直接用；
 *   2. 否则用 `width/height/fx/fy` 求水平、垂直 fov，取较大者；
 *   3. 都没有则用 60°。
 *
 * 编辑器把较大轴 fov 交给 PlayCanvas，并用 `camera.horizontalFov = width > height`
 * 决定它作用在哪个轴；viewer 用同样的约定（`horizontalFov = 画布宽 > 高`）。
 * 因此横屏素材 + 横屏画布时两者完全一致。
 */
export const resolveCameraFov = (raw: RawCameraPose): number => {
    if (isFiniteNumber(raw.fov_x)) {
        return raw.fov_x;
    }

    const { fx, fy, width, height } = raw;
    if (
        isFiniteNumber(fx) && fx > 0 &&
        isFiniteNumber(fy) && fy > 0 &&
        isFiniteNumber(width) && width > 0 &&
        isFiniteNumber(height) && height > 0
    ) {
        const fovX = 2 * Math.atan(width / (2 * fx)) * RAD_TO_DEG;
        const fovY = 2 * Math.atan(height / (2 * fy)) * RAD_TO_DEG;
        return Math.max(fovX, fovY);
    }

    return DEFAULT_FOV;
};

const isRecord = (value: unknown): value is RawCameraPose =>
    typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * 排序键。编辑器优先用 `id`，没有就用 `img_name` 末尾的数字：
 *
 *     a.id ?? a.img_name?.match(/\d*$/)?.[0]
 *
 * 编辑器里 `id: 0` 会被 `(avalue && bvalue)` 当成 falsy 而退化为“相等”，
 * 这里按原本的意图返回 0 并正常参与排序。其余行为保持一致。
 */
const getPoseSortKey = (raw: RawCameraPose): number | null => {
    const id = raw.id;
    if (isFiniteNumber(id)) {
        // 与编辑器的 parseInt 语义一致：小数截断而不是四舍五入。
        return Math.trunc(id);
    }
    if (typeof id === 'string' && id.trim() !== '') {
        const parsed = parseInt(id, 10);
        if (!Number.isNaN(parsed)) {
            return parsed;
        }
    }

    const name = raw.img_name;
    if (typeof name === 'string') {
        const match = name.match(/\d+$/);
        if (match) {
            return parseInt(match[0], 10);
        }
    }

    return null;
};

const compareRawPoses = (a: unknown, b: unknown): number => {
    const aKey = isRecord(a) ? getPoseSortKey(a) : null;
    const bKey = isRecord(b) ? getPoseSortKey(b) : null;
    if (aKey === null || bKey === null) {
        return 0;
    }
    return aKey - bKey;
};

/**
 * 解析 cameras.json。
 *
 * - 输入必须是数组；否则抛 `TypeError`。
 * - 记录按 `id`（或 `img_name` 末尾数字）升序排序；无法解析的记录保留原顺序。
 * - `frame` 是排序后的下标，不是 `id`，这与编辑器一致。
 * - 缺少合法 `position` / `rotation` 的记录会被跳过并记入 `warnings`，
 *   但 `frameCount` 仍取输入数组长度。
 */
export const parseCamerasJson = (
    input: unknown,
    options: ParseCamerasJsonOptions = {}
): ParseCamerasJsonResult => {
    if (!Array.isArray(input)) {
        throw new TypeError('cameras.json 必须是一个数组（每条记录一个相机位姿）');
    }

    const namePrefix = options.namePrefix ?? 'camera';
    const warnings: string[] = [];

    const ordered = input
        .map((raw, index) => ({ raw, index }))
        .sort((a, b) => compareRawPoses(a.raw, b.raw) || (a.index - b.index));

    const poses: CameraPose[] = [];

    ordered.forEach(({ raw }, index) => {
        if (!isRecord(raw)) {
            warnings.push(`第 ${index} 条记录不是对象，已跳过`);
            return;
        }

        const record = raw;
        const label = typeof record.img_name === 'string' ? record.img_name : `#${index}`;

        if (!isVec3(record.position)) {
            warnings.push(`第 ${index} 条记录（${label}）position 不是合法三维数组，已跳过`);
            return;
        }
        if (!isRotation3x3(record.rotation)) {
            warnings.push(`第 ${index} 条记录（${label}）rotation 不是合法 3x3 矩阵，已跳过`);
            return;
        }

        const position = transformCameraPosition(record.position);
        const forward = transformCameraForward(record.rotation);
        const target = makeCameraTarget(position, forward);

        poses.push({
            name: typeof record.img_name === 'string' ? record.img_name : `${namePrefix}_${index}`,
            frame: index,
            position,
            target,
            fov: resolveCameraFov(record)
        });
    });

    return {
        poses,
        frameCount: input.length,
        warnings
    };
};

/**
 * 把解析结果构建成 superSplat-viewer 的 AnimTrack。
 *
 * 时间轴语义（与编辑器 `registerTimelineEvents` + `CameraAnimTrack` 对齐）：
 *
 * - `keyframes.times` 单位是“帧”，取 `pose.frame`；
 * - `frameRate` 默认 30；
 * - `duration = frameCount / frameRate` 秒。播放游标走满 duration 后回绕到 0，
 *   对应编辑器 `time = (time + dt * fps) % frames`；
 * - `loopMode: 'repeat'` 对应编辑器时间轴的无限循环；
 * - `smoothness: 1` 对应编辑器默认值。
 *
 * viewer 的 `AnimState.fromTrack` 会用 `fromPointsLooping(duration * frameRate, ...)`
 * 建样条，与编辑器的 `CubicSpline.fromPointsLooping(frames, ...)` 参数完全一致。
 */
export const buildCameraAnimTrack = (
    result: Pick<ParseCamerasJsonResult, 'poses' | 'frameCount'>,
    options: BuildAnimTrackOptions = {}
): ViewerAnimTrack => {
    const frameRate = options.frameRate ?? DEFAULT_FRAME_RATE;
    if (!(typeof frameRate === 'number' && Number.isFinite(frameRate) && frameRate > 0)) {
        throw new RangeError(`frameRate 必须是正数，收到 ${String(frameRate)}`);
    }
    if (result.poses.length === 0) {
        throw new Error('没有可用的相机位姿，无法构建时间轴');
    }

    const ordered = result.poses.slice().sort((a, b) => a.frame - b.frame);

    const times: number[] = [];
    const position: number[] = [];
    const target: number[] = [];
    const fov: number[] = [];

    for (const pose of ordered) {
        times.push(pose.frame);
        position.push(pose.position[0], pose.position[1], pose.position[2]);
        target.push(pose.target[0], pose.target[1], pose.target[2]);
        fov.push(pose.fov);
    }

    const lastFrame = times[times.length - 1];
    const frameCount = Math.max(result.frameCount, lastFrame + 1);

    return {
        name: options.name ?? 'cameras.json',
        duration: frameCount / frameRate,
        frameRate,
        loopMode: options.loopMode ?? 'repeat',
        interpolation: 'spline',
        smoothness: options.smoothness ?? DEFAULT_SMOOTHNESS,
        keyframes: {
            times,
            values: {
                position,
                target,
                fov
            }
        }
    };
};

/**
 * 生成可直接合并进 viewer settings 的三块内容。
 *
 * 用法见 README 的“方案 A：启动前注入 settings”。要求 settings 是 v2
 * （`version: 2`）；v1 的 animTrack.times 单位是秒，不要直接混用。
 */
export const buildViewerCameraSettingsPatch = (
    result: ParseCamerasJsonResult,
    options: BuildAnimTrackOptions = {},
    track?: ViewerAnimTrack
): ViewerCameraSettingsPatch => {
    const animTrack = track ?? buildCameraAnimTrack(result, options);
    const first = result.poses.slice().sort((a, b) => a.frame - b.frame)[0];

    return {
        animTracks: [animTrack],
        cameras: [{
            initial: {
                position: first.position,
                target: first.target,
                fov: first.fov
            }
        }],
        startMode: 'animTrack'
    };
};

/**
 * 在已有 v2 settings 上覆盖相机动画相关字段。返回新对象，不修改入参。
 */
export const withViewerCameraSettings = <T extends Record<string, unknown>>(
    settings: T,
    result: ParseCamerasJsonResult,
    options: BuildAnimTrackOptions = {}
): T & ViewerCameraSettingsPatch => {
    if (settings.version !== 2) {
        throw new Error(
            `只支持 v2 settings（version: 2），收到 version: ${String(settings.version)}。` +
            'v1 的 animTrack.times 单位是秒，不能与这里的帧号混用'
        );
    }
    return {
        ...settings,
        ...buildViewerCameraSettingsPatch(result, options)
    } as T & ViewerCameraSettingsPatch;
};

export { DEFAULT_FOV as CAMERA_JSON_DEFAULT_FOV, DEFAULT_FRAME_RATE as CAMERA_JSON_DEFAULT_FRAME_RATE };
