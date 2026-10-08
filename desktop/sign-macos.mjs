import { createRequire } from 'node:module';
import '../scripts/require-github-actions.mjs';

// Reuse the signer already pinned by electron-builder, including its per-file
// entitlements. The builder version treats '-' as a certificate name otherwise.
const require = createRequire(import.meta.url);
const builderRequire = createRequire(require.resolve('electron-builder'));
const appBuilderRequire = createRequire(builderRequire.resolve('app-builder-lib'));
const { signAsync } = appBuilderRequire('@electron/osx-sign');

export default async function signMacOS(options) {
  await signAsync({ ...options, identity: '-', identityValidation: false });
}
