$ErrorActionPreference = 'Stop'

function Get-VerifiedReleaseTool {
    param(
        [Parameter(Mandatory)][string]$Repository,
        [Parameter(Mandatory)][string]$Tag,
        [Parameter(Mandatory)][string]$ArchiveName,
        [Parameter(Mandatory)][string]$ChecksumsName,
        [Parameter(Mandatory)][string]$BinaryName
    )
    if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
        throw 'CI 工具仅在 GitHub 托管 runner 上执行'
    }
    $baseUrl = "https://github.com/$Repository/releases/download/$Tag"
    $archive = Join-Path $env:RUNNER_TEMP $ArchiveName
    $checksumFile = Join-Path $env:RUNNER_TEMP $ChecksumsName
    foreach ($download in @(@($ArchiveName, $archive), @($ChecksumsName, $checksumFile))) {
        Invoke-WebRequest -Uri "$baseUrl/$($download[0])" -OutFile $download[1] -MaximumRetryCount 3 -RetryIntervalSec 5
    }
    $checksumLine = @(Get-Content -LiteralPath $checksumFile -Encoding UTF8 | Where-Object { $_ -match ('\s\*?' + [regex]::Escape($ArchiveName) + '$') })
    if ($checksumLine.Count -ne 1) { throw '无法确认工具校验值' }
    $expected = ($checksumLine[0] -split '\s+')[0]
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw 'CI 工具下载校验失败' }
    $toolDir = Join-Path $env:RUNNER_TEMP ($Repository.Split('/')[-1] + '-' + $Tag)
    Expand-Archive -LiteralPath $archive -DestinationPath $toolDir -Force
    return Join-Path $toolDir $BinaryName
}
