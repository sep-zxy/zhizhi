if (process.env.GITHUB_ACTIONS !== 'true' || process.env.RUNNER_ENVIRONMENT !== 'github-hosted') {
  console.error('构建仅允许在 GitHub 托管的 Actions runner 上执行。请触发 Desktop Build 工作流并下载产物。');
  process.exit(1);
}
