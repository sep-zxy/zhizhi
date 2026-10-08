ALTER TABLE growth_tasks ADD COLUMN source_commit_shas JSONB NOT NULL DEFAULT '[]'::jsonb;

UPDATE growth_tasks AS task
SET source_commit_shas = jsonb_build_array(snapshot.capture_scope->>'sha')
FROM growth_opportunities AS opportunity
JOIN growth_analysis_runs AS analysis
  ON analysis.account_id = opportunity.account_id
 AND analysis.analysis_id = opportunity.analysis_id
JOIN growth_snapshots AS snapshot
  ON snapshot.account_id = analysis.account_id
 AND snapshot.snapshot_id = analysis.snapshot_id
WHERE task.account_id = opportunity.account_id
  AND task.opportunity_id = opportunity.opportunity_id
  AND snapshot.capture_scope->>'kind' = 'commit'
  AND snapshot.capture_scope ? 'sha';
