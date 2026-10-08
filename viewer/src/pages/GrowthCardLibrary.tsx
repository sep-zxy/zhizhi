import { useState } from 'react';
import { Link } from 'react-router-dom';
import { exportGrowthCards, type GrowthLocalState, type GrowthTaskSummary } from '../api/growth';

export function projectForCard(state: GrowthLocalState, card: GrowthTaskSummary) {
  if (card.project_id) return card.project_id;
  const opportunity = state.opportunities.find((item) => item.opportunity_id === card.opportunity_id);
  const analysis = state.analysis_runs.find((item) => item.analysis_id === opportunity?.analysis_id);
  const snapshot = state.snapshots.find((item) => item.snapshot_id === analysis?.snapshot_id);
  return state.features.find((item) => item.feature_id === snapshot?.feature_id)?.project_id;
}
const PROGRESS: Record<string, string> = { ready: '新卡', in_progress: '学习中', paused: '已暂停', completed: '已学过', dismissed: '已忽略' };

export default function GrowthCardLibrary({ state, projectId, onOpen }: {
  state: GrowthLocalState; projectId: string; onOpen: (card: GrowthTaskSummary) => void;
}) {
  const [query, setQuery] = useState('');
  const [topic, setTopic] = useState('');
  const [progress, setProgress] = useState('');
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  async function downloadCards() {
    setExporting(true); setExportError('');
    try {
      const blob = await exportGrowthCards(projectId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'growth-knowledge-cards.apkg';
      anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setExportError('Anki 导出失败，请重试。'); }
    finally { setExporting(false); }
  }
  const cards = state.growth_tasks.filter((card) => (!projectId || projectForCard(state, card) === projectId)
    && (!topic || card.topic_id === topic) && (!progress || card.progress === progress)
    && (!query || [card.front_question ?? card.question, card.learning_goal ?? '', card.back_answer ?? ''].join(' ').toLocaleLowerCase().includes(query.toLocaleLowerCase())));
  return <section className="growth-page__panel growth-library" aria-labelledby="growth-library-title">
    <div className="growth-workspace__section-heading"><h2 id="growth-library-title">知识卡片</h2><span>{cards.length} 张</span></div>
    <div className="growth-page__actions"><button type="button" className="growth-page__secondary" disabled={exporting || cards.length === 0} onClick={() => { void downloadCards(); }}>{exporting ? '正在准备导出…' : '导出项目知识卡为 Anki'}</button><small>单向 .apkg 导出。Anki 与本应用分别安排复习进度。</small></div>
    {exportError && <p role="alert">{exportError}</p>}
    <div className="growth-workspace__filters">
      <label>查找知识点<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="问题、目标或答案" /></label>
      <label>主题<select value={topic} onChange={(event) => setTopic(event.target.value)}><option value="">全部主题</option>
        {state.local_topics.map((item) => <option key={item.topic_id} value={item.topic_id}>{item.title}</option>)}</select></label>
      <label>进度<select value={progress} onChange={(event) => setProgress(event.target.value)}><option value="">全部进度</option>
        {Object.entries(PROGRESS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    </div>
    {cards.length === 0 ? <div className="growth-workspace__empty"><h3>还没有符合条件的知识卡</h3><p>调整筛选，或确认一个来自代码变化与探讨的知识点。</p><Link to={'/growth?view=pending&project=' + projectId}>查看待确认</Link></div>
      : <div className="growth-library__list">{cards.map((card) => <article key={card.card_id ?? card.task_id} className="growth-library__card">
        <div className="growth-workspace__section-heading"><span className="growth-workspace__badge">{PROGRESS[card.progress]}</span><small>v{card.card_version ?? 1}</small></div>
        <h3>{card.front_question ?? card.question}</h3><p>{card.learning_goal}</p>
        <div className="growth-library__metadata"><span>{state.local_topics.find((item) => item.topic_id === card.topic_id)?.title ?? '待核对主题'}</span><span>{state.projects.find((item) => item.project_id === projectForCard(state, card))?.name ?? '未关联项目'}</span></div>
        <small>{card.source_link_status === 'needs_review' || !(card.source_commit_shas?.length) ? '来源关联待核对' : card.source_commit_shas.map((sha) => sha.slice(0, 8)).join(' · ')}</small>
        <button type="button" onClick={() => onOpen(card)}>打开卡片</button>
      </article>)}</div>}
  </section>;
}
