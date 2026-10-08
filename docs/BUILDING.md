# 构建与发布

[返回项目首页](../README.md)

## 唯一构建环境

所有构建只允许在 GitHub 托管的 Actions runner 上执行，不使用本机或自托管 runner。前端和 Electron 的项目构建命令、Python sidecar 脚本均检查运行环境；不要伪造 CI 环境变量绕过限制。

工作流：`.github/workflows/desktop-build.yml`，页面为 [Desktop Build](https://github.com/sep-zxy/zhizhi/actions/workflows/desktop-build.yml)。

| 目标 | Runner | 产物 |
| --- | --- | --- |
| Windows x64 | `windows-latest` | NSIS `.exe` |
| macOS arm64 | `macos-15` | DMG `.dmg` |

工作流锁定 Node.js 22、pnpm 10.33.0、Python 3.13，并使用仓库中的 pnpm 和 uv 锁文件。Electron 使用 `desktop/package.json` 中锁定的版本。

## 操作步骤

1. 修改源码和文档，在本地完成语法、类型与配置校验。
2. 使用中文提交信息提交修改，并推送到 GitHub。
3. 手动触发工作流，明确选择要构建的分支或已有标签。
4. 确认两个平台任务的结果；查看日志与 `build-info.json` 中的源码提交。
5. 下载成功任务的 Artifact，校验安装包哈希。
6. 发布时将已验证的安装包、校验文件和构建记录附到 GitHub Release，不重新在本地打包。

```powershell
$ErrorActionPreference = 'Stop'
gh workflow run 'desktop-build.yml' --repo 'sep-zxy/zhizhi' --ref 'main'
gh run list --repo 'sep-zxy/zhizhi' --workflow 'desktop-build.yml' --limit 5
# 替换 123456789 为实际运行编号
gh run view 123456789 --repo 'sep-zxy/zhizhi'
gh run download 123456789 --repo 'sep-zxy/zhizhi' --dir './dist/downloads/123456789'
```

每次运行都对应确定的源码提交。发布标签应指向成功构建的提交；发布已有产物不需要重新构建。

## 产物与验证

Artifact 分别命名为 `zhizhi-windows-x64` 与 `zhizhi-macos-arm64`，每份包括安装包、`SHA256SUMS.txt` 和 `build-info.json`。

CI 会检查：

- TypeScript 和 Electron 入口语法。
- 前端资源构建及初始 JavaScript 资源引用。
- PyInstaller sidecar 打包与 `--version` 启动。
- Electron 包内的前端、后端与图标资源。
- 打包 PNG 与仓库新图标的哈希一致。
- Windows 打包应用能启动本机服务并加载页面，然后自动退出。
- macOS 应用的临时签名及后端启动。

这些是构建与启动检查，不进行截图或页面视觉审查，不覆盖跨端同步、模型回复质量和真实使用情境。Windows 安装包的交互安装及 macOS 图形界面启动也不在上述检查范围内。

下载后可核对安装包：

```powershell
$ErrorActionPreference = 'Stop'
Get-FileHash -Algorithm SHA256 -LiteralPath './dist/downloads/123456789/zhizhi-windows-x64/zhizhi-0.1.1-win-x64.exe'
```

将结果与同目录下 `SHA256SUMS.txt` 比较。发布目录按平台区分，以免两个平台的同名校验文件互相覆盖。

## 版本与兼容

知枝桌面版本由 `desktop/package.json` 决定；Python 分析底座保留 AhaDiff 的技术版本和 `ahadiff` CLI。两者不要求相同。

保持 Electron `appId` 为 `io.local.growthcompanion`、数据目录为 `growth-companion-desktop`。改动产品名称不应隐式改变已有数据位置。

Windows 代码签名和 Apple 公证尚未配置。macOS 当前使用 ad hoc 签名；不要将成功的临时签名校验描述为已公证。
