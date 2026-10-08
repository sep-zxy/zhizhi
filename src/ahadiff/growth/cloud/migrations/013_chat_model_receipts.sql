ALTER TABLE growth_chat_messages
  ADD COLUMN provider_name TEXT,
  ADD COLUMN model_name TEXT,
  ADD COLUMN provider_request_id TEXT,
  ADD COLUMN request_payload_hash CHAR(64),
  ADD COLUMN response_payload_hash CHAR(64),
  ADD COLUMN input_tokens INTEGER,
  ADD COLUMN output_tokens INTEGER,
  ADD COLUMN elapsed_ms INTEGER;

ALTER TABLE growth_chat_messages
  ADD CONSTRAINT growth_chat_model_receipt_complete CHECK (
    origin <> 'live' OR (
      provider_name IS NOT NULL AND model_name IS NOT NULL
      AND request_payload_hash IS NOT NULL AND response_payload_hash IS NOT NULL
      AND input_tokens >= 0 AND output_tokens >= 0 AND elapsed_ms >= 0
    )
  ) NOT VALID;
