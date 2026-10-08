import { app, BrowserWindow, Menu, Tray, nativeImage } from 'electron';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
// Keep the established data directory when the display/product name changes.
app.setPath('userData', path.join(app.getPath('appData'), 'growth-companion-desktop'));
let sidecar = null;
let window = null;
let tray = null;
let quitting = false;
let sidecarOutput = '';

function executablePath() {
  if (process.env.GROWTH_SIDECAR_PATH) return process.env.GROWTH_SIDECAR_PATH;
  if (app.isPackaged) {
    return path.join(
      process.resourcesPath,
      'sidecar',
      process.platform === 'win32' ? 'growth-sidecar.exe' : 'growth-sidecar',
    );
  }
  return path.join(
    root,
    '.venv',
    process.platform === 'win32' ? 'Scripts/ahadiff.exe' : 'bin/ahadiff',
  );
}

function viewerPath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'viewer')
    : path.join(root, 'viewer', 'dist');
}

function iconPath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'icon.png')
    : path.join(root, 'viewer', 'public', 'icons', 'growth-192.png');
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

async function waitForServer(origin) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (!sidecar || sidecar.exitCode !== null) {
      throw new Error(`本机服务提前退出（code=${sidecar?.exitCode}）：${sidecarOutput.slice(-2000)}`);
    }
    try {
      const response = await fetch(`${origin}/healthz`, { signal: AbortSignal.timeout(1000) });
      if (response.ok) return;
    } catch {
      // The child may still be initializing.
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error('本机服务启动超时');
}

async function startSidecar() {
  const executable = executablePath();
  const viewer = viewerPath();
  if (!existsSync(executable)) throw new Error(`缺少本机服务：${executable}`);
  if (!existsSync(path.join(viewer, 'index.html'))) throw new Error('缺少已构建界面');
  const workspace = process.env.GROWTH_WORKSPACE || path.join(app.getPath('userData'), 'workspace');
  mkdirSync(workspace, { recursive: true });
  if (!existsSync(path.join(workspace, '.git'))) {
    mkdirSync(path.join(workspace, '.ahadiff'), { recursive: true });
  }
  const port = await freePort();
  const origin = `http://127.0.0.1:${port}`;
  sidecar = spawn(
    executable,
    ['serve', '--repo-root', workspace, '--port', String(port), '--no-browser'],
    {
      cwd: workspace,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, AHADIFF_VIEWER_DIST: viewer },
    },
  );
  for (const stream of [sidecar.stdout, sidecar.stderr]) {
    stream?.on('data', (chunk) => {
      sidecarOutput = (sidecarOutput + String(chunk)).slice(-4000);
    });
  }
  sidecar.once('error', (error) => {
    console.error('本机服务启动失败：', error.message);
    app.quit();
  });
  await waitForServer(origin);
  return origin;
}

function createWindow(origin) {
  window = new BrowserWindow({
    width: 1160,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    icon: iconPath(),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(`${origin}/`)) event.preventDefault();
  });
  if (process.env.GROWTH_SMOKE_EXIT === '1') {
    window.webContents.once('did-finish-load', () => {
      console.log('GROWTH_DESKTOP_SMOKE_OK');
      app.quit();
    });
    window.webContents.once('did-fail-load', (_event, code, description) => {
      console.error(`桌面页面加载失败：${code} ${description}`);
      app.exit(1);
    });
  }
  void window.loadURL(origin);
  window.on('close', (event) => {
    if (!quitting && tray) {
      event.preventDefault();
      window.hide();
    }
  });
}

function createTray() {
  const icon = nativeImage.createFromPath(iconPath());
  tray = new Tray(icon);
  tray.setToolTip('知枝');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '我有 5 分钟', click: () => { window?.show(); window?.focus(); } },
    { label: '打开主窗口', click: () => { window?.show(); window?.focus(); } },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() },
  ]));
  tray.on('double-click', () => { window?.show(); window?.focus(); });
}

app.whenReady().then(async () => {
  try {
    const origin = await startSidecar();
    createWindow(origin);
    createTray();
  } catch (error) {
    console.error(error);
    app.quit();
  }
});

app.on('before-quit', () => {
  quitting = true;
  if (sidecar && sidecar.exitCode === null) sidecar.kill();
});

app.on('window-all-closed', () => {
  if (process.platform === 'darwin') return;
  if (!tray) app.quit();
});

app.on('activate', () => {
  window?.show();
});
