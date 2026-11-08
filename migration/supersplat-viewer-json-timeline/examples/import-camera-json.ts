/**
 * 最小 UI 接入示例：一个文件选择按钮 + 拖拽导入。
 *
 * 把 `installCameraJsonImport` 放在 viewer 加载完成后调用（例如 `state.loaded` 变为 true、
 * 或首帧事件之后）。`host` 是页面上任意容器，按钮会插到它里面。
 *
 * 这段代码不依赖任何 UI 框架；如果你的项目已有工具栏，把按钮换成自己的按钮，
 * 保留 `importCameraJsonFromFile` 调用即可。
 */

import { attachCameraJsonDrop, importCameraJsonFromFile } from '../src/camera-json-import';
import type { CameraJsonViewer } from '../src/camera-json-import';

export type InstallCameraJsonImportOptions = {
    /** 导入后是否自动播放，默认 true。 */
    autoplay?: boolean;
    /** 时间轴帧率，默认 30。 */
    frameRate?: number;
};

/**
 * @returns 卸载函数：移除按钮、文件输入和拖拽监听。
 */
export const installCameraJsonImport = (
    viewer: CameraJsonViewer,
    host: HTMLElement,
    options: InstallCameraJsonImportOptions = {}
): (() => void) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = '导入相机 JSON';
    button.style.cssText = 'position:absolute;top:12px;right:12px;z-index:10;padding:6px 10px;';

    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json,application/json';
    input.hidden = true;

    const onButtonClick = () => input.click();

    const onFileChange = () => {
        const file = input.files?.[0];
        if (!file) {
            return;
        }
        importCameraJsonFromFile(viewer, file, {
            autoplay: options.autoplay ?? true,
            frameRate: options.frameRate ?? 30
        }).catch((error) => {
            console.error('[camera-json] 导入失败', error);
        });
        input.value = '';
    };

    button.addEventListener('click', onButtonClick);
    input.addEventListener('change', onFileChange);
    host.append(button, input);

    const detachDrop = attachCameraJsonDrop(viewer, host, {
        autoplay: options.autoplay ?? true,
        frameRate: options.frameRate ?? 30
    });

    return () => {
        detachDrop();
        button.removeEventListener('click', onButtonClick);
        input.removeEventListener('change', onFileChange);
        button.remove();
        input.remove();
    };
};
