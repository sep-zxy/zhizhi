import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  PLATFORMS, parseChecksumManifest, validatePackageEntries, validateBuildInfo, validateBuildRun,
  validateJobs, validateMainMembership, validatePublishContext, validateReleaseState,
} from './release-gates.mjs';

function gh(...args) {
  return execFileSync('gh', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}
function api(endpoint) {
  return JSON.parse(gh('api', endpoint));
}
function optionalApi(endpoint) {
  try {
    return api(endpoint);
  } catch (error) {
    if (error.status === 1 && /\(HTTP 404\)/.test(String(error.stderr))) return null;
    throw error;
  }
}
function write(path, value) {
  writeFileSync(path, value, 'utf8');
}

const { GITHUB_REF: ref, GITHUB_REPOSITORY: repository, GITHUB_REPOSITORY_ID: repositoryId,
  RELEASE_BUILD_RUN: runId, RELEASE_VERSION: version } = process.env;
assert.equal(process.env.GITHUB_ACTIONS, 'true', '验证只能在 GitHub Actions 上运行');
assert.equal(process.env.RUNNER_ENVIRONMENT, 'github-hosted', '拒绝自托管 runner');
validatePublishContext({ ref, version });
assert.match(runId ?? '', /^\d+$/, '无效的运行编号');
const run = api(`repos/${repository}/actions/runs/${runId}`);
validateBuildRun(run, { repository, repositoryId });
function fetchJobs(filter) {
  const jobs = [];
  for (let page = 1; ; page++) {
    const result = api(`repos/${repository}/actions/runs/${runId}/jobs?filter=${filter}&per_page=100&page=${page}`);
    jobs.push(...result.jobs);
    if (jobs.length >= result.total_count) return jobs;
  }
}
const jobs = fetchJobs('latest');
validateJobs(jobs);
const jobHistory = run.run_attempt === 1 ? jobs : fetchJobs('all');
validateMainMembership(api(`repos/${repository}/compare/${run.head_sha}...main`), run.head_sha);
const source = api(`repos/${repository}/contents/desktop/package.json?ref=${run.head_sha}`);
const packageInfo = JSON.parse(Buffer.from(source.content, 'base64').toString('utf8'));
assert.equal(packageInfo.version, version, '发布版本与源码版本不匹配');

// Dry runs must reject the same existing-release/tag conflicts as publication.
// The write job checks again immediately before publishing to avoid stale state.
const tag = `v${version}`;
const existingTag = optionalApi(`repos/${repository}/git/ref/tags/${tag}`);
let tagTarget = existingTag?.object;
for (let depth = 0; tagTarget?.type === 'tag' && depth < 10; depth++) {
  tagTarget = api(`repos/${repository}/git/tags/${tagTarget.sha}`).object;
}
validateReleaseState(optionalApi(`repos/${repository}/releases/tags/${tag}`), tagTarget, run.head_sha);

mkdirSync('packages', { recursive: true });
const records = [];
const checksums = [];
for (const platform of PLATFORMS) {
  const dir = join('packages', platform);
  gh('run', 'download', runId, '--repo', repository, '--name', `zhizhi-${platform}`, '--dir', dir);
  const info = JSON.parse(readFileSync(join(dir, 'build-info.json'), 'utf8').replace(/^\uFEFF/, ''));
  validateBuildInfo(info, run, platform, version, jobHistory);
  validatePackageEntries(readdirSync(dir), platform, version);
  const line = readFileSync(join(dir, 'SHA256SUMS.txt'), 'utf8').replace(/^\uFEFF/, '').trim();
  const { filename, sha256 } = parseChecksumManifest(line, platform, version);
  const actual = createHash('sha256').update(readFileSync(join(dir, filename))).digest('hex');
  assert.equal(actual, sha256, '安装包哈希不匹配');
  checksums.push(line);
  records.push(info);
}
write('packages/SHA256SUMS.txt', checksums.join('\n') + '\n');
write('packages/build-info.json', JSON.stringify({ commit: run.head_sha, run_url: run.html_url, platforms: records }, null, 2) + '\n');
write('packages/source-commit.txt', run.head_sha + '\n');
if (process.env.GITHUB_OUTPUT) writeFileSync(process.env.GITHUB_OUTPUT, `commit=${run.head_sha}\n`, { encoding: 'utf8', flag: 'a' });
console.log(`Release gate passed: ${run.head_sha}, run ${run.id}, attempt ${run.run_attempt}`);

