CREATE TABLE growth_note_revisions (
    account_id UUID NOT NULL,
    note_id UUID NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    content_text TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    source_conflict_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, note_id, revision),
    FOREIGN KEY (account_id, note_id) REFERENCES growth_notes(account_id, note_id)
);

CREATE TABLE growth_note_conflicts (
    account_id UUID NOT NULL,
    conflict_id UUID NOT NULL,
    note_id UUID NOT NULL,
    base_revision INTEGER NOT NULL,
    observed_server_revision INTEGER NOT NULL,
    proposed_text TEXT NOT NULL,
    proposed_hash CHAR(64) NOT NULL,
    resolved_revision INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, conflict_id),
    FOREIGN KEY (account_id, note_id) REFERENCES growth_notes(account_id, note_id),
    FOREIGN KEY (account_id, note_id, base_revision)
        REFERENCES growth_note_revisions(account_id, note_id, revision)
);

INSERT INTO growth_note_revisions (account_id, note_id, revision, content_text, content_hash)
SELECT account_id, note_id, revision, content_text, content_hash FROM growth_notes;

ALTER TABLE growth_devices ADD COLUMN last_change_seq BIGINT NOT NULL DEFAULT 0;
ALTER TABLE growth_change_feed ADD COLUMN event_type TEXT;
