ALTER TABLE growth_notes ALTER COLUMN task_id DROP NOT NULL;

ALTER TABLE growth_topics ADD COLUMN parent_topic_id UUID;
ALTER TABLE growth_topics ADD CONSTRAINT growth_topics_parent_fk
    FOREIGN KEY (account_id, parent_topic_id)
    REFERENCES growth_topics(account_id, topic_id);
ALTER TABLE growth_topics ADD CONSTRAINT growth_topics_not_self_parent
    CHECK (parent_topic_id IS NULL OR parent_topic_id <> topic_id);

CREATE TABLE growth_topic_aliases (
    account_id UUID NOT NULL,
    alias_topic_id UUID NOT NULL,
    canonical_topic_id UUID NOT NULL,
    merged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation_id UUID NOT NULL,
    PRIMARY KEY (account_id, alias_topic_id),
    FOREIGN KEY (account_id, alias_topic_id)
        REFERENCES growth_topics(account_id, topic_id),
    FOREIGN KEY (account_id, canonical_topic_id)
        REFERENCES growth_topics(account_id, topic_id),
    CHECK (alias_topic_id <> canonical_topic_id)
);

CREATE TABLE growth_topic_moves (
    account_id UUID NOT NULL,
    move_id UUID NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('note', 'task')),
    entity_id UUID NOT NULL,
    from_topic_id UUID NOT NULL,
    to_topic_id UUID NOT NULL,
    operation_id UUID NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('merge', 'split_link')),
    moved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, move_id),
    FOREIGN KEY (account_id, from_topic_id)
        REFERENCES growth_topics(account_id, topic_id),
    FOREIGN KEY (account_id, to_topic_id)
        REFERENCES growth_topics(account_id, topic_id)
);

CREATE TABLE growth_topic_links (
    account_id UUID NOT NULL,
    topic_id UUID NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('note', 'task')),
    entity_id UUID NOT NULL,
    source_topic_id UUID NOT NULL,
    operation_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, topic_id, entity_type, entity_id),
    FOREIGN KEY (account_id, topic_id)
        REFERENCES growth_topics(account_id, topic_id),
    FOREIGN KEY (account_id, source_topic_id)
        REFERENCES growth_topics(account_id, topic_id)
);
