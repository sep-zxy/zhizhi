import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { decideGrowthRecommendations, type GrowthLocalState } from '../api/growth';
import { useGrowthHistoryIndex } from './useGrowthHistoryIndex';

export default function GrowthRecommendations({ state, projectId, onChanged }: {
  state: GrowthLocalState; projectId: string; onChanged: () => Promise<void>;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [status, setStatus] = useState('pending');
  const [query, setQuery] = useState('');
  const [author, setAuthor] = useState('');
  const [since, setSince] = useState('');
  const { history, error: historyError } = useGrowthHistoryIndex(state, projectId);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const pending = useRef<{ key: string; requestId: string } | null>(null);
  const visible = state.opportunities.filter((item) => (!projectId || item.project_id === projectId)
    && (!status || (item.recommendation_status ?? 'pending') === status)
    && ((!author && !since) || history.some((commit) => item.source_commit_shas?.includes(commit.sha) && (!author || commit.author_email === author) && (!since || commit.authored_at.slice(0, 10) >= since)))
    && (!query || [item.title, item.reason, item.learning_goal].join(' ').toLocaleLowerCase().includes(query.toLocaleLowerCase())));
  const actionable = visible.filter((item) => ['pending', 'deferred'].includes(item.recommendation_status ?? 'pending'));
  async function decide(action: 'confirm' | 'defer' | 'ignore') {
    setBusy(true); setError(''); setMessage('');
    try {
      const chosen = state.opportunities.filter((item) => selected.includes(item.opportunity_id));
      const grouped = new Map<string, typeof chosen>();
      for (const item of chosen) {
        const key = (item.project_id ?? '') + ':' + (item.recommendation_key || item.opportunity_id);
        grouped.set(key, [...(grouped.get(key) ?? []), item]);
      }
      const groups = (action === 'confirm' ? [...grouped.values()] : []).filter((items) => items[0].suggested_card_count !== 0).map((items) => {
        const proposal = state.topic_proposals.find((entry) => entry.opportunity_id === items[0].opportunity_id && entry.status !== 'rejected');
        if (!proposal) throw new Error('部分推荐尚无可确认主题，请先在下方分析结果中选择主题。');
        return { opportunity_ids: items.map((item) => item.opportunity_id), proposal_id: proposal.proposal_id,
          existing_topic_id: proposal.topic_id ?? state.local_topics.find((topic) => topic.title === proposal.title && topic.status === 'active')?.topic_id ?? null };
      });
      const body = { confirm_groups: action === 'confirm' ? groups : [], defer_ids: action === 'defer' ? selected : [], ignore_ids: action === 'ignore' ? selected : [] };
      const key = JSON.stringify(body);
      if (pending.current?.key !== key) pending.current = { key, requestId: crypto.randomUUID() };
      const result = await decideGrowthRecommendations({ ...body, request_id: pending.current.requestId });
      pending.current = null; setSelected([]); await onChanged();
      setMessage(action === 'confirm' ? '已确认，新增 ' + result.created_card_count + ' 张正式知识卡。' : action === 'defer' ? '已暂缓所选推荐。' : '已忽略所选推荐。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败，请重试。'); }
    finally { setBusy(false); }
  }
  return <section className="growth-page__panel" aria-labelledby="growth-recommendations-title">
    <div className="growth-workspace__section-heading"><h2 id="growth-recommendations-title">待确认知识点</h2><span>{visible.length} 项推荐</span></div>
    <p className="growth-page__hint">同一个知识点的提交证据会合并保留。勾选你想学习的建议，一次确认后生成正式卡片。</p>
    <div className="growth-workspace__filters"><label>查找推荐<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="知识点、理由或目标" /></label><label>状态<select value={status} onChange={(event) => { setStatus(event.target.value); setSelected([]); }}><option value="pending">待确认</option><option value="deferred">已暂缓</option><option value="confirmed">已确认</option><option value="ignored">已忽略</option><option value="">全部</option></select></label></div>
    <div className="growth-workspace__filters"><label>提交作者<select value={author} onChange={(event) => { setAuthor(event.target.value); setSelected([]); }}><option value="">全部作者</option>{[...new Map(history.map((item) => [item.author_email, item])).values()].map((item) => <option key={item.author_email} value={item.author_email}>{item.author_name} · {item.author_email}</option>)}</select></label><label>起始日期<input type="date" value={since} onChange={(event) => { setSince(event.target.value); setSelected([]); }} /></label></div>
    {historyError && <p role="alert">{historyError}</p>}
    {visible.length === 0 && <div className="growth-workspace__empty"><h3>当前没有待处理的推荐</h3><p>选择下方提交，核对发送范围并生成推荐。没有值得学习的知识点时可以保留 0 张卡片。</p></div>}
    {visible.map((item) => <article className="growth-library__card" key={item.opportunity_id}>
      <label className="growth-page__approval"><input type="checkbox" checked={selected.includes(item.opportunity_id)} disabled={busy || !actionable.includes(item)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, item.opportunity_id] : current.filter((id) => id !== item.opportunity_id))} /><strong>{item.title}</strong></label>
      <p>{item.reason}</p><p>学习目标：{item.learning_goal}</p><small>建议 {item.suggested_card_count ?? 1} 张 · {item.estimated_minutes} 分钟</small>
      <div className="growth-page__source-list">{item.source_commit_shas?.map((sha) => <code key={sha}>{sha.slice(0, 8)}</code>)}</div>
      <details><summary>查看来源引用</summary><pre className="growth-page__payload">{item.source_refs_json}</pre></details>
    </article>)}
    {actionable.length > 0 && <div className="growth-page__actions"><button type="button" className="growth-page__secondary" disabled={busy} onClick={() => setSelected(actionable.map((item) => item.opportunity_id))}>全选当前推荐</button><button type="button" disabled={busy || selected.length === 0} onClick={() => { void decide('confirm'); }}>确认所选 {selected.length} 项并生成卡片</button><button type="button" className="growth-page__secondary" disabled={busy || selected.length === 0} onClick={() => { void decide('defer'); }}>暂缓</button><button type="button" className="growth-page__secondary" disabled={busy || selected.length === 0} onClick={() => { void decide('ignore'); }}>忽略</button></div>}
    {message && <p role="status">{message} <Link to={'/growth?view=cards&project=' + projectId}>打开知识卡片库</Link></p>}{error && <p role="alert" className="growth-page__error">{error}</p>}
  </section>;
}
