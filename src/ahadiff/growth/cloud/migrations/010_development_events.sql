CREATE TABLE growth_development_events (
    account_id UUID NOT NULL,
    event_id UUID NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    project_id UUID NOT NULL,
    feature_id UUID,
    event_type TEXT NOT NULL CHECK (event_type = 'coding_task_finished'),
    occurred_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    target_device_id UUID,
    expected_head_sha CHAR(40) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending_confirmation', 'targeted')),
    payload_hash CHAR(64) NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, event_id),
    FOREIGN KEY (account_id, project_id) REFERENCES growth_projects(account_id, project_id),
    FOREIGN KEY (account_id, feature_id) REFERENCES growth_features(account_id, feature_id),
    FOREIGN KEY (account_id, target_device_id)
        REFERENCES growth_devices(account_id, device_id)
);

CREATE INDEX growth_development_events_inbox_idx
    ON growth_development_events(account_id, target_device_id, created_at);
