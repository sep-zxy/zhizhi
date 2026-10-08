import GrowthChatPanel from './GrowthChatPanel';
import GrowthReviewsPanel from './GrowthReviewsPanel';
import GrowthTopicsPanel from './GrowthTopicsPanel';
import { GrowthSyncNotes, GrowthSyncTasks, GrowthSourceExcerptApproval } from './GrowthSyncWorkspace';
import type { useGrowthWorkspace } from './useGrowthWorkspace';

export default function GrowthSettingsPanel({ workspace }: { workspace: ReturnType<typeof useGrowthWorkspace> }) {
  const { cachedAccounts, selectCachedAccount, t, view, state, projectId, busy, modelSources, cloudUrl, setCloudUrl, cloudToken, setCloudToken, syncSummary, setSyncSummary, syncPreview, setSyncPreview, syncApproved, setSyncApproved, syncError, syncBusy, cloudExport, setCloudExport, exportBusy, exportError, pendingExportId, eventInbox, eventId, setEventId, setEventBindingId, eventUntrackedPaths, eventSelectedUntracked, setEventSelectedUntracked, eventBusy, eventMessage, setEventMessage, eventError, setEventError, topicRefreshKey, setTopicRefreshKey, refreshHistory, setRefreshHistory, refresh, selectedEvent, selectedEventBindingId, selectedSnapshotId, captureEvent, submitSync, prepareCloudExport, downloadCloudExport, previewSync } = workspace;
  return <>{((view === 'all' || view === 'settings') || view === 'settings' || view === 'topics') && <section
          className="growth-page__panel growth-page__sync-panel" aria-labelledby="growth-sync-title">
          {cachedAccounts.length > 1 && <label>本机缓存账号<select aria-label="本机缓存账号" value={syncSummary?.account_id ?? ''} onChange={(event) => selectCachedAccount(event.target.value)}><option value="">选择要离线查看的账号</option>{cachedAccounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.account_id} · {account.cloud_origin}</option>)}</select></label>}
          <details>
            <summary id="growth-sync-title">{t('Growth.sync_title')}</summary>
            <p className="growth-page__hint">{t('Growth.sync_description')}</p>
          <p>{t('Growth.sync_selected_project')}: {
            state?.projects.find((item) => item.project_id === projectId)?.name ?? '—'
          }</p>
          <button type="button" disabled={busy || syncBusy || !projectId}
            onClick={() => { void previewSync(); }}>{t('Growth.sync_preview')}</button>
          {syncPreview?.project_id === projectId && <>
            <p>{t('Growth.sync_preview_count', { count: syncPreview.count })}</p>
            <details>
              <summary>{t('Growth.sync_preview_details')}</summary>
              <pre style={{ overflowX: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                {JSON.stringify(syncPreview.operations, null, 2)}
              </pre>
            </details>
            <label>
              <input type="checkbox" checked={syncApproved}
                onChange={(event) => setSyncApproved(event.target.checked)} />
              {t('Growth.sync_approve')}
            </label>
            <button type="button" onClick={() => { setSyncPreview(null); setSyncApproved(false); }}>
              {t('Growth.sync_cancel_preview')}
            </button>
          </>}
          <form onSubmit={(event) => { void submitSync(event); }} className="growth-page__form">
            <label htmlFor="growth-cloud-url">{t('Growth.cloud_url')}</label>
            <input id="growth-cloud-url" type="url" value={cloudUrl}
              onChange={(event) => { setCloudUrl(event.target.value);
                setSyncSummary(null); setCloudExport(null); pendingExportId.current = null; }}
              autoComplete="url" maxLength={2048} disabled={busy} required />
            <label htmlFor="growth-cloud-token">{t('Growth.cloud_token')}</label>
            <input id="growth-cloud-token" type="password" value={cloudToken}
              onChange={(event) => { setCloudToken(event.target.value);
                setSyncSummary(null); setCloudExport(null); pendingExportId.current = null; }}
              autoComplete="off" spellCheck={false} disabled={busy} required />
            <label className="growth-page__approval"><input type="checkbox"
              checked={refreshHistory}
              onChange={(event) => setRefreshHistory(event.target.checked)}
              disabled={busy} />{t('Growth.sync_refresh_history')}</label>
            <button disabled={busy || syncBusy || !cloudUrl.trim() || !cloudToken.trim()
              || (syncPreview?.project_id === projectId && !syncApproved)}>
              {syncBusy ? t('Growth.sync_running') : t('Growth.sync_now')}
            </button>
          </form>
          {syncError && <p className="growth-page__error" role="alert">{syncError}</p>}
          {syncSummary && <p role="status">{t('Growth.sync_outbox_status', {
            queued: syncSummary.queued,
            pending: syncSummary.outbox.pending,
            uploaded: syncSummary.uploaded,
            pulled: syncSummary.pulled,
            conflict: syncSummary.outbox.conflict,
            deleted: syncSummary.outbox.deleted,
          })}</p>}
          </details>
          {(view === 'all' || view === 'settings') && <div className="growth-page__sync-notes">
            <h3>{t('Growth.export_title')}</h3>
            <p className="growth-page__hint">{t('Growth.export_hint')}</p>
            <button type="button" disabled={!syncSummary || busy || syncBusy || exportBusy}
              onClick={() => { void prepareCloudExport(); }}>
              {exportBusy ? t('Growth.export_preparing') : t('Growth.export_prepare')}
            </button>
            {exportError && <p className="growth-page__error" role="alert">{exportError}</p>}
            {cloudExport && <div role="status">
              <p>{t('Growth.export_ready')}: <code>{cloudExport.export_id}</code></p>
              <button type="button" onClick={() => downloadCloudExport('json')}>
                {t('Growth.export_json')}
              </button>
              <button type="button" onClick={() => downloadCloudExport('markdown')}>
                {t('Growth.export_markdown')}
              </button>
            </div>}
          </div>}
          {syncSummary && ((view === 'all' || view === 'settings')) && <div className="growth-page__sync-notes">
            <h3>{t('Growth.event_inbox_title')}</h3>
            <p className="growth-page__hint">{t('Growth.event_inbox_hint')}</p>
            {eventInbox.length === 0 ? <p>{t('Growth.event_inbox_empty')}</p> : <>
              <label htmlFor="growth-event-select">{t('Growth.event_select')}</label>
              <select id="growth-event-select" value={eventId} onChange={(event) => {
                setEventId(event.target.value);
                setEventBindingId('');
                setEventMessage('');
                setEventError('');
              }}>
                <option value="">{t('Growth.event_select_placeholder')}</option>
                {eventInbox.map((item) => <option key={item.event_id} value={item.event_id}>
                  {item.event_id.slice(0, 8)} · {item.expected_head_sha.slice(0, 12)}
                  {' · '}{item.status}
                </option>)}
              </select>
              {selectedEvent && <>
                <p>{t('Growth.event_expected_head')}: <code>{selectedEvent.expected_head_sha}</code></p>
                {selectedEvent.status !== 'targeted' &&
                  <p role="status">{t('Growth.event_pending_target')}</p>}
                {selectedEvent.bindings.length === 0 &&
                  <p role="status">{t('Growth.event_bind_first')}</p>}
                {selectedEvent.bindings.length > 0 && <>
                  <label htmlFor="growth-event-binding">{t('Growth.event_binding')}</label>
                  <select id="growth-event-binding" value={selectedEventBindingId}
                    onChange={(event) => setEventBindingId(event.target.value)}>
                    {selectedEvent.bindings.map((binding) => <option
                      key={binding.binding_id} value={binding.binding_id}>
                      {binding.canonical_local_path}
                    </option>)}
                  </select>
                  {eventUntrackedPaths.length > 0 && <fieldset className="growth-page__untracked">
                    <legend>{t('Growth.event_untracked')}</legend>
                    {eventUntrackedPaths.map((path) => <label key={path}>
                      <input type="checkbox" checked={eventSelectedUntracked.includes(path)}
                        onChange={(event) => setEventSelectedUntracked((current) =>
                          event.target.checked ? [...current, path]
                            : current.filter((item) => item !== path))} />
                      <code>{path}</code>
                    </label>)}
                  </fieldset>}
                  <button type="button" disabled={busy || eventBusy
                    || selectedEvent.status !== 'targeted' || !selectedEvent.feature_id}
                    onClick={() => { void captureEvent(); }}>
                    {eventBusy ? t('Growth.event_capturing') : t('Growth.event_capture')}
                  </button>
                </>}
                {selectedEvent.capture && <p>{t('Growth.event_snapshot')}:
                  {' '}<code>{selectedEvent.capture.snapshot_id}</code></p>}
              </>}
            </>}
            {eventError && <p className="growth-page__error" role="alert">{eventError}</p>}
            {eventMessage && <p role="status">{eventMessage}</p>}
          </div>}
          {syncSummary && ((view === 'all' || view === 'settings') || view === 'topics') && <GrowthSyncNotes key={`notes:${syncSummary.account_id}`}
            refreshKey={syncSummary} disabled={busy || syncBusy} />}
          {(view === 'all' || view === 'settings') && syncSummary && modelSources &&
            <GrowthSourceExcerptApproval key={`excerpt:${selectedSnapshotId}`}
              accountId={syncSummary.account_id} sources={modelSources}
              disabled={busy || syncBusy} />}
          {(view === 'all' || view === 'settings') && syncSummary && <GrowthSyncTasks key={`tasks:${syncSummary.account_id}`}
            refreshKey={syncSummary} disabled={busy || syncBusy}
            bindings={state?.bindings ?? []}
            onBindingAdded={refresh}
            access={syncSummary && cloudUrl.trim() && cloudToken.trim() ? {
              cloud_url: cloudUrl.trim(), access_token: cloudToken.trim(),
              account_id: syncSummary.account_id,
            } : null} />}
          {(view === 'all' || view === 'settings') && syncSummary && <GrowthReviewsPanel
            access={syncSummary && cloudUrl.trim() && cloudToken.trim() ? {
              cloud_url: cloudUrl.trim(), access_token: cloudToken.trim(),
              account_id: syncSummary.account_id,
            } : null}
            disabled={busy || syncBusy} />}
          {syncSummary && ((view === 'all' || view === 'settings') || view === 'topics') && <GrowthChatPanel
            access={syncSummary && cloudUrl.trim() && cloudToken.trim() ? {
              cloud_url: cloudUrl.trim(), access_token: cloudToken.trim(),
              account_id: syncSummary.account_id,
            } : null}
            projectId={projectId || null} disabled={busy || syncBusy}
            onTopicConfirmed={() => setTopicRefreshKey((value) => value + 1)} />}
          {syncSummary && ((view === 'all' || view === 'settings') || view === 'topics') && <GrowthTopicsPanel
            access={syncSummary && cloudUrl.trim() && cloudToken.trim() ? {
              cloud_url: cloudUrl.trim(), access_token: cloudToken.trim(),
              account_id: syncSummary.account_id,
            } : null}
            disabled={busy || syncBusy} refreshKey={topicRefreshKey} />}
        </section>}</>;
}
