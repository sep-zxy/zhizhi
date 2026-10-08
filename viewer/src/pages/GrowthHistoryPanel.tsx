import { useCallback, useEffect, useRef, useState } from 'react';
import { getGrowthCommits, groupGrowthAuthors, type GrowthCommit } from '../api/growth';

export default function GrowthHistoryPanel({ bindingId, featureId, selected, onSelect, onAnalyze, busy }: {
  bindingId: string; featureId: string; selected: string[]; onSelect: (ids: string[]) => void;
  onAnalyze: () => void; busy: boolean;
}) {
  const [commits, setCommits] = useState<GrowthCommit[]>([]);
  const [authors, setAuthors] = useState<Awaited<ReturnType<typeof getGrowthCommits>>['authors']>([]);
  const [selectedAuthors, setSelectedAuthors] = useState<string[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  const [after, setAfter] = useState('');
  const [groupName, setGroupName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const load = useCallback(async (next?: string, scan = false) => {
    const request = ++sequence.current;
    setLoading(true); setError('');
    try {
      const result = await getGrowthCommits(bindingId, featureId, { cursor: next, authors: selectedAuthors, scan });
      if (request !== sequence.current) return;
      setCommits((current) => next ? [...current, ...result.commits.filter((item) => !current.some((known) => known.sha === item.sha))] : result.commits);
      setAuthors(result.authors ?? []); setCursor(result.next_cursor);
    } catch { if (request === sequence.current) setError('提交历史读取失败，请重试。'); }
    finally { if (request === sequence.current) setLoading(false); }
  }, [bindingId, featureId, selectedAuthors]);
  useEffect(() => { void load(); return () => { sequence.current++; }; }, [load]);
  const visible = commits.filter((item) => (!status || (status === 'new' ? item.is_new : item.learning_status === status)) && (!after || item.authored_at.slice(0, 10) >= after));
  return <section className="growth-page__panel growth-page__commit-panel" aria-labelledby="growth-commits-title">
    <div className="growth-workspace__section-heading"><h2 id="growth-commits-title">完整提交历史</h2><button type="button" className="growth-page__secondary" disabled={loading || busy} onClick={() => { void load(undefined, true); }}>扫描本机新提交</button></div>
    <p className="growth-page__hint">浏览本机已有引用的历史。提交是学习线索，确认知识点后才生成卡片。</p>
    <div className="growth-workspace__filters"><label>学习状态<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option><option value="new">上次扫描后发现</option><option value="unlearned">未关联卡片</option><option value="learned">已关联卡片</option></select></label><label>起始日期<input type="date" value={after} onChange={(event) => setAfter(event.target.value)} /></label></div>
    <details><summary>作者身份 · {selectedAuthors.length ? '已选择 ' + selectedAuthors.length + ' 个身份' : '全部作者'}</summary>
      <div className="growth-workspace__author-list">{authors.map((author) => <label key={author.author_name + author.author_email}><input type="checkbox" checked={selectedAuthors.includes(author.author_email)} onChange={(event) => setSelectedAuthors((current) => event.target.checked ? [...new Set([...current, author.author_email])] : current.filter((email) => email !== author.author_email))} /><span>{author.author_name} &lt;{author.author_email}&gt;{author.group_name !== author.author_email ? ' · ' + author.group_name : ''}</span></label>)}</div>
      <label>将所选身份归为同一人<input value={groupName} onChange={(event) => setGroupName(event.target.value)} placeholder="姓名或分组名称" /></label>
      <button type="button" disabled={loading || selectedAuthors.length < 1 || !groupName.trim()} onClick={() => { setLoading(true); void groupGrowthAuthors(bindingId, selectedAuthors, groupName.trim()).then(() => load()).catch(() => { setError('身份归组失败，请重试。'); setLoading(false); }); }}>保存身份分组</button>
    </details>
    {visible.map((item) => <label key={item.sha} className="growth-page__commit-row"><input type="checkbox" checked={selected.includes(item.sha)} onChange={(event) => onSelect(event.target.checked ? [...selected, item.sha] : selected.filter((sha) => sha !== item.sha))} /><span><strong>{item.title}</strong><small>{item.sha.slice(0, 8)} · {item.author_name} · {new Date(item.authored_at).toLocaleDateString()} · {item.paths.length} 个文件</small><small>{item.is_new ? '新发现 · ' : ''}{item.linked_card_count ? '已关联 ' + item.linked_card_count + ' 张卡片' : '尚未关联卡片'}</small></span></label>)}
    {!loading && visible.length === 0 && <p>没有符合筛选的提交。可调整作者、日期或状态。</p>}
    {loading && <p role="status">正在读取提交历史…</p>}
    {error && <p role="alert">{error}<button type="button" onClick={() => { void load(); }}>重试</button></p>}
    <div className="growth-page__actions">{cursor && <button type="button" className="growth-page__secondary" disabled={loading} onClick={() => { void load(cursor); }}>加载更早提交</button>}<button type="button" disabled={busy || selected.length === 0} onClick={onAnalyze}>为选中的 {selected.length} 个提交准备学习材料</button></div>
  </section>;
}
