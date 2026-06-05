import fs from 'fs';
import path from 'path';
import { spawnSync } from 'child_process';

const REQUIRED = [20, 19, 0];

const versionOf = (nodePath) => {
    const result = spawnSync(nodePath, ['--version'], { encoding: 'utf8' });
    if (result.status !== 0) {
        return null;
    }

    const match = result.stdout.trim().match(/^v(\d+)\.(\d+)\.(\d+)/);
    return match ? match.slice(1).map(Number) : null;
};

const isSupported = (version) => {
    if (!version) {
        return false;
    }

    for (let i = 0; i < REQUIRED.length; ++i) {
        if (version[i] > REQUIRED[i]) {
            return true;
        }
        if (version[i] < REQUIRED[i]) {
            return false;
        }
    }

    return true;
};

const currentVersion = versionOf(process.execPath);
if (isSupported(currentVersion)) {
    process.exit(spawnSync(process.argv[2], process.argv.slice(3), {
        stdio: 'inherit',
        shell: process.platform === 'win32'
    }).status ?? 1);
}

const candidates = [
    process.env.SUPERSPLAT_NODE_HOME,
    '/data/new_disk7/shenzhh/worldmodel/node-v24.15.0-linux-x64',
    '/data/new_disk7/shenzhh/node-v24.15.0-linux-x64'
].filter(Boolean);

const nodeHome = candidates.find((candidate) => {
    const nodeBin = path.join(candidate, 'bin', 'node');
    return fs.existsSync(nodeBin) && isSupported(versionOf(nodeBin));
});

if (!nodeHome) {
    console.error(`SuperSplat requires Node >= ${REQUIRED.join('.')}. Current Node is ${process.version}.`);
    console.error('Install Node 20.19+ or set SUPERSPLAT_NODE_HOME to a compatible Node installation.');
    process.exit(1);
}

const env = {
    ...process.env,
    PATH: `${path.join(nodeHome, 'bin')}${path.delimiter}${process.env.PATH || ''}`
};

process.exit(spawnSync(process.argv[2], process.argv.slice(3), {
    stdio: 'inherit',
    shell: process.platform === 'win32',
    env
}).status ?? 1);
