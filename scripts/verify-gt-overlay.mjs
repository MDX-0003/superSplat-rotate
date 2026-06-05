import fs from 'fs';
import path from 'path';
import { spawnSync } from 'child_process';
import { createRequire } from 'module';
import { fileURLToPath } from 'url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const host = process.env.SUPERSPLAT_VERIFY_URL || 'http://127.0.0.1:3000/';
const camerasPath = process.env.SUPERSPLAT_CAMERAS_JSON ||
    path.join(root, 'CameraData', '02', 'cameras.json');
const gtImagePath = process.env.SUPERSPLAT_GT_IMAGE ||
    path.join(root, 'CameraData', '02', 'camera_06_2026-06-04-214336', '0001.jpg');
const screenshotPath = process.env.SUPERSPLAT_VERIFY_SCREENSHOT || path.join(root, 'CameraData', 'screenshot.png');
const nodePath = '/root/.npm/_npx/420ff84f11983ee5/node_modules';
const require = createRequire(import.meta.url);

const requirePlaywright = () => {
    const requireScript = `
        const { chromium } = require('playwright');
        console.log(require.resolve('playwright'));
    `;
    const result = spawnSync(process.execPath, ['-e', requireScript], {
        encoding: 'utf8',
        env: {
            ...process.env,
            NODE_PATH: `${process.env.NODE_PATH ? `${process.env.NODE_PATH}:` : ''}${nodePath}`
        }
    });

    if (result.status !== 0) {
        throw new Error(`Playwright is not available. Run with NODE_PATH=${nodePath} or install Playwright. ${result.stderr}`);
    }
};

const runBrowserCheck = async () => {
    const { chromium } = require('playwright');
    const cameras = {
        name: 'cameras.json',
        type: 'application/json',
        base64: fs.readFileSync(camerasPath).toString('base64')
    };

    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({
        viewport: { width: 1280, height: 720 },
        deviceScaleFactor: 1
    });

    try {
        page.setDefaultTimeout(60000);
        await page.goto(`${host}?verifyGtOverlay=${Date.now()}`, { waitUntil: 'domcontentloaded' });
        await page.waitForFunction(() => window.scene && window.scene.events);

        const result = await page.evaluate(async ({ cameras }) => {
            const toBytes = (base64) => {
                const binary = atob(base64);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) {
                    bytes[i] = binary.charCodeAt(i);
                }
                return bytes;
            };

            const events = window.scene.events;
            await events.invoke('import', [{
                filename: cameras.name,
                contents: new File([toBytes(cameras.base64)], cameras.name, { type: cameras.type })
            }]);

            const poses = events.invoke('camera.poses');
            const pose = poses[0];
            events.fire('camera.setPose', pose, 0);
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

            const image = document.getElementById('gt-reference-dist') || document.getElementById('gt-reference');
            const container = document.getElementById('canvas-container');
            const containerRect = container.getBoundingClientRect();
            const imageRect = image.getBoundingClientRect();

            return {
                poseCount: poses.length,
                poseName: pose?.name,
                hasCalibratedData: !!(pose?.rotation && pose?.intrinsics),
                image: image ? {
                    id: image.id,
                    hidden: image.hidden,
                    src: image.src,
                    naturalWidth: image.naturalWidth,
                    naturalHeight: image.naturalHeight,
                    zIndex: getComputedStyle(image).zIndex
                } : null,
                container: {
                    width: containerRect.width,
                    height: containerRect.height
                },
                imageRect: {
                    width: imageRect.width,
                    height: imageRect.height
                }
            };
        }, { cameras });

        await page.locator('#canvas-container').screenshot({ path: screenshotPath });
        return result;
    } finally {
        await browser.close();
    }
};

const runPixelCheck = () => {
    const script = `
from PIL import Image
import json, math

screen = Image.open(${JSON.stringify(screenshotPath)}).convert('RGB')
gt = Image.open(${JSON.stringify(gtImagePath)}).convert('RGB')
sw, sh = screen.size
gw, gh = gt.size
scale = min(sw / gw, sh / gh)
dw = int(round(gw * scale))
dh = int(round(gh * scale))
left = int(round((sw - dw) / 2))
top = int(round((sh - dh) / 2))
gt_scaled = gt.resize((dw, dh), Image.Resampling.BICUBIC)
region = screen.crop((left, top, left + dw, top + dh))
acc = 0
acc2 = 0
maxd = 0
count = dw * dh * 3
for y in range(dh):
    sp = region.crop((0, y, dw, y + 1)).tobytes()
    gp = gt_scaled.crop((0, y, dw, y + 1)).tobytes()
    for a, b in zip(sp, gp):
        d = abs(a - b)
        acc += d
        acc2 += d * d
        if d > maxd:
            maxd = d
print(json.dumps({
    'screen': [sw, sh],
    'gt': [gw, gh],
    'displayRegion': {'left': left, 'top': top, 'width': dw, 'height': dh},
    'mae': acc / count,
    'rmse': math.sqrt(acc2 / count),
    'maxAbsDiff': maxd
}))
`;

    const result = spawnSync('python3', ['-c', script], { encoding: 'utf8' });
    if (result.status !== 0) {
        throw new Error(`Pixel check failed: ${result.stderr || result.stdout}`);
    }
    return JSON.parse(result.stdout);
};

try {
    requirePlaywright();
    const browserResult = await runBrowserCheck();
    const pixelResult = runPixelCheck();
    const summary = {
        browser: browserResult,
        pixel: pixelResult,
        screenshot: screenshotPath
    };

    console.log(JSON.stringify(summary, null, 2));

    if (!browserResult.hasCalibratedData ||
        !browserResult.image ||
        browserResult.image.hidden ||
        browserResult.poseCount !== 103 ||
        browserResult.image.naturalWidth !== 4147 ||
        browserResult.image.naturalHeight !== 2205 ||
        pixelResult.mae > 1.0) {
        process.exitCode = 1;
    }
} catch (error) {
    console.error(error.message ?? error);
    process.exit(1);
}
