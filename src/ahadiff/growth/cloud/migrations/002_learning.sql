CREATE TABLE growth_opportunity_batches (
    account_id UUID NOT NULL,
    analysis_id UUID NOT NULL,
    output_hash CHAR(64) NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('live', 'replay')),
    provider_name TEXT,
    model_name TEXT,
    provider_request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, analysis_id),
    FOREIGN KEY (account_id, analysis_id)
        REFERENCES growth_analysis_runs(account_id, analysis_id)
);

CREATE TABLE growth_opportunities (
    account_id UUID NOT NULL,
    opportunity_id UUID NOT NULL,
    analysis_id UUID NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 2),
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    learning_goal TEXT NOT NULL,
    source_refs JSONB NOT NULL,
    estimated_minutes INTEGER NOT NULL CHECK (estimated_minutes BETWEEN 3 AND 5),
    uncertainties JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, opportunity_id),
    UNIQUE (account_id, analysis_id, ordinal),
    FOREIGN KEY (account_id, analysis_id)
        REFERENCES growth_opportunity_batches(account_id, analysis_id)
);

CREATE TABLE growth_topics (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    topic_id UUID NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, topic_id)
);

CREATE TABLE growth_topic_proposals (
    account_id UUID NOT NULL,
    proposal_id UUID NOT NULL,
    opportunity_id UUID NOT NULL,
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'rejected')),
    topic_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, proposal_id),
    UNIQUE (account_id, opportunity_id, ordinal),
    FOREIGN KEY (account_id, opportunity_id)
        REFERENCES growth_opportunities(account_id, opportunity_id),
    FOREIGN KEY (account_id, topic_id)
        REFERENCES growth_topics(account_id, topic_id)
);

CREATE TABLE growth_tasks (
    account_id UUID NOT NULL,
    task_id UUID NOT NULL,
    opportunity_id UUID NOT NULL,
    topic_id UUID NOT NULL,
    question TEXT NOT NULL,
    followups JSONB NOT NULL,
    source_refs JSONB NOT NULL,
    estimated_minutes INTEGER NOT NULL CHECK (estimated_minutes BETWEEN 3 AND 5),
    progress TEXT NOT NULL DEFAULT 'ready'
        CHECK (progress IN ('ready', 'in_progress', 'paused', 'completed', 'dismissed')),
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, task_id),
    UNIQUE (account_id, opportunity_id),
    FOREIGN KEY (account_id, opportunity_id)
        REFERENCES growth_opportunities(account_id, opportunity_id),
    FOREIGN KEY (account_id, topic_id)
        REFERENCES growth_topics(account_id, topic_id)
);

CREATE TABLE growth_learning_attempts (
    account_id UUID NOT NULL,
    attempt_id UUID NOT NULL,
    task_id UUID NOT NULL,
    parent_attempt_id UUID,
    answer_text TEXT NOT NULL,
    answer_hash CHAR(64) NOT NULL,
    hint_level INTEGER NOT NULL CHECK (hint_level BETWEEN 0 AND 3),
    actor_origin TEXT NOT NULL CHECK (actor_origin IN ('user', 'simulated_user')),
    status TEXT NOT NULL DEFAULT 'accepted'
        CHECK (status IN ('accepted', 'feedback_pending', 'feedback_ready', 'feedback_failed')),
    feedback JSONB,
    feedback_origin TEXT,
    feedback_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    feedback_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, attempt_id),
    FOREIGN KEY (account_id, task_id)
        REFERENCES growth_tasks(account_id, task_id),
    FOREIGN KEY (account_id, parent_attempt_id)
        REFERENCES growth_learning_attempts(account_id, attempt_id)
);

CREATE TABLE growth_notes (
    account_id UUID NOT NULL,
    note_id UUID NOT NULL,
    task_id UUID NOT NULL,
    topic_id UUID NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    author TEXT NOT NULL CHECK (author = 'user'),
    content_text TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    source_refs JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, note_id),
    FOREIGN KEY (account_id, task_id)
        REFERENCES growth_tasks(account_id, task_id),
    FOREIGN KEY (account_id, topic_id)
        REFERENCES growth_topics(account_id, topic_id)
);

CREATE TABLE growth_timeline (
    account_id UUID NOT NULL,
    evidence_id UUID NOT NULL,
    task_id UUID NOT NULL,
    source_type TEXT NOT NULL,
    source_id UUID NOT NULL,
    evidence_label TEXT NOT NULL,
    actor_origin TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, evidence_id),
    UNIQUE (account_id, source_type, source_id),
    FOREIGN KEY (account_id, task_id)
        REFERENCES growth_tasks(account_id, task_id)
);
