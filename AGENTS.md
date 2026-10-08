# 项目协作规则

始终使用简体中文解释；代码、命令、日志保留原文。提交信息使用中文。

- 所有构建必须在 **GitHub 托管的 GitHub Actions runner** 上执行。禁止本地构建，也禁止使用自托管 runner。
- 禁止本地执行 `pnpm build`、`vite build`、`uv build`、PyInstaller 或 Electron 打包。不要设置假的 CI 环境变量绕过限制。
- 构建入口为 `.github/workflows/desktop-build.yml`。先将修改推送到 GitHub，再触发工作流，下载对应提交的产物。
- 本地只做必要的语法、类型和配置校验；未经明确要求不做视觉检查。
- Python 通过 `uv` 调用，所有 CLI 使用 PowerShell 7。PowerShell 脚本首部包含 `$ErrorActionPreference = 'Stop'`，文件读写指定 UTF8。
- 保留 `ahadiff` 技术入口、Electron `appId` 和 `growth-companion-desktop` 数据目录；变更这些值必须设计数据迁移。
- 不提交 `.local/`、`.ahadiff/`、凭证、开发截图、验收运行数据、依赖目录或构建产物。
- 保留上游许可证与版权声明。优先小步修改，不删除仅因主界面暂未引用的底层能力或测试。
