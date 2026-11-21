param([switch]$AllRefs)
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'download-tool.ps1')
$tool = Get-VerifiedReleaseTool -Repository 'gitleaks/gitleaks' -Tag 'v8.30.1' -ArchiveName 'gitleaks_8.30.1_windows_x64.zip' -ChecksumsName 'gitleaks_8.30.1_checksums.txt' -BinaryName 'gitleaks.exe'
# Ordinary CI checks only the tested commit's reachable history. Unrelated refs
# belong to the separate scheduled audit, not to a PR's merge decision.
$scope = if ($AllRefs) { '--all' } else { 'HEAD' }
& $tool git --redact=100 --no-banner "--log-opts=$scope" --report-format=json "--report-path=$env:RUNNER_TEMP/gitleaks-redacted.json"
if ($LASTEXITCODE -ne 0) { throw '凭证扫描失败；诊断报告已完全脱敏' }
