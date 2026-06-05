import fs from 'fs';
import http from 'http';
import path from 'path';
import { fileURLToPath } from 'url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dist = path.join(root, 'dist');
const host = process.env.HOST || '127.0.0.1';
const port = Number(process.env.PORT || 3000);
const gtImageDir = process.env.SUPERSPLAT_GT_IMAGE_DIR ||
    '/data/new_disk7/shenzhh/Dome_Dataset/MangoTv/1/data1_undistortion/images';
const mangotvPly = process.env.SUPERSPLAT_MANGOTV_PLY ||
    '/data/new_disk7/guochch/code/origin_3dgs/output/mangotv/point_cloud/iteration_30000/point_cloud.ply';

const types = new Map([
    ['.css', 'text/css; charset=utf-8'],
    ['.gif', 'image/gif'],
    ['.html', 'text/html; charset=utf-8'],
    ['.ico', 'image/x-icon'],
    ['.jpg', 'image/jpeg'],
    ['.jpeg', 'image/jpeg'],
    ['.js', 'text/javascript; charset=utf-8'],
    ['.json', 'application/json; charset=utf-8'],
    ['.map', 'application/json; charset=utf-8'],
    ['.png', 'image/png'],
    ['.ply', 'application/ply'],
    ['.svg', 'image/svg+xml'],
    ['.wasm', 'application/wasm'],
    ['.webmanifest', 'application/manifest+json']
]);

const send = (res, status, body, contentType = 'text/plain; charset=utf-8') => {
    res.writeHead(status, {
        'Content-Type': contentType,
        'Cache-Control': 'no-store'
    });
    res.end(body);
};

const resolveFile = (urlPath) => {
    const parsed = new URL(urlPath, `http://${host}:${port}`);
    const decoded = decodeURIComponent(parsed.pathname);
    const normalized = path.normalize(decoded).replace(/^(\.\.[/\\])+/, '');
    const candidate = path.join(dist, normalized === '/' ? 'index.html' : normalized);
    const relative = path.relative(dist, candidate);

    if (relative.startsWith('..') || path.isAbsolute(relative)) {
        return null;
    }

    return candidate;
};

const resolveGtImage = (urlPath) => {
    const parsed = new URL(urlPath, `http://${host}:${port}`);
    if (!parsed.pathname.startsWith('/gt-images/')) {
        return null;
    }

    const decoded = decodeURIComponent(parsed.pathname.substring('/gt-images/'.length));
    const candidate = path.join(gtImageDir, decoded);
    const relative = path.relative(gtImageDir, candidate);

    if (relative.startsWith('..') || path.isAbsolute(relative)) {
        return null;
    }

    return candidate;
};

const resolveLocalAsset = (urlPath) => {
    const parsed = new URL(urlPath, `http://${host}:${port}`);
    if (parsed.pathname === '/local-assets/mangotv-point-cloud.ply') {
        return mangotvPly;
    }
    return null;
};

if (!fs.existsSync(dist)) {
    console.error('dist directory not found. Run `npm run build` first.');
    process.exit(1);
}

const server = http.createServer((req, res) => {
    if (req.method !== 'GET' && req.method !== 'HEAD') {
        send(res, 405, 'Method not allowed');
        return;
    }

    const gtImage = resolveGtImage(req.url || '/');
    if (gtImage) {
        fs.readFile(gtImage, (readError, data) => {
            if (readError) {
                send(res, 404, 'Not found');
                return;
            }

            res.writeHead(200, {
                'Content-Type': types.get(path.extname(gtImage)) || 'application/octet-stream',
                'Cache-Control': 'no-store'
            });

            res.end(req.method === 'HEAD' ? undefined : data);
        });
        return;
    }

    const localAsset = resolveLocalAsset(req.url || '/');
    if (localAsset) {
        fs.readFile(localAsset, (readError, data) => {
            if (readError) {
                send(res, 404, 'Not found');
                return;
            }

            res.writeHead(200, {
                'Content-Type': types.get(path.extname(localAsset)) || 'application/octet-stream',
                'Cache-Control': 'no-store'
            });

            res.end(req.method === 'HEAD' ? undefined : data);
        });
        return;
    }

    const file = resolveFile(req.url || '/');
    if (!file) {
        send(res, 403, 'Forbidden');
        return;
    }

    fs.stat(file, (statError, stat) => {
        const target = !statError && stat.isDirectory() ? path.join(file, 'index.html') : file;

        fs.readFile(target, (readError, data) => {
            if (readError) {
                send(res, 404, 'Not found');
                return;
            }

            res.writeHead(200, {
                'Content-Type': types.get(path.extname(target)) || 'application/octet-stream',
                'Cache-Control': 'no-store'
            });

            res.end(req.method === 'HEAD' ? undefined : data);
        });
    });
});

server.listen(port, host, () => {
    console.log(`Serving dist at http://${host}:${port}/`);
});
