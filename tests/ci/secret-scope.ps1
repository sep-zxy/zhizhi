$ErrorActionPreference = 'Stop'

# Exercise the real scanner on a disposable Git history inside the hosted runner.
# No fixture is pushed, and complete credential-shaped strings are never stored
# in this source file or printed in the log.
. (Join-Path $PSScriptRoot '../../scripts/ci/download-tool.ps1')
$tool = Get-VerifiedReleaseTool -Repository 'gitleaks/gitleaks' -Tag 'v8.30.1' -ArchiveName 'gitleaks_8.30.1_windows_x64.zip' -ChecksumsName 'gitleaks_8.30.1_checksums.txt' -BinaryName 'gitleaks.exe'
$fixtureRepo = Join-Path $env:RUNNER_TEMP ('secret-scope-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixtureRepo | Out-Null

function Invoke-FixtureGit {
    param([Parameter(ValueFromRemainingArguments)][string[]]$GitArguments)
    git -C $fixtureRepo @GitArguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '创建扫描范围验证仓库失败' }
}

Invoke-FixtureGit init -b main
Invoke-FixtureGit config user.name '知枝 CI'
Invoke-FixtureGit config user.email 'ci@example.invalid'
Set-Content -LiteralPath (Join-Path $fixtureRepo 'README.md') -Value 'Harmless main history' -Encoding UTF8
Invoke-FixtureGit add README.md
Invoke-FixtureGit commit -m '创建扫描范围验证样例'
Invoke-FixtureGit switch -c unrelated
$seed = [Text.Encoding]::UTF8.GetBytes('zhizhi scope regression fixture')
$body = [Convert]::ToBase64String([Security.Cryptography.SHA256]::HashData($seed)) -replace '[+/=]', 'A'
$fakeToken = 'ghp_' + $body.Substring(0, 36)
Set-Content -LiteralPath (Join-Path $fixtureRepo 'fixture.txt') -Value ('token=' + $fakeToken) -Encoding UTF8
Invoke-FixtureGit add fixture.txt
Invoke-FixtureGit commit -m '创建无关分支的伪造凭证样例'
Invoke-FixtureGit switch main

Push-Location -LiteralPath $fixtureRepo
try {
    & $tool git --no-banner --redact=100 --log-opts=HEAD
    if ($LASTEXITCODE -ne 0) { throw '当前提交扫描错误地包含了无关分支' }
    $report = Join-Path $env:RUNNER_TEMP 'secret-scope-redacted.json'
    & $tool git --no-banner --redact=100 --log-opts=--all --report-format=json "--report-path=$report"
    if ($LASTEXITCODE -ne 1) { throw '全引用审计未检测到伪造凭证样例' }
    $findings = @(Get-Content -LiteralPath $report -Encoding UTF8 -Raw | ConvertFrom-Json)
    if ($findings.Count -ne 1 -or $findings[0].File -ne 'fixture.txt') { throw '扫描范围验证结果不匹配' }
    $status = git status --porcelain
    if ($LASTEXITCODE -ne 0 -or $status) { throw '扫描范围验证仓库状态不正确' }
    'Secret scope regression passed: HEAD is clean; all refs detects the synthetic fixture.'
}
finally {
    Pop-Location
}
