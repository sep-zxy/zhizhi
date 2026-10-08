CREATE TABLE growth_review_schedules (
    account_id UUID NOT NULL,
    task_id UUID NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    fsrs_state TEXT NOT NULL,
    scheduler_version TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    last_hint_level INTEGER CHECK (last_hint_level BETWEEN 0 AND 3),
    last_review_id UUID,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, task_id),
    FOREIGN KEY (account_id, task_id) REFERENCES growth_tasks(account_id, task_id)
);

CREATE INDEX growth_review_due ON growth_review_schedules(account_id, due_at)
    WHERE status='active';

CREATE TABLE growth_review_events (
    account_id UUID NOT NULL,
    review_id UUID NOT NULL,
    task_id UUID NOT NULL,
    schedule_revision INTEGER NOT NULL,
    answer TEXT NOT NULL CHECK (answer IN ('easy', 'good', 'hard', 'wrong')),
    hint_level INTEGER NOT NULL CHECK (hint_level BETWEEN 0 AND 3),
    actor_origin TEXT NOT NULL CHECK (actor_origin IN ('user', 'simulated_user')),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 4),
    due_before TIMESTAMPTZ NOT NULL,
    due_after TIMESTAMPTZ NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, review_id),
    FOREIGN KEY (account_id, task_id)
        REFERENCES growth_review_schedules(account_id, task_id)
);
