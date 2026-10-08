CREATE TABLE growth_exports (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    export_id UUID NOT NULL,
    snapshot_seq BIGINT NOT NULL CHECK (snapshot_seq >= 0),
    json_payload JSONB NOT NULL,
    markdown_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, export_id)
);
