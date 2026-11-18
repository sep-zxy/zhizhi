import assert from 'node:assert/strict';

export const CI_WORKFLOW = '.github/workflows/checks.yml';
export const PLATFORMS = ['windows-x64', 'macos-arm64'];

export function validatePublishContext({ ref, version }) {
  assert.equal(ref, 'refs/heads/main', '正式发布必须从 main 工作流触发');
  assert.match(version ?? '', /^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?$/, '无效的桌面版本');
}

export function validateBuildRun(run, { repository, repositoryId }) {
  assert.equal(run.repository?.full_name, repository, '构建不属于当前仓库');
  assert.equal(String(run.repository?.id), String(repositoryId), '仓库编号不匹配');
  assert.equal(run.head_repository?.full_name, repository, '拒绝 Fork 来源的构建');
  assert.equal(run.path, CI_WORKFLOW, '必须使用完整 CI 的构建产物');
  assert.equal(run.event, 'push', '拒绝 PR、手动构建或合并队列的预览产物');
  assert.equal(run.head_branch, 'main', '构建必须来自主分支');
  assert.equal(run.status, 'completed', '构建尚未完成');
  assert.equal(run.conclusion, 'success', 'CI 未全部通过');
  assert.match(run.head_sha ?? '', /^[0-9a-f]{40}$/, '无效的构建提交');
  assert.ok(Number.isSafeInteger(run.id) && run.id > 0, '无效的运行编号');
  assert.ok(Number.isSafeInteger(run.run_attempt) && run.run_attempt > 0, '无效的运行次数');
}

export function validateJobs(jobs) {
  const required = ['secrets', 'static', 'configuration', 'CI Gate'];
  for (const platform of PLATFORMS) required.push(`Package (${platform})`);
  for (const name of required) {
    const matches = jobs.filter(job => job.name === name || job.name.endsWith(` / ${name}`));
    assert.equal(matches.length, 1, `缺少或重复 CI 任务：${name}`);
    assert.equal(matches[0].status, 'completed', `任务尚未完成：${name}`);
    assert.equal(matches[0].conclusion, 'success', `任务未通过：${name}`);
  }
}

export function validateMainMembership(comparison, sha) {
  assert.ok(['ahead', 'identical'].includes(comparison.status), '构建提交尚未进入 main');
  assert.equal(comparison.merge_base_commit?.sha, sha, '构建提交不是 main 的祖先');
}

export function expectedPackageName(platform, version) {
  assert.ok(PLATFORMS.includes(platform), '未知的平台');
  return `zhizhi-${version}-${platform === 'windows-x64' ? 'win-x64.exe' : 'mac-arm64.dmg'}`;
}

export function validateBuildInfo(info, run, platform, version) {
  assert.equal(info.commit, run.head_sha, '产物源码提交不匹配');
  assert.equal(String(info.build_run_id), String(run.id), '产物来自其他 CI 运行');
  assert.equal(String(info.build_run_attempt), String(run.run_attempt), '产物来自旧的运行次数，请重跑全部 CI');
  assert.equal(info.platform, platform, '产物平台不匹配');
  assert.equal(info.desktop_version, version, '产物桌面版本不匹配');
  assert.equal(info.startup, platform === 'windows-x64' ? 'desktop-load-pass' : 'sidecar-version-pass', '启动检查未通过');
  assert.match(info.icon_sha256 ?? '', /^[0-9a-f]{64}$/, '缺少图标校验记录');
}

export function validatePackageEntries(entries, platform, version) {
  const filename = expectedPackageName(platform, version);
  assert.deepEqual([...entries].sort(), ['SHA256SUMS.txt', 'build-info.json', filename].sort(), '产物包含缺失或多余文件');
}

export function parseChecksumManifest(text, platform, version) {
  const match = text.replace(/^\uFEFF/, '').trim().match(/^([0-9a-f]{64}) {2}([^\r\n]+)$/);
  assert.ok(match && match[2] === expectedPackageName(platform, version), '校验清单或文件名错误');
  return { sha256: match[1], filename: match[2] };
}

