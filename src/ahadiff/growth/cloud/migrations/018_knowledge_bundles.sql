CREATE TABLE growth_knowledge_bundles (
  account_id UUID NOT NULL REFERENCES growth_accounts(account_id),
  card_id UUID NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  project_ids UUID[] NOT NULL,
  bundle JSONB NOT NULL,
  payload_sha256 CHAR(64) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, card_id)
);
