import { useEffect, useState } from 'react';
import KnowledgeWorkspace from './KnowledgeWorkspace';
import { getKnowledge } from '../api/knowledge';
import { KnowledgeProjects, KnowledgeExplore, KnowledgeSettings } from './KnowledgeSupportPages';
import { KnowledgeVaultSettings } from './KnowledgeWiki';
import GrowthHistoryPanel from './GrowthHistoryPanel';



import { Link } from 'react-router-dom';
import GrowthShell from '../growth/GrowthShell';
import { getGrowthCommits, setGrowthProjectPolicy } from '../api/growth';
import { useGrowthWorkspace } from './useGrowthWorkspace';

import GrowthDiscussionPanel from './GrowthDiscussionPanel';
import GrowthSettingsPanel from './GrowthSettingsPanel';
import GrowthAnalysisPanel from './GrowthAnalysisPanel';
import './GrowthPage.css';
import './KnowledgeWorkspace.css';

export default function GrowthPage() {
  const workspace = useGrowthWorkspace();
  const [latestScanAt,setLatestScanAt]=useState<string>();
  const [pendingCount,setPendingCount]=useState<number>();
  useEffect(()=>{let current=true;void getKnowledge().then(index=>{if(current)setPendingCount(index.candidates.filter(c=>['pending','deferred'].includes(c.status)&&(!workspace.projectId||c.project_ids?.includes(workspace.projectId))).length);}).catch(()=>{if(current)setPendingCount(undefined);});return()=>{current=false;};},[workspace.state,workspace.projectId]);
  useEffect(()=>{let current=true;setLatestScanAt(undefined);const state=workspace.state;void Promise.all((state?.bindings??[]).filter(b=>!workspace.projectId||b.project_id===workspace.projectId).map(async b=>{try{return (await getGrowthCommits(b.binding_id,state?.features.find(f=>f.project_id===b.project_id)?.feature_id??'')).scan?.scanned_at;}catch{return undefined;}})).then(dates=>{if(current)setLatestScanAt(dates.filter((date):date is string=>!!date&&Number.isFinite(Date.parse(date))).sort().at(-1));});return()=>{current=false;};},[workspace.projectId,workspace.state?.bindings]);
  const {
    t, pageTitles, pageDescriptions,
    view,
    showProjectManagement,
    state,
    projectId,
    setProjectId,
    setBindingId,
    setFeatureId,
    projectName,
    setProjectName,
    quickName,
    setQuickName,
    quickPath,
    setQuickPath,
    quickModelAllowed,
    setQuickModelAllowed,
    repositoryPath,
    setRepositoryPath,
    featureLabel,
    setFeatureLabel,
    baseRef,
    setBaseRef,
    busy,
    error,
    captured,
    setCaptured,
    untrackedPaths,
    selectedUntracked,
    setSelectedUntracked,
    selectedCommits,
    setSelectedCommits,
    commitError,
    commitMessage,
    setSnapshotId,
    policyDraft,
    setPolicyDraft,
    bindings,
    selectedProject,
    features,
    selectedBindingId,
    selectedFeatureId,
    selectedBinding,
    featureSnapshots,
    act,
    submitProject,
    submitQuickImport,
    submitBinding,
    submitFeature,
    capture,
    captureSelectedCommits
  } = workspace;
  if (['projects', 'topics', 'settings'].includes(view)) return <GrowthShell pendingCount={pendingCount} latestScanAt={latestScanAt} projects={state?.projects??[]} projectId={projectId} onProjectChange={setProjectId}><div className="growth-page"><header className="growth-page__header"><h1>{pageTitles[view]}</h1><p>{view==='settings'?'管理本地项目、模型连接与知识保存位置。':view==='projects'?'查看完整提交历史，追溯知识点的真实来源':'围绕真实项目追问，把值得学习的问题留成候选。'}</p></header>{error&&<p role="alert">{error}</p>}{view==='projects'?<KnowledgeProjects workspace={workspace}/>:view==='topics'?<KnowledgeExplore workspace={workspace}/>:<KnowledgeSettings workspace={workspace}/>}</div></GrowthShell>;
  return (
    <GrowthShell pendingCount={pendingCount} latestScanAt={latestScanAt} projects={state?.projects ?? []} projectId={projectId} onProjectChange={setProjectId}>
      <div className="growth-page">
        <header className="growth-page__header">

          <h1>{pageTitles[view] ?? '成长工作区'}</h1>
          <p>{pageDescriptions[view]}</p>
        </header>

        {error && <p className="growth-page__error" role="alert">{error}</p>}

        {view === 'projects' && <section className="growth-page__panel growth-page__quick-import"
          aria-labelledby="growth-quick-import-title">
          <p className="growth-page__eyebrow">开始学习一个项目</p>
          <h2 id="growth-quick-import-title">导入 Git 项目</h2>
          <p className="growth-page__hint">填写名称和本机路径，即可建立项目与当前分支的学习入口。</p>
          <form onSubmit={submitQuickImport} className="growth-page__form">
            <label htmlFor="growth-quick-name">项目名称</label>
            <input id="growth-quick-name" value={quickName}
              onChange={(event) => setQuickName(event.target.value)}
              placeholder="例如：我的服务端项目" />
            <label htmlFor="growth-quick-path">Git 仓库路径</label>
            <input id="growth-quick-path" value={quickPath}
              onChange={(event) => setQuickPath(event.target.value)}
              placeholder="例如：C:\\Code\\my-project" />
            <label className="growth-page__approval"><input type="checkbox"
              checked={quickModelAllowed}
              onChange={(event) => setQuickModelAllowed(event.target.checked)} />
              允许对这个项目使用已配置的模型</label>
            <button disabled={busy || !quickName.trim() || !quickPath.trim()}>
              导入并查看新变化</button>
          </form>
        </section>}

        {view === 'projects' && selectedProject && <section
          className="growth-page__panel" aria-labelledby="growth-current-project-title">
          <h2 id="growth-current-project-title">正在学习的项目</h2>
          <p><strong>{selectedProject.name}</strong></p>
          {selectedBinding && <p className="growth-page__hint">{selectedBinding.canonical_local_path}</p>}
          {features.find((item) => item.feature_id === selectedFeatureId) &&
            <p>当前 Feature：{features.find((item) => item.feature_id === selectedFeatureId)?.label}</p>}
          <div className="growth-page__actions">
            <Link className="btn" to={'/growth?view=pending&project=' + projectId}>选择提交并开始学习</Link>
            <Link className="btn ghost" to={'/growth?view=discuss&project=' + projectId}>提问并探索主题</Link>
          </div>
        </section>}

        {['today','learning','cards','wiki'].includes(view) && state && <KnowledgeWorkspace view={view} projectId={projectId} state={state} onChanged={workspace.refresh} />}
        {view === 'settings' && <section className="growth-page__panel"><h2>应用与模型</h2><p>配置模型连接、语言与应用偏好。</p><Link to="/settings">打开应用设置</Link></section>}
        {!state && !error && <p role="status">正在读取成长工作区…</p>}
        {error && <button type="button" onClick={() => { void workspace.refresh(); }}>重试加载</button>}

        {false && view === 'learning' && <section className="growth-page__panel"
          aria-labelledby="growth-learning-context-title">
          <h2 id="growth-learning-context-title">{t('Growth.learning_context')}</h2>
          {state?.projects.length === 0 ? <p>{t('Growth.learning_setup')}</p> : <>
            <label htmlFor="growth-learning-project">{t('Growth.selected_project')}</label>
            <select id="growth-learning-project" value={projectId}
              onChange={(event) => { setProjectId(event.target.value); setBindingId('');
                setFeatureId(''); setSnapshotId(''); setCaptured(null); }}>
              {state?.projects.map((item) => <option key={item.project_id} value={item.project_id}>
                {item.name}</option>)}
            </select>
            {features.length > 0 && <>
              <label htmlFor="growth-learning-feature">{t('Growth.selected_feature')}</label>
              <select id="growth-learning-feature" value={selectedFeatureId}
                onChange={(event) => { setFeatureId(event.target.value); setSnapshotId('');
                  setCaptured(null); }}>
                {features.map((item) => <option key={item.feature_id} value={item.feature_id}>
                  {item.label}</option>)}
              </select>
            </>}
          </>}
          <Link to="/growth?view=projects">{t('Growth.learning_manage_projects')}</Link>
        </section>}

        <GrowthDiscussionPanel workspace={workspace} />

        {view === 'projects' && selectedBindingId && <GrowthHistoryPanel key={selectedBindingId} bindingId={selectedBindingId} featureId={selectedFeatureId} selected={selectedCommits} onSelect={setSelectedCommits} busy={busy} onAnalyze={() => { void captureSelectedCommits(); }} />}
        {commitError && <p role="alert">{commitError}</p>}
        {commitMessage && <p role="status">{commitMessage}</p>}

        {view === 'settings' && <KnowledgeVaultSettings />}
        <GrowthSettingsPanel workspace={workspace} />

        {showProjectManagement && <section className="growth-page__panel" aria-labelledby="growth-projects-title">
          <h2 id="growth-projects-title">{t('Growth.projects')}</h2>
          <form onSubmit={submitProject} className="growth-page__form">
            <label htmlFor="growth-project-name">{t('Growth.project_name')}</label>
            <input id="growth-project-name" value={projectName}
              onChange={(event) => setProjectName(event.target.value)} maxLength={120} />
            <button disabled={busy || !projectName.trim()}>{t('Growth.create_project')}</button>
          </form>
          <label htmlFor="growth-project-select">{t('Growth.selected_project')}</label>
          <select id="growth-project-select" value={projectId} onChange={(event) => {
            setProjectId(event.target.value);
            setBindingId('');
            setFeatureId('');
            setSnapshotId('');
            setCaptured(null);
          }}>
            <option value="">{t('Growth.select_project')}</option>
            {state?.projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
          </select>
          {selectedProject && <form className="growth-page__form" onSubmit={(event) => {
            event.preventDefault();
            void act(async () => { await setGrowthProjectPolicy(projectId, policyDraft); });
          }}>
            <h3>{t('Growth.project_policy')}</h3>
            <label><input type="checkbox" checked={policyDraft.local_processing}
              onChange={(event) => setPolicyDraft((current) => ({
                ...current, local_processing: event.target.checked,
              }))} />{t('Growth.local_processing')}</label>
            <label><input type="checkbox" checked={policyDraft.model_allowed}
              onChange={(event) => setPolicyDraft((current) => ({
                ...current, model_allowed: event.target.checked,
              }))} />{t('Growth.model_allowed')}</label>
            <label><input type="checkbox" checked={policyDraft.cloud_allowed}
              disabled={!selectedProject.cloud_allowed}
              onChange={(event) => setPolicyDraft((current) => ({
                ...current, cloud_allowed: event.target.checked,
              }))} />{t('Growth.cloud_allowed')}</label>
            <p className="growth-page__hint">{t('Growth.cloud_permission_hint')}</p>
            {selectedProject.last_publish_error && <p className="growth-page__error" role="alert">
              {t('Growth.cloud_publish_blocked')}: {selectedProject.last_publish_error}
            </p>}
            <button disabled={busy}>{t('Growth.save_project_policy')}</button>
          </form>}
        </section>}

        {showProjectManagement && projectId && <section className="growth-page__panel" aria-labelledby="growth-bindings-title">
          <h2 id="growth-bindings-title">{t('Growth.bindings')}</h2>
          <form onSubmit={submitBinding} className="growth-page__form">
            <label htmlFor="growth-repository-path">{t('Growth.repository_path')}</label>
            <input id="growth-repository-path" value={repositoryPath}
              onChange={(event) => setRepositoryPath(event.target.value)} maxLength={4096}
              placeholder={t('Growth.repository_path_hint')} />
            <button disabled={busy || !repositoryPath.trim()}>{t('Growth.bind_repository')}</button>
          </form>
          <label htmlFor="growth-binding-select">{t('Growth.selected_binding')}</label>
          <select id="growth-binding-select" value={selectedBindingId}
            onChange={(event) => {
              setBindingId(event.target.value);
              setSnapshotId('');
              setCaptured(null);
            }}>
            <option value="">{t('Growth.select_binding')}</option>
            {bindings.map((item) => <option key={item.binding_id} value={item.binding_id}>
              {item.canonical_local_path}
            </option>)}
          </select>
          {selectedBinding && <p className="growth-page__hint">{selectedBinding.canonical_local_path}</p>}
        </section>}

        {showProjectManagement && selectedBindingId && <section className="growth-page__panel" aria-labelledby="growth-features-title">
          <h2 id="growth-features-title">{t('Growth.features')}</h2>
          <form onSubmit={submitFeature} className="growth-page__form growth-page__form--feature">
            <label htmlFor="growth-feature-label">{t('Growth.feature_label')}</label>
            <input id="growth-feature-label" value={featureLabel}
              onChange={(event) => setFeatureLabel(event.target.value)} maxLength={160} />
            <label htmlFor="growth-base-ref">{t('Growth.base_ref')}</label>
            <input id="growth-base-ref" value={baseRef}
              onChange={(event) => setBaseRef(event.target.value)} maxLength={200}
              placeholder={t('Growth.base_ref_hint')} />
            <button disabled={busy || !featureLabel.trim() || !baseRef.trim()}>
              {t('Growth.create_feature')}
            </button>
          </form>
          <label htmlFor="growth-feature-select">{t('Growth.selected_feature')}</label>
          <select id="growth-feature-select" value={selectedFeatureId}
            onChange={(event) => { setFeatureId(event.target.value); setSnapshotId(''); setCaptured(null); }}>
            <option value="">{t('Growth.select_feature')}</option>
            {features.map((item) => <option key={item.feature_id} value={item.feature_id}>
              {item.label} · {item.base_ref}
            </option>)}
          </select>
        </section>}

        {showProjectManagement && selectedFeatureId && <section className="growth-page__panel" aria-labelledby="growth-capture-title">
          <h2 id="growth-capture-title">{t('Growth.capture_title')}</h2>
          <p>{t('Growth.capture_description')}</p>
          {untrackedPaths.length > 0 && <details className="growth-page__advanced">
            <summary>按需包含未跟踪文件（{untrackedPaths.length}）</summary>
            <fieldset className="growth-page__untracked">
            <legend>{t('Growth.untracked_files')}</legend>
            {untrackedPaths.map((path) => <label key={path}>
              <input type="checkbox" checked={selectedUntracked.includes(path)}
                onChange={(event) => setSelectedUntracked((current) => event.target.checked
                  ? [...current, path] : current.filter((item) => item !== path))} />
              <code>{path}</code>
            </label>)}
            </fieldset>
          </details>}
          <button onClick={capture} disabled={busy}>{t('Growth.capture')}</button>
          {captured && <div className="growth-page__capture" role="status">
            <strong>{t('Growth.saved_locally')}</strong>
            <p>{t('Growth.snapshot_id')}: <code>{captured.snapshot_id}</code></p>
            <p>{t('Growth.head')}: <code>{captured.head_sha.slice(0, 12)}</code></p>
            <p>{t('Growth.changed_files')}: {captured.changed_paths.length}</p>
            <ul>{captured.changed_paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
          </div>}
          <p className="growth-page__hint">{t('Growth.previous_snapshots')}: {featureSnapshots.length}</p>
          {featureSnapshots.length > 0 && <Link to="/growth?view=learning">
            {t('Growth.continue_learning')}</Link>}
        </section>}

        <GrowthAnalysisPanel workspace={workspace} />

      </div>
    </GrowthShell>
  );
}
