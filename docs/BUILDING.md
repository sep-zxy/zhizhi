# 构建与发布

[返回项目首页](../README.md)

## 唯一构建环境

所有构建只允许在 GitHub 托管的 Actions runner 上执行，不使用本机或自托管 runner。前端和 Electron 的项目构建命令、Python sidecar 脚本均检查运行环境；不要伪造 CI 环境变量绕过限制。

工作流：`.github/workflows/desktop-build.yml`，页面为 [Desktop Build](https://github.com/sep-zxy/zhizhi/actions/workflows/desktop-build.yml)。

| 目标 | Runner | 产物 |
| --- | --- | --- |
| Windows x64 | `windows-2025` | NSIS `.exe` |
| macOS arm64 | `macos-26` | DMG `.dmg` |

共享的 `.github/actions/setup-toolchain/action.yml` 锁定 Node.js 22.23.5、pnpm 10.33.0、uv 0.13.9 和 Python 3.13.18，第三方 Action 固定到提交 SHA，并使用仓库中的 pnpm 和 uv 锁文件。仅安装依赖的任务开启依赖缓存，避免只读配置检查在保存空缓存时失败。Electron 使用 `desktop/package.json` 中锁定的版本。

## 合并检查

`Static Checks` 在 PR、主分支 push、合并队列和手动触发时运行同一套流程。PR 默认检查 GitHub 生成的模拟合并提交；主分支检查实际落地提交，合并后再次运行是必要的。

流程为：密钥扫描、语法类型检查、工作流及发布门禁验证 → 同一提交的 Windows/macOS 构建与启动检查 → `CI Gate`。任一任务失败、取消或跳过，`CI Gate` 都不会通过；主分支保护要求该检查通过且分支与最新主分支保持同步。

普通 CI 的密钥扫描只遍历当前提交的可达历史；独立的 `Security Audit` 每周一和手动触发时扫描所有已获取的分支、标签。旧测试样例仅按 `.gitleaksignore` 中的精确历史指纹豁免，新增命中仍会失败。

## 操作步骤

1. 修改源码和文档，将验证与构建交由 GitHub 托管 runner。
2. 使用中文提交信息提交修改，并推送到 GitHub。
3. 创建 PR，等待完整 CI；需要单独预览时，可手动触发 `Desktop Build`。预览产物不能用于正式发布。
4. 确认两个平台任务的结果；查看日志与 `build-info.json` 中的源码提交。
5. 下载成功任务的 Artifact，校验安装包哈希。
6. PR 合并后等待主分支的完整 `Static Checks` 通过。从 `main` 触发 `publish-release.yml`，填写这次主分支 push CI 的运行编号与桌面版本。先保留默认 `dry_run=true` 预演，确认后以 `dry_run=false` 正式发布。

发布门禁检查仓库身份、工作流路径、主分支 push 事件、全部必需任务、提交已进入 main、源码版本及产物的提交/运行编号/运行次数/平台/启动结果/文件名/SHA256。来自 PR、Fork、手动打包或失败/不完整 CI 的产物均被拒绝。若重新运行 CI，必须重跑全部任务，避免混入旧运行次数的安装包。验证阶段只有读取权限；只有验证成功且明确关闭预演时，发布任务才获得写入权限。已发布版本或指向其他提交的标签不能覆盖。

预演同样检查已发布版本、已有草稿和标签归属，避免预演通过后才发现版本冲突。正式写入前会再次核对现有状态；API 查询异常时停止发布，不将网络或权限故障当作版本不存在。

```powershell
$ErrorActionPreference = 'Stop'
gh workflow run 'desktop-build.yml' --repo 'sep-zxy/zhizhi' --ref 'main'
gh run list --repo 'sep-zxy/zhizhi' --workflow 'desktop-build.yml' --limit 5
# 替换 123456789 为实际运行编号
gh run view 123456789 --repo 'sep-zxy/zhizhi'
gh run download 123456789 --repo 'sep-zxy/zhizhi' --dir './dist/downloads/123456789'
gh workflow run 'publish-release.yml' --repo 'sep-zxy/zhizhi' --ref 'main' -f 'run_id=123456789' -f 'version=0.1.1' -f 'dry_run=true'
# 预演通过后，使用相同输入并设置 dry_run=false 正式发布
```

每次运行都对应确定的源码提交。发布标签应指向成功构建的提交；发布已有产物不需要重新构建。

新门禁合并后，旧的 Desktop Build 运行不能直接发布；应使用合并后主分支完整 CI 的产物。PR 验证不创建 Release，也不自动合并。

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
