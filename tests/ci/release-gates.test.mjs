import assert from 'node:assert/strict';
import test from 'node:test';
import {
  CI_WORKFLOW, PLATFORMS, expectedPackageName, parseChecksumManifest, validatePackageEntries, validateBuildInfo, validateBuildRun,
  validateJobs, validateMainMembership, validatePublishContext, validateReleaseState,
} from '../../scripts/ci/release-gates.mjs';

const sha = 'a'.repeat(40);
const identity = { repository: 'example/project', repositoryId: 123 };
const run = {
  id: 1234, run_attempt: 2, path: CI_WORKFLOW, event: 'push', head_branch: 'main',
  status: 'completed', conclusion: 'success', head_sha: sha,
  repository: { full_name: identity.repository, id: identity.repositoryId },
  head_repository: { full_name: identity.repository },
};
const jobs = ['secrets', 'static', 'configuration', 'CI Gate',
  'desktop / Package (windows-x64)', 'desktop / Package (macos-arm64)']
  .map(name => ({ name, status: 'completed', conclusion: 'success', run_id: run.id, run_attempt: 2, head_sha: sha }));
const info = {
  commit: sha, build_run_id: '1234', build_run_attempt: '2', platform: 'windows-x64',
  desktop_version: '0.1.1', startup: 'desktop-load-pass', icon_sha256: 'b'.repeat(64),
};

test('accepts a complete main CI, both platforms, source ancestry and provenance', () => {
  validatePublishContext({ ref: 'refs/heads/main', version: '0.1.1' });
  validateBuildRun(run, identity);
  validateJobs(jobs);
  for (const status of ['ahead', 'identical']) validateMainMembership({ status, merge_base_commit: { sha } }, sha);
  validateBuildInfo(info, run, 'windows-x64', '0.1.1', jobs);
  validateBuildInfo({ ...info, platform: 'macos-arm64', startup: 'sidecar-version-pass' }, run, 'macos-arm64', '0.1.1', jobs);
  assert.deepEqual(PLATFORMS.map(p => expectedPackageName(p, '0.1.1')), ['zhizhi-0.1.1-win-x64.exe', 'zhizhi-0.1.1-mac-arm64.dmg']);
});

for (const [label, change] of Object.entries({
  'PR preview': { event: 'pull_request' },
  'merge queue preview': { event: 'merge_group' },
  'manual build': { event: 'workflow_dispatch' },
  'feature branch': { head_branch: 'feature' },
  'packaging-only workflow': { path: '.github/workflows/desktop-build.yml' },
  'same workflow name at another path': { path: '.github/workflows/spoof.yml', name: 'Static Checks' },
  'failed checks': { conclusion: 'failure' },
  'cancelled CI': { conclusion: 'cancelled' },
  'unfinished CI': { status: 'in_progress' },
  'another repository': { repository: { ...run.repository, full_name: 'other/project' } },
  'same name with different repository ID': { repository: { ...run.repository, id: 456 } },
  'fork source': { head_repository: { full_name: 'fork/project' } },
  'invalid source SHA': { head_sha: 'main' },
  'missing run attempt': { run_attempt: undefined },
})) test(`rejects ${label}`, () => assert.throws(() => validateBuildRun({ ...run, ...change }, identity)));

for (const job of jobs) {
  test(`rejects missing job: ${job.name}`, () => assert.throws(() => validateJobs(jobs.filter(j => j !== job))));
  for (const conclusion of ['failure', 'cancelled', 'skipped']) {
    test(`rejects ${conclusion} job: ${job.name}`, () => assert.throws(() => validateJobs(jobs.map(j => j === job ? { ...j, conclusion } : j))));
  }
}
test('rejects duplicate required jobs', () => assert.throws(() => validateJobs([...jobs, jobs[0]])));
test('rejects a branch-triggered release', () => assert.throws(() => validatePublishContext({ ref: 'refs/heads/feature', version: '0.1.1' })));
test('rejects unsafe version input', () => assert.throws(() => validatePublishContext({ ref: 'refs/heads/main', version: '0.1.1/../x' })));
for (const status of ['behind', 'diverged']) test(`rejects ${status} main ancestry`, () => assert.throws(() => validateMainMembership({ status, merge_base_commit: { sha } }, sha)));
test('rejects the wrong merge base', () => assert.throws(() => validateMainMembership({ status: 'ahead', merge_base_commit: { sha: 'c'.repeat(40) } }, sha)));
for (const [field, value] of Object.entries({
  commit: 'c'.repeat(40), build_run_id: '4321', build_run_attempt: '1', platform: 'macos-arm64',
  desktop_version: '0.1.2', startup: 'failed', icon_sha256: '',
})) test(`rejects mismatched artifact ${field}`, () => assert.throws(() => validateBuildInfo({ ...info, [field]: value }, run, 'windows-x64', '0.1.1', jobs)));

const previousWindowsJob = { ...jobs.find(job => job.name.endsWith('Package (windows-x64)')), run_attempt: 1 };
test('accepts a successful same-commit platform artifact reused after a failed-job retry', () => {
  validateBuildInfo({ ...info, build_run_attempt: '1' }, run, 'windows-x64', '0.1.1', [...jobs, previousWindowsJob]);
});
for (const [field, value] of Object.entries({
  run_id: 4321, head_sha: 'c'.repeat(40), status: 'in_progress', conclusion: 'failure', name: 'desktop / Package (macos-arm64)',
})) test(`rejects reused artifacts with invalid historical ${field}`, () => {
  assert.throws(() => validateBuildInfo({ ...info, build_run_attempt: '1' }, run, 'windows-x64', '0.1.1', [...jobs, { ...previousWindowsJob, [field]: value }]));
});
for (const attempt of ['0', '3', 'not-an-attempt']) test(`rejects invalid artifact attempt ${attempt}`, () => {
  assert.throws(() => validateBuildInfo({ ...info, build_run_attempt: attempt }, run, 'windows-x64', '0.1.1', jobs));
});

const filename = expectedPackageName('windows-x64', '0.1.1');
const digest = 'd'.repeat(64);
test('accepts a complete package and UTF8 BOM checksum manifest', () => {
  validatePackageEntries(['SHA256SUMS.txt', filename, 'build-info.json'], 'windows-x64', '0.1.1');
  assert.deepEqual(parseChecksumManifest(`\uFEFF${digest}  ${filename}\r\n`, 'windows-x64', '0.1.1'), { sha256: digest, filename });
});
for (const badName of ['../escape.exe', 'zhizhi-0.1.2-win-x64.exe', 'zhizhi-0.1.1-mac-arm64.dmg']) {
  test(`rejects manifest filename ${badName}`, () => assert.throws(() => parseChecksumManifest(`${digest}  ${badName}`, 'windows-x64', '0.1.1')));
}
test('rejects multiple manifest entries', () => assert.throws(() => parseChecksumManifest(`${digest}  ${filename}\n${digest}  ${filename}`, 'windows-x64', '0.1.1')));
test('rejects a missing installer', () => assert.throws(() => validatePackageEntries(['SHA256SUMS.txt', 'build-info.json'], 'windows-x64', '0.1.1')));
test('rejects unexpected artifact files', () => assert.throws(() => validatePackageEntries(['SHA256SUMS.txt', 'build-info.json', filename, 'extra.exe'], 'windows-x64', '0.1.1')));

test('dry run accepts a new release or a matching resumable draft/tag', () => {
  validateReleaseState(null, undefined, sha);
  validateReleaseState({ draft: true, target_commitish: sha }, undefined, sha);
  validateReleaseState({ draft: true, target_commitish: sha }, { type: 'commit', sha }, sha);
});
test('dry run rejects an already published version', () => assert.throws(() => validateReleaseState({ draft: false, target_commitish: sha }, undefined, sha)));
test('dry run rejects a draft for another source commit', () => assert.throws(() => validateReleaseState({ draft: true, target_commitish: 'c'.repeat(40) }, undefined, sha)));
test('dry run rejects a tag for another commit', () => assert.throws(() => validateReleaseState(null, { type: 'commit', sha: 'c'.repeat(40) }, sha)));
test('dry run fails closed if a tag cannot be fully resolved', () => assert.throws(() => validateReleaseState(null, { type: 'tag', sha }, sha)));
test('dry run fails closed on incomplete release metadata', () => assert.throws(() => validateReleaseState({ target_commitish: sha }, undefined, sha)));
