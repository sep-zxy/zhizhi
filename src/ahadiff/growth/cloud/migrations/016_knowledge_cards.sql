ALTER TABLE growth_tasks ADD COLUMN learning_goal TEXT NOT NULL DEFAULT '';
ALTER TABLE growth_tasks ADD COLUMN back_answer TEXT NOT NULL DEFAULT '';
ALTER TABLE growth_tasks ADD COLUMN back_explanation TEXT NOT NULL DEFAULT '';
ALTER TABLE growth_tasks ADD COLUMN card_version INTEGER NOT NULL DEFAULT 1
    CHECK (card_version > 0);

UPDATE growth_tasks AS task SET learning_goal = opportunity.learning_goal
FROM growth_opportunities AS opportunity
WHERE opportunity.account_id = task.account_id
  AND opportunity.opportunity_id = task.opportunity_id;
