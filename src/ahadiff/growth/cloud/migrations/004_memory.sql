CREATE TABLE growth_memory_jobs (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    source_type TEXT NOT NULL CHECK (source_type IN ('note', 'attempt')),
    source_id UUID NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    projection_key CHAR(64) NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('upsert', 'delete')),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'skipped')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, source_type, source_id, source_version),
    UNIQUE (projection_key)
);

CREATE INDEX growth_memory_jobs_ready ON growth_memory_jobs
    (next_retry_at, created_at)
    WHERE state IN ('pending', 'failed', 'running');

CREATE TABLE growth_memory_current (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    source_type TEXT NOT NULL,
    source_id UUID NOT NULL,
    source_version INTEGER NOT NULL,
    projection_key CHAR(64) NOT NULL,
    reme_path TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, source_type, source_id)
);
