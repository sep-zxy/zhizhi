import { useEffect, useRef, useState } from 'react';
import {
  editGrowthCloudModule,
  listGrowthCloudModules,
  mapGrowthCloudModule,
  type GrowthCloudAccess,
  type GrowthCloudModule,
  type GrowthCodeIndex,
  type GrowthModuleMapOperation,
} from '../api/growth';
import { useTranslation } from '../i18n/useTranslation';

interface Props {
  access: GrowthCloudAccess | null;
  projectId: string;
  bindingId: string;
  snapshotId: string;
  codeIndex: GrowthCodeIndex | null;
  onModulesChange?: (modules: GrowthCloudModule[]) => void;
}

export default function GrowthModulesPanel({ access, projectId, bindingId,
  snapshotId, codeIndex, onModulesChange }: Props) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [modules, setModules] = useState<GrowthCloudModule[]>([]);
  const [selectedModuleId, setSelectedModuleId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const pendingMap = useRef<GrowthModuleMapOperation | null>(null);
  const pendingReindex = useRef<GrowthModuleMapOperation | null>(null);
  const pendingLock = useRef<{ operation_id: string; base_revision: number;
    name: string; locked: boolean; member_paths: string[]; resolve_pending: boolean } | null>(null);
  const pendingResolve = useRef<typeof pendingLock.current>(null);

  useEffect(() => {
    pendingMap.current = null;
    pendingReindex.current = null;
    pendingLock.current = null;
    pendingResolve.current = null;
    setSelectedModuleId('');
    setModules([]);
    onModulesChange?.([]);
    setError('');
    setMessage('');
  }, [projectId, snapshotId, codeIndex?.index_revision, onModulesChange]);

  useEffect(() => {
    if (!access || !projectId) return;
    let active = true;
    void listGrowthCloudModules(access, projectId).then((result) => {
      if (active) { setModules(result.modules); onModulesChange?.(result.modules); }
    }).catch(() => { if (active) setError(t('Growth.module_list_failed')); });
    return () => { active = false; };
  }, [access?.cloud_url, access?.account_id, access?.access_token, projectId,
    snapshotId, codeIndex?.index_revision, t, onModulesChange]);

  const selected = modules.find((item) => item.module_id === selectedModuleId);
  const indexReady = codeIndex?.snapshot_id === snapshotId;
  const indexedPaths = codeIndex
    ? [...new Set(codeIndex.flow.nodes.map((node) => node.file_path))] : [];
  const canReindex = !!selected && indexReady && !selected.pending_adjustment
    && (selected.snapshot_id !== snapshotId
      || selected.index_revision !== codeIndex?.index_revision)
    && selected.member_paths.some((path) => indexedPaths.includes(path));

  async function refreshModules() {
    if (!access) return;
    const result = await listGrowthCloudModules(access, projectId);
    setModules(result.modules);
    onModulesChange?.(result.modules);
  }

  async function mapModule() {
    if (!access || !codeIndex || codeIndex.snapshot_id !== snapshotId || !name.trim()) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      if (!pendingMap.current) {
        pendingMap.current = {
          operation_id: crypto.randomUUID(), module_id: crypto.randomUUID(),
          project_id: projectId, snapshot_id: snapshotId,
          index_revision: codeIndex.index_revision,
          effective_tree_hash: codeIndex.effective_tree_hash,
          suggested_name: name.trim(),
          member_paths: [...new Set(codeIndex.flow.nodes.map((node) => node.file_path))],
          flow: {
            ...codeIndex.flow, snapshot_id: snapshotId,
            index_revision: codeIndex.index_revision,
            effective_tree_hash: codeIndex.effective_tree_hash,
          },
          base_revision: null,
        };
      }
      const result = await mapGrowthCloudModule(access, bindingId, pendingMap.current);
      await refreshModules();
      setSelectedModuleId(result.module_id);
      setMessage(t('Growth.module_mapped'));
      pendingMap.current = null;
    } catch {
      setError(t('Growth.module_map_failed'));
    } finally {
      setBusy(false);
    }
  }

  async function lockModule() {
    if (!access || !selected || !name.trim() || selected.locked) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      if (!pendingLock.current) {
        pendingLock.current = {
          operation_id: crypto.randomUUID(), base_revision: selected.revision,
          name: name.trim(), locked: true,
          member_paths: selected.member_paths, resolve_pending: false,
        };
      }
      await editGrowthCloudModule(access, selected.module_id, pendingLock.current);
      await refreshModules();
      setMessage(t('Growth.module_locked'));
      pendingLock.current = null;
    } catch {
      setError(t('Growth.module_lock_failed'));
    } finally {
      setBusy(false);
    }
  }

  async function reindexModule() {
    if (!access || !selected || !codeIndex || !canReindex) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      if (!pendingReindex.current) {
        pendingReindex.current = {
          operation_id: crypto.randomUUID(), module_id: selected.module_id,
          project_id: projectId, snapshot_id: snapshotId,
          index_revision: codeIndex.index_revision,
          effective_tree_hash: codeIndex.effective_tree_hash,
          suggested_name: selected.name,
          member_paths: indexedPaths,
          flow: {
            ...codeIndex.flow, snapshot_id: snapshotId,
            index_revision: codeIndex.index_revision,
            effective_tree_hash: codeIndex.effective_tree_hash,
          },
          base_revision: selected.revision,
        };
      }
      const result = await mapGrowthCloudModule(access, bindingId, pendingReindex.current);
      await refreshModules();
      setMessage(t(result.pending_adjustment
        ? 'Growth.module_adjustment_proposed' : 'Growth.module_reindexed'));
      pendingReindex.current = null;
    } catch {
      setError(t('Growth.module_reindex_failed'));
    } finally {
      setBusy(false);
    }
  }

  async function resolveAdjustment() {
    if (!access || !selected?.pending_adjustment) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      if (!pendingResolve.current) {
        pendingResolve.current = {
          operation_id: crypto.randomUUID(), base_revision: selected.revision,
          name: selected.name, locked: selected.locked,
          member_paths: selected.pending_adjustment.member_paths,
          resolve_pending: true,
        };
      }
      await editGrowthCloudModule(access, selected.module_id, pendingResolve.current);
      await refreshModules();
      setMessage(t('Growth.module_adjustment_accepted'));
      pendingResolve.current = null;
    } catch {
      setError(t('Growth.module_adjustment_failed'));
    } finally {
      setBusy(false);
    }
  }

  return <div className="growth-page__code-index" aria-labelledby="growth-module-title">
    <h3 id="growth-module-title">{t('Growth.module_title')}</h3>
    <p>{t('Growth.module_description')}</p>
    {!access && <p role="status">{t('Growth.module_cloud_required')}</p>}
    {access && <>
      <label htmlFor="growth-module-name">{t('Growth.module_name')}</label>
      <input id="growth-module-name" value={name} maxLength={200}
        onChange={(event) => {
          setName(event.target.value);
          pendingMap.current = null;
          pendingLock.current = null;
        }} />
      <button disabled={busy || !!selected || !codeIndex
        || codeIndex.snapshot_id !== snapshotId || !name.trim()}
        onClick={() => { void mapModule(); }}>{t('Growth.module_map')}</button>
      {modules.length > 0 && <>
        <label htmlFor="growth-module-select">{t('Growth.module_selected')}</label>
        <select id="growth-module-select" value={selectedModuleId}
          onChange={(event) => {
            setSelectedModuleId(event.target.value);
            setName(modules.find((item) => item.module_id === event.target.value)?.name ?? '');
            pendingLock.current = null;
            pendingReindex.current = null;
            pendingResolve.current = null;
          }}>
          <option value="">{t('Growth.module_select')}</option>
          {modules.map((item) => <option key={item.module_id} value={item.module_id}>
              {item.name} · {item.locked ? t('Growth.module_status_locked')
              : t('Growth.module_status_draft')} · {item.snapshot_id.slice(0, 8)}
          </option>)}
        </select>
      </>}
      {selected && <button disabled={busy || selected.locked || !name.trim()}
        onClick={() => { void lockModule(); }}>{t('Growth.module_lock')}</button>}
      {selected && <button disabled={busy || !canReindex}
        onClick={() => { void reindexModule(); }}>{t('Growth.module_reindex')}</button>}
      {selected?.pending_adjustment && <div>
        <p>{t('Growth.module_adjustment_review')}: <code>{
          selected.pending_adjustment.index_revision.slice(0, 12)
        }</code></p>
        <ul>{selected.pending_adjustment.member_paths.map((item) =>
          <li key={item}>{item}</li>)}</ul>
        <button disabled={busy} onClick={() => { void resolveAdjustment(); }}>
          {t('Growth.module_adjustment_accept')}
        </button>
      </div>}
      {message && <p role="status">{message}</p>}
      {error && <p role="alert" className="growth-page__error">{error}</p>}
    </>}
  </div>;
}
