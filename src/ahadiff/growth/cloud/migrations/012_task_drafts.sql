CREATE TABLE growth_task_drafts (
    account_id UUID NOT NULL,
    task_id UUID NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    content_text TEXT NOT NULL CHECK (char_length(content_text) <= 20000),
    submitted_attempt_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, task_id),
    FOREIGN KEY (account_id, task_id)
        REFERENCES growth_tasks(account_id, task_id),
    FOREIGN KEY (account_id, submitted_attempt_id)
        REFERENCES growth_learning_attempts(account_id, attempt_id)
);
