CREATE TABLE growth_accounts (
    account_id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE growth_devices (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    device_id UUID NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, device_id)
);

CREATE TABLE growth_sync_heads (
    account_id UUID PRIMARY KEY REFERENCES growth_accounts(account_id),
    last_seq BIGINT NOT NULL DEFAULT 0 CHECK (last_seq >= 0)
);

CREATE TABLE growth_projects (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    project_id UUID NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    sync_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, project_id)
);

CREATE TABLE growth_features (
    account_id UUID NOT NULL,
    project_id UUID NOT NULL,
    feature_id UUID NOT NULL,
    label TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    start_base_sha CHAR(40) NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, feature_id),
    UNIQUE (account_id, project_id, feature_id),
    FOREIGN KEY (account_id, project_id)
        REFERENCES growth_projects(account_id, project_id)
);

CREATE TABLE growth_snapshots (
    account_id UUID NOT NULL,
    project_id UUID NOT NULL,
    feature_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    resolved_base_sha CHAR(40) NOT NULL,
    head_sha CHAR(40) NOT NULL,
    effective_tree_hash CHAR(64) NOT NULL,
    diff_hash CHAR(64) NOT NULL,
    capture_scope JSONB NOT NULL,
    privacy_policy_version TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, snapshot_id),
    UNIQUE (account_id, project_id, feature_id, snapshot_id),
    FOREIGN KEY (account_id, project_id, feature_id)
        REFERENCES growth_features(account_id, project_id, feature_id)
);

CREATE TABLE growth_source_refs (
    account_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    source_ref_id UUID NOT NULL,
    relative_path TEXT NOT NULL,
    blob_hash CHAR(64) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, source_ref_id),
    UNIQUE (account_id, snapshot_id, source_ref_id),
    FOREIGN KEY (account_id, snapshot_id)
        REFERENCES growth_snapshots(account_id, snapshot_id)
);

CREATE TABLE growth_analysis_runs (
    account_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    analysis_id UUID NOT NULL,
    input_fingerprint CHAR(64) NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    upstream_run_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, analysis_id),
    FOREIGN KEY (account_id, snapshot_id)
        REFERENCES growth_snapshots(account_id, snapshot_id)
);

CREATE TABLE growth_domain_events (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    event_id UUID NOT NULL,
    trace_id UUID NOT NULL,
    parent_event_id UUID,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, event_id)
);

CREATE TABLE growth_change_feed (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    change_seq BIGINT NOT NULL CHECK (change_seq > 0),
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    deleted_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, change_seq)
);

CREATE TABLE growth_sync_operations (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    operation_id UUID NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    status_code INTEGER NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, operation_id)
);
