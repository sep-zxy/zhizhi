import GrowthModulesPanel from './GrowthModulesPanel';
import { parseCodeSymbols } from './useGrowthWorkspace';
import type { useGrowthWorkspace } from './useGrowthWorkspace';

export default function GrowthAnalysisPanel({ workspace }: { workspace: ReturnType<typeof useGrowthWorkspace> }) {
  const { legacyMode, t, showLearningAnalysis, state, projectId, busy, setSnapshotId, codeSymbols, setCodeSymbols, codeIndex, setCodeIndex, codeIndexBusy, codeIndexError, analysisTaskId, analysisMessage, analysisError, modelSources, selectedSourceIds, setSelectedSourceIds, providerName, setProviderName, modelPreview, setModelPreview, modelApproved, setModelApproved, modelError, setSelectedTaskId, cloudModules, setCloudModules, taskModuleIds, setTaskModuleIds, cloudUrl, cloudToken, syncSummary, cloudTopics, cloudTopicsReady, cloudTopicsError, topicReuseIds, setTopicReuseIds, eventAnalysisContext, selectedBindingId, selectedFeatureId, featureSnapshots, selectedSnapshotId, snapshotAnalyses, snapshotOpportunities, snapshotTasks, indexCode, analyze, previewModelRequest, analyzeLive, decideTopic, createTask } = workspace;
  return <>{showLearningAnalysis && selectedFeatureId && featureSnapshots.length > 0 && <section
          className="growth-page__panel" aria-labelledby="growth-analysis-title">
          <h2 id="growth-analysis-title">{t('Growth.analysis_title')}</h2>
          <p>{t('Growth.analysis_description')}</p>
          {eventAnalysisContext?.snapshot_id === selectedSnapshotId &&
            <p role="status">{t('Growth.event_analysis_context')}</p>}
          <label htmlFor="growth-snapshot-select">{t('Growth.selected_snapshot')}</label>
          <select id="growth-snapshot-select" value={selectedSnapshotId}
            onChange={(event) => setSnapshotId(event.target.value)}>
            {featureSnapshots.map((item) => <option key={item.snapshot_id} value={item.snapshot_id}>
              {item.snapshot_id.slice(0, 8)} · {item.head_sha.slice(0, 12)}
            </option>)}
          </select>
          <div className="growth-page__model">
            <h3>{t('Growth.model_title')}</h3>
            <p>{t('Growth.model_description')}</p>
            {modelError && <p className="growth-page__error" role="alert">{modelError}</p>}
            {modelSources && <>
              <label htmlFor="growth-provider-select">{t('Growth.provider')}</label>
              <select id="growth-provider-select" value={providerName} onChange={(event) => {
                setProviderName(event.target.value);
                setModelPreview(null);
                setModelApproved(false);
              }}>
                <option value="">{t('Growth.select_provider')}</option>
                {modelSources.provider_names.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              {modelSources.provider_names.length === 0 && <p>{t('Growth.no_provider')}</p>}
              <fieldset className="growth-page__untracked">
                <legend>{t('Growth.model_sources')}</legend>
                {modelSources.sources.map((source) => <label key={source.source_ref_id}>
                  <input type="checkbox" checked={selectedSourceIds.includes(source.source_ref_id)}
                    onChange={(event) => {
                      setSelectedSourceIds((current) => event.target.checked
                        ? [...current, source.source_ref_id]
                        : current.filter((item) => item !== source.source_ref_id));
                      setModelPreview(null);
                      setModelApproved(false);
                    }} />
                  <code>{source.relative_path}</code> ({source.byte_count} B)
                  {source.deleted === 1 && ` · ${t('Growth.deleted_source')}`}
                </label>)}
              </fieldset>
              <button disabled={busy || !providerName || selectedSourceIds.length === 0}
                onClick={() => { void previewModelRequest(); }}>
                {t('Growth.preview_model_request')}
              </button>
            </>}
            {modelPreview && <div>
              <p>{t('Growth.model_destination')}: <strong>{modelPreview.provider_host}</strong>
                {' · '}{modelPreview.model_name}</p>
              <p>{t('Growth.model_exact_payload')}</p>
              <pre className="growth-page__payload">{modelPreview.payload_text}</pre>
              <label className="growth-page__approval">
                <input type="checkbox" checked={modelApproved}
                  onChange={(event) => setModelApproved(event.target.checked)} />
                {t('Growth.approve_model_send')}
              </label>
              <button disabled={busy || !!analysisTaskId || !modelApproved}
                onClick={() => analyzeLive()}>{t('Growth.start_model_analysis')}</button>
              <button disabled={busy || !!analysisTaskId || !modelApproved
                || eventAnalysisContext?.snapshot_id === selectedSnapshotId}
                onClick={() => analyzeLive(true)}>
                {t('Growth.reanalyze_model')}
              </button>
            </div>}
          </div>
          <details className="growth-page__advanced">
            <summary>源码索引与本机诊断（可选）</summary>
          <div className="growth-page__code-index" aria-labelledby="growth-code-index-title">
            <h3 id="growth-code-index-title">{t('Growth.code_index_title')}</h3>
            <p>{t('Growth.code_index_description')}</p>
            <label htmlFor="growth-code-symbols">{t('Growth.code_index_symbols')}</label>
            <input id="growth-code-symbols" value={codeSymbols}
              onChange={(event) => { setCodeSymbols(event.target.value); setCodeIndex(null); }}
              placeholder={t('Growth.code_index_example')} />
            <button disabled={codeIndexBusy || parseCodeSymbols(codeSymbols).length < 2}
              onClick={() => { void indexCode(); }}>
              {codeIndexBusy ? t('Growth.code_index_running') : t('Growth.code_index_start')}
            </button>
            {codeIndexError && <p role="alert" className="growth-page__error">{codeIndexError}</p>}
            {codeIndex && <div role="status" className="growth-page__capture">
              <p>{t('Growth.code_index_revision')}: <code>{codeIndex.index_revision}</code></p>
              <ol>{codeIndex.flow.nodes.map((node) => <li key={node.symbol}>
                <strong>{node.symbol}</strong> · <code>{node.file_path}:{node.start_line}</code>
              </li>)}</ol>
              <ul>{codeIndex.flow.edges.map((edge) => <li key={`${edge.from}:${edge.to}`}>
                {edge.from} → {edge.to}: {t(edge.status === 'indexed'
                  ? 'Growth.code_index_supported' : 'Growth.code_index_unknown')}
              </li>)}</ul>
            </div>}
          </div>
          <GrowthModulesPanel
            access={syncSummary && cloudUrl.trim() && cloudToken.trim() ? {
              cloud_url: cloudUrl.trim(), access_token: cloudToken.trim(),
              account_id: syncSummary.account_id,
            } : null}
            projectId={projectId} bindingId={selectedBindingId}
            snapshotId={selectedSnapshotId} codeIndex={codeIndex}
            onModulesChange={setCloudModules} />
          <button onClick={() => analyze()} disabled={busy || !!analysisTaskId
            || eventAnalysisContext?.snapshot_id === selectedSnapshotId}>
            {t('Growth.start_analysis')}
          </button>
          </details>
          {analysisError && <p className="growth-page__error" role="alert">{analysisError}</p>}
          {analysisMessage && <p role="status" aria-live="polite">{analysisMessage}</p>}
          {snapshotAnalyses.length > 0 && <ul className="growth-page__analyses">
            {snapshotAnalyses.map((item) => <li key={item.analysis_id}>
              <span>{item.mode === 'live' ? t('Growth.analysis_live')
                : t('Growth.analysis_dry_run')} · {t(`Growth.analysis_status_${item.status}`)}</span>
              {item.needs_recapture && <span>{t('Growth.analysis_needs_recapture')}</span>}
              {item.upstream_run_id && <code>{item.upstream_run_id}</code>}
              {item.status === 'failed' && !item.needs_recapture && <button
                disabled={busy || !!analysisTaskId}
                onClick={() => analyze(item.analysis_id)}>
                {t('Growth.retry_analysis')}
              </button>}
            </li>)}
          </ul>}
          {legacyMode && snapshotOpportunities.length > 0 && <div>
            <h3>{t('Growth.opportunities_title')}</h3>
            <ul className="growth-page__opportunities">{snapshotOpportunities.map((item) => <li
              key={item.opportunity_id}>
              <strong>{item.title}</strong>
              <p>{item.reason}</p>
              <p>{t('Growth.learning_goal')}: {item.learning_goal}</p>
              <p>{t('Growth.estimated_minutes', { count: item.estimated_minutes })}</p>
              {state?.topic_proposals.filter(
                (proposal) => proposal.opportunity_id === item.opportunity_id,
              ).map((proposal) => <div key={proposal.proposal_id}>
                <span>{t('Growth.topic_suggestion')}: {proposal.title}
                  {' · '}{t(`Growth.topic_${proposal.status}`)}</span>
                {proposal.status === 'pending' && <>
                  <button disabled={busy || (syncSummary !== null && !cloudTopicsReady)
                    || cloudTopics.some((topic) => topic.status === 'active'
                      && !topic.canonical_topic_id && topic.title === proposal.title)}
                    onClick={() => decideTopic(proposal.proposal_id, 'confirm')}>
                    {t('Growth.confirm_topic')}
                  </button>
                  {syncSummary && <>
                    <label htmlFor={`growth-topic-reuse-${proposal.proposal_id}`}>
                      {t('Growth.select_existing_topic')}
                    </label>
                    <select id={`growth-topic-reuse-${proposal.proposal_id}`}
                      value={topicReuseIds[proposal.proposal_id]
                        ?? cloudTopics.find((topic) => topic.status === 'active'
                          && !topic.canonical_topic_id && topic.title === proposal.title)
                          ?.topic_id ?? ''}
                      onChange={(event) => setTopicReuseIds((current) => ({
                        ...current, [proposal.proposal_id]: event.target.value,
                      }))} disabled={busy || !cloudTopicsReady}>
                      <option value="">{t('Growth.select_existing_topic')}</option>
                      {cloudTopics.filter((topic) => topic.status === 'active'
                        && !topic.canonical_topic_id).map((topic) =>
                        <option key={topic.topic_id} value={topic.topic_id}>{topic.title}</option>)}
                    </select>
                    <button disabled={busy || !cloudTopicsReady
                      || !(topicReuseIds[proposal.proposal_id]
                        ?? cloudTopics.find((topic) => topic.status === 'active'
                          && !topic.canonical_topic_id && topic.title === proposal.title)
                          ?.topic_id)}
                      onClick={() => decideTopic(proposal.proposal_id, 'confirm',
                        topicReuseIds[proposal.proposal_id]
                        ?? cloudTopics.find((topic) => topic.status === 'active'
                          && !topic.canonical_topic_id && topic.title === proposal.title)
                          ?.topic_id)}>
                      {t('Growth.reuse_topic')}
                    </button>
                    {cloudTopicsError && <span role="alert">
                      {t('Growth.topic_reuse_load_failed')}
                    </span>}
                  </>}
                  <button disabled={busy} onClick={() => decideTopic(proposal.proposal_id, 'reject')}>
                    {t('Growth.reject_topic')}
                  </button>
                </>}
                {proposal.status === 'confirmed' && proposal.topic_id
                  && !snapshotTasks.some((task) => task.opportunity_id === item.opportunity_id)
                  && <div>
                    {cloudModules.some((module) => module.snapshot_id === selectedSnapshotId)
                      && <>
                        <label htmlFor={`growth-card-module-${item.opportunity_id}`}>
                          {t('Growth.card_module')}
                        </label>
                        <select id={`growth-card-module-${item.opportunity_id}`}
                          value={taskModuleIds[item.opportunity_id] ?? ''}
                          onChange={(event) => setTaskModuleIds((current) => ({
                            ...current, [item.opportunity_id]: event.target.value,
                          }))}>
                          <option value="">{t('Growth.card_module_none')}</option>
                          {cloudModules.filter((module) => module.snapshot_id === selectedSnapshotId)
                            .map((module) => <option key={module.module_id} value={module.module_id}>
                              {module.name} · {module.locked
                                ? t('Growth.module_status_locked')
                                : t('Growth.module_status_draft')}
                            </option>)}
                        </select>
                      </>}
                    <button disabled={busy}
                      onClick={() => createTask(item.opportunity_id, proposal.topic_id!)}>
                      {t('Growth.create_card')}
                    </button>
                  </div>}
              </div>)}
              {snapshotTasks.filter((task) => task.opportunity_id === item.opportunity_id)
                .map((task) => <button key={task.task_id} disabled={busy}
                  onClick={() => setSelectedTaskId(task.task_id)}>
                  {t('Growth.open_card')} · {t(`Growth.task_${task.progress}`)}
                </button>)}
            </li>)}</ul>
          </div>}
          
        </section>}</>;
}
