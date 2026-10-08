import { useCallback, useEffect, useState } from 'react';
import GrowthShell from '../growth/GrowthShell';
import ProviderCard from '../components/ProviderCard';
import { getConfig, getProviders, putConfig, type ConfigResponse,
  type ProviderSummary } from '../api/config';
import { createProvider, deleteProvider, probeProvider, updateProvider } from '../api/providers';
import type { ProviderCreateInput, ProviderUpdateInput } from '../api/types';
import { useTranslation } from '../i18n/useTranslation';
import './GrowthSettingsPage.css';

const NEW_PROVIDER: ProviderSummary = {
  alias: '',
  provider_class: 'openai',
  provider_kind: 'openai',
  model_name: '',
  base_url: '',
  api_key_env: null,
  key_status: 'unknown',
  probed: false,
  probed_max_context: null,
};

const PRIVACY_MODES = [
  ['strict_local', 'Settings_page.privacy_mode_strict_local'],
  ['redacted_remote', 'Settings_page.privacy_mode_redacted_remote'],
  ['explicit_remote', 'Settings_page.privacy_mode_explicit_remote'],
] as const;

export default function GrowthSettingsPage() {
  const { t } = useTranslation();
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [privacyMode, setPrivacyMode] = useState('strict_local');
  const [showNew, setShowNew] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    const [nextConfig, nextProviders] = await Promise.all([getConfig(), getProviders()]);
    setConfig(nextConfig);
    setProviders(nextProviders.providers);
    setPrivacyMode(nextConfig.privacy_mode ?? 'strict_local');
    setError('');
    setLoading(false);
  }, []);

  useEffect(() => {
    void load().catch(() => {
      setLoading(false);
      setError(t('Growth.settings_load_failed'));
    });
  }, [load, t]);

  async function saveProvider(alias: string, data: ProviderCreateInput | ProviderUpdateInput) {
    const result = 'alias' in data
      ? await createProvider(data) : await updateProvider(alias, data);
    setShowNew(false);
    await load();
    return result;
  }

  async function removeProvider(alias: string, scope?: 'repo' | 'global') {
    await deleteProvider(alias, { scope });
    await load();
  }

  async function savePrivacy() {
    setSaving(true);
    setError('');
    setMessage('');
    try {
      await putConfig({ privacy_mode: privacyMode });
      await load();
      setMessage(t('Growth.settings_saved'));
    } catch {
      setError(t('Growth.settings_save_failed'));
    } finally {
      setSaving(false);
    }
  }

  return <GrowthShell>
    <div className="growth-settings">
      <header>
        <h1>{t('Growth.view_settings')}</h1>
        <p>{t('Growth.settings_intro')}</p>
      </header>
      {loading && <p role="status">{t('A11y.loading')}</p>}
      {error && <p role="alert" className="growth-settings__error">{error}</p>}
      {!loading && <>
        <section className="growth-settings__panel" aria-labelledby="growth-settings-provider">
          <div className="growth-settings__section-head">
            <div>
              <h2 id="growth-settings-provider">{t('Growth.settings_models')}</h2>
              <p>{t('Growth.settings_models_hint')}</p>
            </div>
            <button type="button" onClick={() => setShowNew(true)} disabled={showNew}>
              {t('Settings_page.provider_add')}
            </button>
          </div>
          {providers.length === 0 && !showNew && <p>{t('Growth.settings_no_provider')}</p>}
          {showNew && <ProviderCard provider={NEW_PROVIDER} isNew
            onSave={saveProvider} onDelete={removeProvider}
            onProbe={async (alias, scope) =>
              (await probeProvider(alias, undefined, { scope })).task_id ?? null}
            onCancelNew={() => setShowNew(false)} onRefresh={() => { void load(); }} />}
          {providers.map((provider) => <ProviderCard
            key={`${provider.scope ?? 'repo'}:${provider.alias}`} provider={provider}
            onSave={saveProvider} onDelete={removeProvider}
            onProbe={async (alias, scope) =>
              (await probeProvider(alias, undefined, { scope })).task_id ?? null}
            onRefresh={() => { void load(); }} />)}
        </section>
        <section className="growth-settings__panel" aria-labelledby="growth-settings-privacy">
          <h2 id="growth-settings-privacy">{t('Growth.settings_privacy')}</h2>
          <p>{t('Growth.settings_privacy_hint')}</p>
          <label htmlFor="growth-settings-privacy-mode">{t('Settings_page.privacy_mode')}</label>
          <select id="growth-settings-privacy-mode" value={privacyMode}
            onChange={(event) => { setPrivacyMode(event.target.value); setMessage(''); }}>
            {PRIVACY_MODES.map(([value, key]) => <option key={value} value={value}>
              {t(key)}</option>)}
          </select>
          <button type="button" onClick={() => { void savePrivacy(); }}
            disabled={saving || !config || privacyMode === (config.privacy_mode ?? 'strict_local')}>
            {t('Growth.settings_save')}
          </button>
          {message && <p role="status">{message}</p>}
        </section>
      </>}
    </div>
  </GrowthShell>;
}
