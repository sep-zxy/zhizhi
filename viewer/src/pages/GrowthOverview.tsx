import { Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { listGrowthLocalReviews, type GrowthLocalState, type GrowthDueReview } from '../api/growth';
import { projectForCard } from './GrowthCardLibrary';
import { useGrowthHistoryIndex } from './useGrowthHistoryIndex';

export default function GrowthOverview({ state, projectId }: { state: GrowthLocalState; projectId: string }) {
  const projects = state.projects.filter((project) => !projectId || project.project_id === projectId);
  const { history, error } = useGrowthHistoryIndex(state, projectId);
  const [due, setDue] = useState<GrowthDueReview[] | null>(null);
  useEffect(() => { let active = true; void listGrowthLocalReviews().then((result) => { if (active) setDue(result.due); }).catch(() => { if (active) setDue(null); }); return () => { active = false; }; }, [state]);
  return <section className="growth-overview" aria-label="项目学习总览">
    {error && <p role="alert">{error}</p>}
    {projects.length === 0 && <div className="growth-page__panel growth-workspace__empty"><h2>从一个真实项目开始</h2><p>导入本机 Git 仓库，发现历史变化中值得理解的知识点。</p><Link to="/growth?view=projects">导入项目</Link></div>}
    {projects.map((project) => {
      const cards = state.growth_tasks.filter((card) => projectForCard(state, card) === project.project_id);
      const features = new Set(state.features.filter((feature) => feature.project_id === project.project_id).map((item) => item.feature_id));
      const snapshots = new Set(state.snapshots.filter((snapshot) => features.has(snapshot.feature_id)).map((item) => item.snapshot_id));
      const analyses = new Set(state.analysis_runs.filter((analysis) => snapshots.has(analysis.snapshot_id)).map((item) => item.analysis_id));
      const pending = state.opportunities.filter((item) => analyses.has(item.analysis_id) && (item.recommendation_status ?? 'pending') === 'pending' && !cards.some((card) => card.opportunity_id === item.opportunity_id));
      return <article className="growth-page__panel growth-overview__project" key={project.project_id}>
        <div className="growth-workspace__section-heading"><h2>{project.name}</h2><span className="growth-workspace__badge">本机项目</span></div>
        <p className="growth-page__hint">{state.bindings.find((binding) => binding.project_id === project.project_id)?.canonical_local_path ?? '等待绑定仓库'}</p>
        <dl className="growth-overview__counts"><div><dt>历史待学习提交</dt><dd>{error ? '—' : history.filter((item) => item.project_id === project.project_id && item.learning_status === 'unlearned' && !item.is_new).length}</dd></div><div><dt>新增提交</dt><dd>{error ? '—' : history.filter((item) => item.project_id === project.project_id && item.is_new).length}</dd></div><div><dt>推荐知识点</dt><dd>{pending.length}</dd></div><div><dt>待复习卡片</dt><dd>{due?.filter((item) => item.project_id === project.project_id).length ?? '—'}</dd></div></dl>
        <p className="growth-page__hint">{cards.length} 张正式知识卡 · {cards.filter((card) => ['in_progress', 'paused'].includes(card.progress)).length} 张学习中</p>
        <div className="growth-page__actions"><Link className="btn" to={'/growth?view=pending&project=' + project.project_id}>查看待确认</Link><Link className="btn ghost" to={'/growth?view=cards&project=' + project.project_id}>继续学习</Link><Link to={'/growth?view=review&project=' + project.project_id}>复习知识卡</Link></div>
      </article>;
    })}
  </section>;
}
