import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, describe, expect, it } from 'vitest';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const script = join(root, 'scripts', 'check-initial-js-budget.mjs');
const tempDirs: string[] = [];

function writeFixtureFile(distDir: string, relativePath: string, content: string): void {
  const path = join(distDir, relativePath);
  writeFileSync(path, content, 'utf8');
}

function createDistFixture(): string {
  const distDir = mkdtempSync(join(tmpdir(), 'ahadiff-budget-'));
  tempDirs.push(distDir);
  const assetsDir = join(distDir, 'assets');
  const viteDir = join(distDir, '.vite');
  mkdirSync(assetsDir);
  mkdirSync(viteDir);
  writeFileSync(join(distDir, 'index.html'), '', 'utf8');
  writeFileSync(join(distDir, 'index.html'), [
    '<script type="module" src="/assets/index.js"></script>',
    '<link rel="modulepreload" href="/assets/vendor.js">',
  ].join('\n'), 'utf8');
  writeFixtureFile(distDir, 'assets/index.js', 'console.log("index");');
  writeFixtureFile(distDir, 'assets/vendor.js', 'console.log("vendor");');
  writeFixtureFile(distDir, 'assets/growth.js', 'console.log("growth");');
  writeFixtureFile(distDir, 'assets/graph.js', 'console.log("graph");');
  writeFixtureFile(
    distDir,
    '.vite/manifest.json',
    JSON.stringify({
      '_vendor.js': { file: 'assets/vendor.js' },
      '_graph.js': { file: 'assets/graph.js' },
      'src/pages/GrowthPage.tsx': {
        src: 'src/pages/GrowthPage.tsx',
        file: 'assets/growth.js',
        imports: ['_graph.js', '_vendor.js'],
      },
    }),
  );
  return distDir;
}

describe('check-initial-js-budget script', () => {
  afterEach(() => {
    for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
  });

  it('measures initial and Growth route bytes without enforcing a cap', () => {
    const distDir = createDistFixture();
    const output = execFileSync(process.execPath, [script], {
      cwd: root,
      encoding: 'utf8',
      env: {
        ...process.env,
        AHADIFF_BUDGET_DIST_DIR: distDir,
      },
    });

    expect(output).toContain('initial-js-gzip total:');
    expect(output).toContain('growth-first-route-js-gzip ./assets/growth.js');
    expect(output).toContain('growth-first-route-js-gzip ./assets/graph.js');
    expect(output).toContain('growth-first-route-js-gzip total:');
    expect(output).toContain('(observed, no budget cap)');
    expect(output.match(/growth-first-route-js-gzip \.\/assets\/vendor\.js/g)).toHaveLength(1);
  });
});
