# 知枝桌面宿主

此目录负责 Electron 窗口、托盘和本机 Python 服务。产品界面来自 `viewer/`，分析与知识能力来自 `src/ahadiff/`。

- `main.mjs`：启动 sidecar，加载本机界面，管理窗口与托盘。
- `sidecar_entry.py`：PyInstaller 的 Python 入口。
- `build-sidecar.ps1`：只允许在 GitHub 托管的 runner 上运行。
- `electron-builder.yml`：Windows NSIS、macOS DMG 与应用资源配置。
- `build/icon.ico`、`build/icon.icns`：桌面品牌图标。

所有前端、sidecar 和 Electron 构建统一通过 [Desktop Build](../.github/workflows/desktop-build.yml) 执行。本地不运行 `pnpm build`、PyInstaller 或 `package:win` / `package:mac`。完整流程见 [构建说明](../docs/BUILDING.md)。

可用 `GROWTH_WORKSPACE` 指定工作区；默认沿用系统应用数据目录中的 `growth-companion-desktop/workspace`。技术 `appId` 继续使用 `io.local.growthcompanion`，避免品牌更新隐式迁移状态。

renderer 只加载本机 loopback 来源，保持 `contextIsolation`、禁用 Node 集成和沙箱。宿主不向页面开放 shell 或任意文件接口。
