ALTER TABLE growth_review_events
    ADD COLUMN answer_text TEXT NOT NULL DEFAULT ''
    CHECK (char_length(answer_text) <= 20000);
