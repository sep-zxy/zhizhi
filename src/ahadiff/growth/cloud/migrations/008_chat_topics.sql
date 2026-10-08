CREATE TABLE growth_chat_sessions (
    account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
    session_id UUID NOT NULL,
    project_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, session_id),
    FOREIGN KEY (account_id, project_id) REFERENCES growth_projects(account_id, project_id)
);

CREATE TABLE growth_chat_messages (
    account_id UUID NOT NULL,
    message_id UUID NOT NULL,
    session_id UUID NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    origin TEXT NOT NULL CHECK (origin IN ('user', 'live', 'replay')),
    content_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, message_id),
    FOREIGN KEY (account_id, session_id)
        REFERENCES growth_chat_sessions(account_id, session_id),
    CHECK ((role = 'user') = (origin = 'user'))
);

CREATE INDEX growth_chat_messages_session_idx
    ON growth_chat_messages(account_id, session_id, created_at, message_id);

CREATE TABLE growth_chat_suggestions (
    account_id UUID NOT NULL,
    suggestion_id UUID NOT NULL,
    session_id UUID NOT NULL,
    assistant_message_id UUID NOT NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'rejected')),
    topic_id UUID,
    decided_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, suggestion_id),
    FOREIGN KEY (account_id, session_id)
        REFERENCES growth_chat_sessions(account_id, session_id),
    FOREIGN KEY (account_id, assistant_message_id)
        REFERENCES growth_chat_messages(account_id, message_id),
    FOREIGN KEY (account_id, topic_id) REFERENCES growth_topics(account_id, topic_id)
);

CREATE INDEX growth_chat_suggestions_session_idx
    ON growth_chat_suggestions(account_id, session_id, suggestion_id);
