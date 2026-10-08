-- Source metadata remains code-free until a user explicitly approves an excerpt.
ALTER TABLE growth_source_refs ADD COLUMN approved_excerpt_id UUID;

CREATE TABLE growth_source_excerpts (
    account_id UUID NOT NULL,
    source_ref_id UUID NOT NULL,
    excerpt_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    blob_hash CHAR(64) NOT NULL,
    start_line INTEGER NOT NULL CHECK (start_line > 0),
    end_line INTEGER NOT NULL CHECK (end_line >= start_line),
    content_text TEXT NOT NULL CHECK (octet_length(content_text) BETWEEN 1 AND 8192),
    content_hash CHAR(64) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision = 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, source_ref_id),
    UNIQUE (account_id, excerpt_id),
    FOREIGN KEY (account_id, source_ref_id)
        REFERENCES growth_source_refs(account_id, source_ref_id),
    FOREIGN KEY (account_id, snapshot_id)
        REFERENCES growth_snapshots(account_id, snapshot_id)
);
