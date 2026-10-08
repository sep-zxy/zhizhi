import { useEffect, useState } from 'react';
import { getGrowthCommits, type GrowthCommit, type GrowthLocalState } from '../api/growth';

/** Read each indexed page so overview counts and recommendation filters cover all history. */
export function useGrowthHistoryIndex(state: GrowthLocalState, projectId = '') {
  const [history, setHistory] = useState<Array<GrowthCommit & { project_id: string }>>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setHistory([]); setError('');
    void Promise.all(state.bindings.filter((binding) => !projectId || binding.project_id === projectId).map(async (binding) => {
      const rows: Array<GrowthCommit & { project_id: string }> = [];
      let cursor: string | null = '0';
      const feature = state.features.find((item) => item.project_id === binding.project_id)?.feature_id ?? '';
      do {
        const result = await getGrowthCommits(binding.binding_id, feature, { cursor });
        rows.push(...result.commits.map((item) => ({ ...item, project_id: binding.project_id })));
        cursor = result.next_cursor;
      } while (active && cursor);
      return rows;
    })).then((results) => {
      if (active) setHistory([...new Map(results.flat().map((item) => [item.project_id + ':' + item.sha, item])).values()]);
    }).catch(() => { if (active) setError('历史统计暂时无法读取，请刷新工作区重试。'); });
    return () => { active = false; };
  }, [state, projectId]);
  return { history, error };
}
