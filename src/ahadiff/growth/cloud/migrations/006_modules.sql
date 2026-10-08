CREATE TABLE growth_modules (
    account_id UUID NOT NULL,
    project_id UUID NOT NULL,
    module_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    index_revision CHAR(64) NOT NULL,
    effective_tree_hash CHAR(64) NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    member_paths JSONB NOT NULL,
    flow JSONB NOT NULL,
    locked BOOLEAN NOT NULL DEFAULT FALSE,
    pending_adjustment JSONB,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, module_id),
    FOREIGN KEY (account_id, project_id) REFERENCES growth_projects(account_id, project_id),
    FOREIGN KEY (account_id, snapshot_id) REFERENCES growth_snapshots(account_id, snapshot_id)
);

CREATE INDEX growth_modules_project_idx ON growth_modules(account_id, project_id, module_id);
