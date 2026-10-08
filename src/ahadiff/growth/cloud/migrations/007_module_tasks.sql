ALTER TABLE growth_tasks ADD COLUMN module_id UUID;
ALTER TABLE growth_tasks ADD COLUMN module_index_revision CHAR(64);
ALTER TABLE growth_tasks ADD CONSTRAINT growth_tasks_module_fk
    FOREIGN KEY (account_id, module_id) REFERENCES growth_modules(account_id, module_id);
ALTER TABLE growth_tasks ADD CONSTRAINT growth_tasks_module_pair_check
    CHECK ((module_id IS NULL) = (module_index_revision IS NULL));

CREATE INDEX growth_tasks_module_idx ON growth_tasks(account_id, module_id, task_id)
    WHERE module_id IS NOT NULL;
