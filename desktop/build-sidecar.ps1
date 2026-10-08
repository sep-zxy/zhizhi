$ErrorActionPreference = 'Stop'

if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'sidecar 构建仅允许在 GitHub 托管的 Actions runner 上执行。请触发 Desktop Build 工作流。'
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$entry = Join-Path $PSScriptRoot 'sidecar_entry.py'
$distPath = Join-Path $PSScriptRoot 'sidecar'
$workPath = Join-Path (Join-Path $PSScriptRoot '.pyinstaller') 'build'
$specPath = Join-Path $PSScriptRoot '.pyinstaller'
$binaryName = if ($IsWindows) { 'growth-sidecar.exe' } elseif ($IsMacOS) {
    'growth-sidecar'
} else {
    throw '桌面 sidecar 仅支持 Windows 和 macOS 构建'
}

Push-Location -LiteralPath $projectRoot
try {
    $buildArgs = @('run', '--frozen', '--no-dev', '--group', 'desktop', 'pyinstaller',
        '--clean', '--noconfirm', '--onedir', '--collect-all', 'ahadiff',
        '--collect-all', 'genanki',
        '--name', 'growth-sidecar', '--distpath', $distPath,
        '--workpath', $workPath, '--specpath', $specPath, $entry)
    if (Get-Command rtk -ErrorAction SilentlyContinue) {
        rtk proxy uv @buildArgs
    } else {
        uv @buildArgs
    }
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }
    $executable = Join-Path (Join-Path $distPath 'growth-sidecar') $binaryName
    if (-not (Test-Path -LiteralPath $executable)) { throw "缺少 sidecar 可执行文件：$executable" }
    if (Get-Command rtk -ErrorAction SilentlyContinue) {
        rtk proxy $executable --version
    } else {
        & $executable --version
    }
    if ($LASTEXITCODE -ne 0) { throw 'sidecar 版本检查失败' }
}
finally {
    Pop-Location
}
