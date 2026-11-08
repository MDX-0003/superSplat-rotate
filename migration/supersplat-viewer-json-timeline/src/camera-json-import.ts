/**
 * superSplat-viewer 运行时导入 cameras.json。
 *
 * 依赖两件事：
 *   1. 本目录 `patches/camera-manager.patch.md` 里的补丁，给 CameraManager 增加
 *      `cameraAnimation:set` 事件（或直接增加 `setAnimation` 方法）；
 *   2. viewer 已经完成首帧（`viewer.state.loaded === true`），或调用方自己保证
 *      此时 `cameraManager` 已创建。
 *
 * 模块不 import viewer 的类型，只用结构化类型，方便迁移到不同版本的 fork。
 */

import {
    buildCameraAnimTrack,
    buildViewerCameraSettingsPatch,
    parseCamerasJson
} from './camera-json-core.js';
import type {
    BuildAnimTrackOptions,
    ParseCamerasJsonResult,
    ViewerAnimTrack,
    ViewerCameraSettingsPatch
} from './camera-json-core.js';

type ViewerEventTarget = {
    fire: (name: string, ...args: unknown[]) => unknown;
};

/** `importCameraJson` 需要的最小 viewer 接口。 */
export type CameraJsonViewer = {
    events?: ViewerEventTarget;
    state?: {
        loaded?: boolean;
        hasSequence?: boolean;
    };
    /** 打了补丁后会在运行时存在；没有则走 events。 */
    cameraManager?: {
        setAnimation?: (track: ViewerAnimTrack | null, autoplay?: boolean) => void;
    };
};

export type ImportCameraJsonOptions = BuildAnimTrackOptions & {
    /** 导入后是否立即播放。默认 true。 */
    autoplay?: boolean;
};

export type ImportCameraJsonResult = ParseCamerasJsonResult & {
    track: ViewerAnimTrack;
    settingsPatch: ViewerCameraSettingsPatch;
    /** 实际走的分发路径，便于排查补丁是否生效。 */
    dispatchedVia: 'cameraManager.setAnimation' | 'events:cameraAnimation:set';
};

const assertViewerReady = (viewer: CameraJsonViewer) => {
    if (viewer.state?.loaded === false) {
        throw new Error(
            'viewer 尚未加载完成：请等 state.loaded 为 true 后再导入 cameras.json。' +
            'CameraManager 在首帧加载链里才创建，过早触发 cameraAnimation:set 会被丢弃'
        );
    }
    if (viewer.state?.hasSequence) {
        throw new Error('当前内容是可播放的 splat 序列（hasSequence），时间轴已被序列占用，不能同时播放相机动画');
    }
};

const dispatchTrack = (
    viewer: CameraJsonViewer,
    track: ViewerAnimTrack,
    autoplay: boolean
): ImportCameraJsonResult['dispatchedVia'] => {
    const manager = viewer.cameraManager;
    if (manager && typeof manager.setAnimation === 'function') {
        manager.setAnimation(track, autoplay);
        return 'cameraManager.setAnimation';
    }
    if (viewer.events) {
        viewer.events.fire('cameraAnimation:set', track, autoplay);
        return 'events:cameraAnimation:set';
    }
    throw new Error(
        'viewer 既没有 cameraManager.setAnimation，也没有 events。' +
        '请先应用 patches/camera-manager.patch.md'
    );
};

/**
 * 把 cameras.json 数据（已 JSON.parse 的数组）导入 viewer 并开始播放。
 *
 * @example
 * const result = importCameraJson(viewer, json, { frameRate: 30, autoplay: true });
 * console.log(result.track.duration, result.warnings);
 */
export const importCameraJson = (
    viewer: CameraJsonViewer,
    json: unknown,
    options: ImportCameraJsonOptions = {}
): ImportCameraJsonResult => {
    assertViewerReady(viewer);

    const parsed = parseCamerasJson(json);
    const track = buildCameraAnimTrack(parsed, options);
    // 复用已经构建好的轨道，避免大文件重复组装点列
    const settingsPatch = buildViewerCameraSettingsPatch(parsed, options, track);

    const dispatchedVia = dispatchTrack(viewer, track, options.autoplay ?? true);

    if (parsed.warnings.length > 0) {
        console.warn('[camera-json] 部分记录被跳过：\n' + parsed.warnings.join('\n'));
    }

    return {
        ...parsed,
        track,
        settingsPatch,
        dispatchedVia
    };
};

/** 从 `File` / `Blob` 读取并导入。 */
export const importCameraJsonFromFile = async (
    viewer: CameraJsonViewer,
    file: Blob,
    options: ImportCameraJsonOptions = {}
): Promise<ImportCameraJsonResult> => {
    const text = await file.text();
    return importCameraJson(viewer, JSON.parse(text), options);
};

/** 从 URL 读取并导入。 */
export const importCameraJsonFromUrl = async (
    viewer: CameraJsonViewer,
    url: string,
    options: ImportCameraJsonOptions = {}
): Promise<ImportCameraJsonResult> => {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`加载 cameras.json 失败：HTTP ${response.status} ${response.statusText}`);
    }
    return importCameraJson(viewer, await response.json(), options);
};

/**
 * 给一个元素装上“拖入 .json 即导入”的行为，返回卸载函数。
 * 只对文件名以 .json 结尾的文件生效，不拦截其他拖拽。
 */
export const attachCameraJsonDrop = (
    viewer: CameraJsonViewer,
    target: HTMLElement,
    options: ImportCameraJsonOptions = {}
): (() => void) => {
    const onDragOver = (event: DragEvent) => {
        event.preventDefault();
    };

    const onDrop = (event: DragEvent) => {
        const files = Array.from(event.dataTransfer?.files ?? []);
        const file = files.find(f => f.name.toLowerCase().endsWith('.json'));
        if (!file) {
            return;
        }
        event.preventDefault();
        importCameraJsonFromFile(viewer, file, options).catch((err) => {
            console.error('[camera-json] 导入失败', err);
        });
    };

    target.addEventListener('dragover', onDragOver);
    target.addEventListener('drop', onDrop);

    return () => {
        target.removeEventListener('dragover', onDragOver);
        target.removeEventListener('drop', onDrop);
    };
};

