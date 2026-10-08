"""Local growth ledger: stable IDs, source mappings and causal events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ahadiff.core.orchestrator import LearnRequest, run_learn_pipeline

from .git_snapshot import CapturedSnapshot, capture_commit, capture_worktree, git_text, repository_root, worktree_id
from .learning import LEARNING_SCHEMA
from .sync import SYNC_SCHEMA, SYNC_V5_SCHEMA, SYNC_V11_SCHEMA


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
  binding_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  canonical_local_path TEXT NOT NULL, worktree_id TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(project_id, canonical_local_path)
);
CREATE TABLE IF NOT EXISTS features (
  feature_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  label TEXT NOT NULL, base_ref TEXT NOT NULL, start_base_sha TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY, feature_id TEXT NOT NULL REFERENCES features(feature_id),
  resolved_base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
  effective_tree_hash TEXT NOT NULL, diff_hash TEXT NOT NULL,
  capture_scope TEXT NOT NULL, privacy_policy_version TEXT NOT NULL,
  patch_text TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(feature_id, resolved_base_sha, head_sha, effective_tree_hash, diff_hash, capture_scope)
);
CREATE TABLE IF NOT EXISTS source_refs (
  source_ref_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  relative_path TEXT NOT NULL, blob_hash TEXT NOT NULL, content BLOB NOT NULL,
  deleted INTEGER NOT NULL, UNIQUE(snapshot_id, relative_path)
);
CREATE TABLE IF NOT EXISTS analysis_runs (
  analysis_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  request_id TEXT NOT NULL, input_fingerprint TEXT NOT NULL, mode TEXT NOT NULL,
  status TEXT NOT NULL, upstream_run_id TEXT, error TEXT, created_at TEXT NOT NULL,
  UNIQUE(snapshot_id, request_id)
);
CREATE TABLE IF NOT EXISTS analysis_attempts (
  attempt_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analysis_runs(analysis_id),
  attempt_no INTEGER NOT NULL, status TEXT NOT NULL, upstream_run_id TEXT,
  error TEXT, created_at TEXT NOT NULL, UNIQUE(analysis_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS growth_events (
  event_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_event_id TEXT,
  entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_growth_events_entity ON growth_events(entity_type, entity_id);
"""

SNAPSHOT_BINDINGS_SCHEMA = """
CREATE TABLE snapshot_bindings (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
  captured_at TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, binding_id)
);
"""

MODEL_REQUESTS_SCHEMA = """
CREATE TABLE analysis_model_requests (
  analysis_id TEXT PRIMARY KEY REFERENCES analysis_runs(analysis_id),
  binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
  provider_name TEXT NOT NULL, provider_config_hash TEXT NOT NULL,
  source_ref_ids_json TEXT NOT NULL, approved_payload_hash TEXT NOT NULL,
  approved_payload_text TEXT NOT NULL, selected_patch TEXT NOT NULL,
  approved_at TEXT NOT NULL
);
CREATE TABLE analysis_model_responses (
  analysis_id TEXT PRIMARY KEY REFERENCES analysis_runs(analysis_id),
  response_text TEXT NOT NULL, provider_request_id TEXT,
  input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  elapsed_ms INTEGER NOT NULL, created_at TEXT NOT NULL
);
"""

FEEDBACK_REQUESTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_model_requests (
  attempt_id TEXT PRIMARY KEY REFERENCES learning_attempts(attempt_id),
  binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
  provider_name TEXT NOT NULL, provider_config_hash TEXT NOT NULL,
  approved_payload_hash TEXT NOT NULL, approved_payload_text TEXT NOT NULL,
  request_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_model_attempts (
  generation_attempt_id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES feedback_model_requests(attempt_id),
  attempt_no INTEGER NOT NULL, approval_hash TEXT NOT NULL,
  status TEXT NOT NULL, provider_request_id TEXT, error TEXT,
  created_at TEXT NOT NULL, UNIQUE(attempt_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS feedback_model_responses (
  attempt_id TEXT PRIMARY KEY REFERENCES feedback_model_requests(attempt_id),
  response_text TEXT NOT NULL, provider_request_id TEXT,
  input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  elapsed_ms INTEGER NOT NULL, created_at TEXT NOT NULL
);
"""

TASK_HINTS_SCHEMA = """
CREATE TABLE growth_task_hints (
  hint_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES growth_tasks(task_id),
  request_id TEXT NOT NULL UNIQUE,
  level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
  hint_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(task_id, level)
);
"""


PROJECT_POLICY_V12_SCHEMA = """
CREATE TABLE project_policies (
  project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
  local_processing INTEGER NOT NULL DEFAULT 1 CHECK(local_processing IN (0, 1)),
  model_allowed INTEGER NOT NULL DEFAULT 0 CHECK(model_allowed IN (0, 1)),
  cloud_allowed INTEGER NOT NULL DEFAULT 0 CHECK(cloud_allowed IN (0, 1)),
  cloud_account_id TEXT, cloud_origin TEXT, last_publish_error TEXT,
  updated_at TEXT NOT NULL
);
INSERT INTO project_policies(project_id, updated_at)
SELECT project_id, created_at FROM projects;
CREATE TABLE sync_publication_links (
  account_id TEXT NOT NULL, operation_id TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  PRIMARY KEY(account_id, operation_id),
  FOREIGN KEY(account_id, operation_id)
    REFERENCES sync_outbox(account_id, operation_id)
);
"""

DEVELOPMENT_EVENTS_V13_SCHEMA = """
CREATE TABLE local_development_event_captures (
  account_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  feature_id TEXT NOT NULL REFERENCES features(feature_id),
  binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
  expected_head_sha TEXT NOT NULL,
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  analysis_id TEXT REFERENCES analysis_runs(analysis_id),
  captured_at TEXT NOT NULL,
  PRIMARY KEY(account_id, event_id)
);
"""

CHAT_MODEL_V14_SCHEMA = """
CREATE TABLE local_chat_model_requests (
  account_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  message_id TEXT NOT NULL UNIQUE,
  provider_name TEXT NOT NULL,
  approval_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running', 'ready', 'failed')),
  response_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_id, request_id),
  FOREIGN KEY(account_id) REFERENCES sync_accounts(account_id)
);
"""

KNOWLEDGE_CARDS_V20_SCHEMA = """
CREATE TABLE knowledge_cards (
  card_id TEXT PRIMARY KEY REFERENCES growth_tasks(task_id),
  learning_goal TEXT NOT NULL,
  back_answer TEXT NOT NULL,
  back_explanation TEXT NOT NULL,
  card_version INTEGER NOT NULL DEFAULT 1 CHECK(card_version > 0),
  source_link_status TEXT NOT NULL DEFAULT 'unverified'
    CHECK(source_link_status IN ('linked', 'unverified')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE card_source_commits (
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  commit_sha TEXT NOT NULL,
  binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
  link_basis TEXT NOT NULL CHECK(link_basis IN ('confirmed', 'captured')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(card_id, commit_sha)
);
CREATE INDEX idx_card_source_commits_sha ON card_source_commits(commit_sha);
"""

GIT_HISTORY_V21_SCHEMA = """
CREATE TABLE git_history_scans (
  binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id),
  scan_sequence INTEGER NOT NULL CHECK(scan_sequence > 0),
  scanned_at TEXT NOT NULL
);
CREATE TABLE git_history_commits (
  binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
  commit_sha TEXT NOT NULL,
  title TEXT NOT NULL, author_name TEXT NOT NULL, author_email TEXT NOT NULL,
  authored_at TEXT NOT NULL, paths_json TEXT NOT NULL, category TEXT NOT NULL,
  first_seen_scan INTEGER NOT NULL, last_seen_scan INTEGER NOT NULL,
  reachable INTEGER NOT NULL CHECK(reachable IN (0, 1)),
  PRIMARY KEY(binding_id, commit_sha)
);
CREATE INDEX idx_git_history_page ON git_history_commits(
  binding_id, reachable, authored_at DESC, commit_sha DESC
);
CREATE TABLE git_author_identities (
  binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
  author_email TEXT NOT NULL, group_name TEXT NOT NULL,
  PRIMARY KEY(binding_id, author_email)
);
"""

RECOMMENDATIONS_V22_SCHEMA = """
ALTER TABLE opportunities ADD COLUMN recommendation_key TEXT;
ALTER TABLE opportunities ADD COLUMN recommendation_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(recommendation_status IN ('pending', 'deferred', 'ignored', 'confirmed'));
ALTER TABLE opportunities ADD COLUMN suggested_card_count INTEGER NOT NULL DEFAULT 1
  CHECK(suggested_card_count BETWEEN 0 AND 3);
ALTER TABLE opportunities ADD COLUMN canonical_card_id TEXT REFERENCES growth_tasks(task_id);
CREATE INDEX idx_recommendations_key ON opportunities(recommendation_key);
CREATE TABLE recommendation_decisions (
  request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


KNOWLEDGE_FIRST_V23_SCHEMA = """
CREATE TABLE knowledge_concepts (
  concept_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  canonical_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE knowledge_concept_aliases (
  concept_id TEXT NOT NULL REFERENCES knowledge_concepts(concept_id),
  alias_key TEXT NOT NULL,
  alias_text TEXT NOT NULL,
  PRIMARY KEY(concept_id, alias_key)
);
CREATE INDEX idx_knowledge_concept_alias ON knowledge_concept_aliases(alias_key);
CREATE TABLE knowledge_candidate_decisions (
  request_id TEXT PRIMARY KEY,
  request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE card_source_commits_v23 (
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  commit_sha TEXT NOT NULL,
  binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
  link_basis TEXT NOT NULL CHECK(link_basis IN ('confirmed', 'captured')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(card_id, binding_id, commit_sha)
);
INSERT INTO card_source_commits_v23
SELECT card_id,commit_sha,binding_id,link_basis,created_at FROM card_source_commits;
DROP TABLE card_source_commits;
ALTER TABLE card_source_commits_v23 RENAME TO card_source_commits;
CREATE INDEX idx_card_source_commits_sha ON card_source_commits(binding_id,commit_sha);
ALTER TABLE knowledge_cards ADD COLUMN concept_id TEXT REFERENCES knowledge_concepts(concept_id);
CREATE UNIQUE INDEX idx_knowledge_cards_concept ON knowledge_cards(concept_id);
"""


KNOWLEDGE_LEARNING_V24_SCHEMA = """
CREATE TABLE card_learning_materials (
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  card_version INTEGER NOT NULL,
  front_question TEXT NOT NULL,
  back_summary TEXT NOT NULL,
  back_mechanism TEXT NOT NULL,
  back_boundary TEXT NOT NULL,
  misconceptions_json TEXT NOT NULL,
  source_refs_json TEXT NOT NULL,
  provider_name TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(card_id,card_version)
);
CREATE TABLE card_objective_questions (
  question_id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  card_version INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('understanding','prediction')),
  version INTEGER NOT NULL,
  stem TEXT NOT NULL,
  scenario TEXT NOT NULL,
  options_json TEXT NOT NULL,
  correct_option_id TEXT NOT NULL,
  explanations_json TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  boundary TEXT NOT NULL,
  source_refs_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(card_id,card_version,kind)
);
CREATE TABLE card_learning_progress (
  card_id TEXT PRIMARY KEY REFERENCES knowledge_cards(card_id),
  card_version INTEGER NOT NULL,
  stage TEXT NOT NULL DEFAULT 'front' CHECK(stage IN
    ('front','back','understanding','understanding_explanation',
     'prediction','prediction_explanation','completed')),
  option_drafts_json TEXT NOT NULL DEFAULT '{}',
  note_text TEXT NOT NULL DEFAULT '',
  back_seen_at TEXT,
  completed_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE card_objective_answers (
  answer_id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  question_id TEXT NOT NULL REFERENCES card_objective_questions(question_id),
  question_version INTEGER NOT NULL,
  option_id TEXT NOT NULL,
  correct INTEGER NOT NULL CHECK(correct IN (0,1)),
  assisted INTEGER NOT NULL DEFAULT 0 CHECK(assisted IN (0,1)),
  request_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  UNIQUE(card_id,question_id,question_version)
);
CREATE TABLE card_explanation_reads (
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  question_id TEXT NOT NULL REFERENCES card_objective_questions(question_id),
  question_version INTEGER NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  read_at TEXT NOT NULL,
  PRIMARY KEY(card_id,question_id,question_version)
);
CREATE TABLE card_followup_marks (
  card_id TEXT PRIMARY KEY REFERENCES knowledge_cards(card_id),
  mark TEXT NOT NULL CHECK(mark IN ('confused','revisit')),
  reason TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE card_chat_exchanges (
  request_id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  provider_name TEXT NOT NULL,
  user_text TEXT NOT NULL,
  reply_text TEXT NOT NULL,
  context_json TEXT NOT NULL,
  provider_request_id TEXT,
  created_at TEXT NOT NULL
);
"""


KNOWLEDGE_WIKI_V25_SCHEMA = """
CREATE TABLE knowledge_vault_settings (
  setting_id INTEGER PRIMARY KEY CHECK(setting_id=1),
  vault_path TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE knowledge_wiki_articles (
  concept_id TEXT PRIMARY KEY REFERENCES knowledge_concepts(concept_id),
  article_id TEXT NOT NULL UNIQUE,
  relative_path TEXT NOT NULL UNIQUE,
  file_hash TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version > 0),
  published_at TEXT NOT NULL
);
CREATE TABLE knowledge_wiki_drafts (
  draft_id TEXT PRIMARY KEY,
  concept_id TEXT NOT NULL REFERENCES knowledge_concepts(concept_id),
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  request_id TEXT NOT NULL UNIQUE,
  target_relative_path TEXT NOT NULL,
  original_hash TEXT,
  original_markdown TEXT,
  markdown TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','published')),
  publish_request_id TEXT UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_knowledge_wiki_drafts_card ON knowledge_wiki_drafts(card_id,status);
"""


KNOWLEDGE_DISCUSSION_V26_SCHEMA = """
CREATE TABLE knowledge_candidate_discussions (
  session_id TEXT NOT NULL REFERENCES local_explorations(session_id),
  opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
  linked_at TEXT NOT NULL,
  PRIMARY KEY(session_id,opportunity_id)
);
CREATE INDEX idx_knowledge_candidate_discussion_opportunity
  ON knowledge_candidate_discussions(opportunity_id);
"""

KNOWLEDGE_REVISION_V27_SCHEMA = """
CREATE TABLE card_learning_progress_history (
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  card_version INTEGER NOT NULL,
  progress_json TEXT NOT NULL,
  archived_at TEXT NOT NULL,
  PRIMARY KEY(card_id,card_version)
);
CREATE TABLE card_material_revisions (
  request_id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  previous_version INTEGER NOT NULL,
  new_version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(card_id,new_version)
);
"""

KNOWLEDGE_SPLIT_V28_SCHEMA = """
CREATE TABLE knowledge_candidate_overrides (
  opportunity_id TEXT PRIMARY KEY REFERENCES opportunities(opportunity_id),
  concept_key TEXT NOT NULL,
  title TEXT NOT NULL,
  reason TEXT NOT NULL,
  request_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

KNOWLEDGE_SYNC_V29_SCHEMA = """
CREATE TABLE knowledge_sync_state (
  account_id TEXT NOT NULL REFERENCES sync_accounts(account_id),
  card_id TEXT NOT NULL REFERENCES knowledge_cards(card_id),
  revision INTEGER NOT NULL CHECK(revision > 0),
  payload_sha256 TEXT NOT NULL,
  conflict_json TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_id,card_id)
);
"""

class GrowthLocalRepository:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 31:
            raise RuntimeError(f"本地成长库版本过新：{version}")
        if version == 0:
            with self.connection:
                self.connection.executescript(SCHEMA)
                self.connection.execute("PRAGMA user_version=2")
        elif version == 1:
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS analysis_attempts ("
                    "attempt_id TEXT PRIMARY KEY, "
                    "analysis_id TEXT NOT NULL REFERENCES analysis_runs(analysis_id), "
                    "attempt_no INTEGER NOT NULL, status TEXT NOT NULL, "
                    "upstream_run_id TEXT, error TEXT, created_at TEXT NOT NULL, "
                    "UNIQUE(analysis_id, attempt_no))"
                )
                self.connection.execute("PRAGMA user_version=2")
        if version <= 2:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + LEARNING_SCHEMA + "\nPRAGMA user_version=3;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 3:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + SYNC_SCHEMA + "\nPRAGMA user_version=4;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 4:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + SYNC_V5_SCHEMA + "\nPRAGMA user_version=5;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 5:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + SNAPSHOT_BINDINGS_SCHEMA
                    + "\nPRAGMA user_version=6;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 6:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + MODEL_REQUESTS_SCHEMA
                    + "\nPRAGMA user_version=7;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 7:
            with self.connection:
                columns = {
                    row["name"] for row in self.connection.execute(
                        "PRAGMA table_info(analysis_runs)"
                    )
                }
                if "supersedes_analysis_id" not in columns:
                    self.connection.execute(
                        "ALTER TABLE analysis_runs ADD COLUMN supersedes_analysis_id TEXT "
                        "REFERENCES analysis_runs(analysis_id)"
                    )
                self.connection.execute("PRAGMA user_version=8")
        if version <= 8:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + FEEDBACK_REQUESTS_SCHEMA
                    + "\nPRAGMA user_version=9;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 9:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + TASK_HINTS_SCHEMA
                    + "\nPRAGMA user_version=10;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 10:
            with self.connection:
                columns = {
                    row["name"] for row in self.connection.execute(
                        "PRAGMA table_info(sync_accounts)"
                    )
                }
                if "cloud_origin" not in columns:
                    self.connection.execute(SYNC_V11_SCHEMA)
                self.connection.execute("PRAGMA user_version=11")
        if version <= 11:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + PROJECT_POLICY_V12_SCHEMA
                )
                self._backfill_publication_links()
                self.connection.execute("PRAGMA user_version=12")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        if version <= 12:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + DEVELOPMENT_EVENTS_V13_SCHEMA
                    + "\nPRAGMA user_version=13;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 13:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + CHAT_MODEL_V14_SCHEMA
                    + "\nPRAGMA user_version=14;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 14:
            with self.connection:
                columns = {row["name"] for row in self.connection.execute(
                    "PRAGMA table_info(growth_tasks)"
                )}
                for name in ("module_id", "module_index_revision"):
                    if name not in columns:
                        self.connection.execute(
                            f"ALTER TABLE growth_tasks ADD COLUMN {name} TEXT"
                        )
                self.connection.execute("PRAGMA user_version=15")
        if version <= 15:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE opportunities ADD COLUMN card_json TEXT"
                )
                self.connection.execute("PRAGMA user_version=16")
        if version <= 16:
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS local_explorations ("
                    "session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), "
                    "messages_json TEXT NOT NULL, suggestion_json TEXT, topic_id TEXT, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                self.connection.execute("PRAGMA user_version=17")
        if version <= 17:
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS growth_task_materials ("
                    "task_id TEXT PRIMARY KEY REFERENCES growth_tasks(task_id), "
                    "run_id TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL)"
                )
                self.connection.execute("PRAGMA user_version=18")
        if version <= 18:
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS growth_material_attempts ("
                    "attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES growth_tasks(task_id), "
                    "question_id TEXT NOT NULL, answer_text TEXT NOT NULL, "
                    "created_at TEXT NOT NULL)"
                )
                self.connection.execute("PRAGMA user_version=19")
        if version <= 19:
            with self.connection:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + KNOWLEDGE_CARDS_V20_SCHEMA)
                rows = self.connection.execute(
                    "SELECT tasks.task_id, tasks.created_at, opportunities.learning_goal, "
                    "opportunities.card_json FROM growth_tasks AS tasks "
                    "JOIN opportunities USING(opportunity_id)"
                ).fetchall()
                for row in rows:
                    draft = json.loads(row["card_json"]) if row["card_json"] else {}
                    self.connection.execute(
                        "INSERT INTO knowledge_cards "
                        "(card_id,learning_goal,back_answer,back_explanation,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (row["task_id"], row["learning_goal"],
                         draft.get("expected_answer", ""), draft.get("context", ""),
                         row["created_at"], row["created_at"]),
                    )
                self.connection.execute("PRAGMA user_version=20")
        if version <= 20:
            with self.connection:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + GIT_HISTORY_V21_SCHEMA)
                legacy_cards = self.connection.execute(
                    "SELECT tasks.task_id, snapshots.capture_scope, "
                    "snapshot_bindings.binding_id FROM growth_tasks AS tasks "
                    "JOIN opportunities USING(opportunity_id) "
                    "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
                    "JOIN snapshot_bindings USING(snapshot_id) "
                    "ORDER BY snapshot_bindings.captured_at DESC"
                ).fetchall()
                for card in legacy_cards:
                    scope = json.loads(card["capture_scope"])
                    if scope.get("kind") != "commit" or not scope.get("sha"):
                        continue
                    self.connection.execute(
                        "INSERT OR IGNORE INTO card_source_commits VALUES (?,?,?,?,?)",
                        (card["task_id"], scope["sha"], card["binding_id"],
                         "captured", _now()),
                    )
                    self.connection.execute(
                        "UPDATE knowledge_cards SET source_link_status='linked' WHERE card_id=?",
                        (card["task_id"],),
                    )
                self.connection.execute("PRAGMA user_version=21")
        if version <= 21:
            with self.connection:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + RECOMMENDATIONS_V22_SCHEMA)
                rows = self.connection.execute(
                    "SELECT opportunities.opportunity_id, opportunities.learning_goal, "
                    "features.project_id, growth_tasks.task_id "
                    "FROM opportunities JOIN analysis_runs USING(analysis_id) "
                    "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
                    "LEFT JOIN growth_tasks USING(opportunity_id)"
                ).fetchall()
                for row in rows:
                    key = hashlib.sha256((str(row["project_id"]) + ":" +
                        " ".join(str(row["learning_goal"]).casefold().split())).encode()).hexdigest()
                    self.connection.execute(
                        "UPDATE opportunities SET recommendation_key=?, "
                        "recommendation_status=?, canonical_card_id=? WHERE opportunity_id=?",
                        (key, "confirmed" if row["task_id"] else "pending",
                         row["task_id"], row["opportunity_id"]),
                    )
                self.connection.execute("PRAGMA user_version=22")
        if version <= 22:
            try:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + KNOWLEDGE_FIRST_V23_SCHEMA)
                for card in self.connection.execute(
                    "SELECT card_id,learning_goal,created_at,updated_at FROM knowledge_cards"
                ).fetchall():
                    concept_id = _id()
                    # A legacy card remains a distinct concept until a user reviews a merge.
                    canonical_key = "legacy:" + str(card["card_id"])
                    self.connection.execute(
                        "INSERT INTO knowledge_concepts VALUES (?,?,?,?,?)",
                        (concept_id, str(card["learning_goal"]), canonical_key,
                         str(card["created_at"]), str(card["updated_at"])),
                    )
                    self.connection.execute(
                        "UPDATE knowledge_cards SET concept_id=? WHERE card_id=?",
                        (concept_id, card["card_id"]),
                    )
                self.connection.execute("PRAGMA user_version=23")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        if version <= 23:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + KNOWLEDGE_LEARNING_V24_SCHEMA
                    + "\nPRAGMA user_version=24;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 24:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + KNOWLEDGE_WIKI_V25_SCHEMA
                    + "\nPRAGMA user_version=25;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 25:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + KNOWLEDGE_DISCUSSION_V26_SCHEMA
                    + "\nPRAGMA user_version=26;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 26:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + KNOWLEDGE_REVISION_V27_SCHEMA
                    + "\nPRAGMA user_version=27;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 27:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + KNOWLEDGE_SPLIT_V28_SCHEMA
                    + "\nPRAGMA user_version=28;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 28:
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + KNOWLEDGE_SYNC_V29_SCHEMA
                    + "\nPRAGMA user_version=29;\nCOMMIT;\n"
                )
            except sqlite3.Error:
                self.connection.rollback()
                raise
        if version <= 29:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE knowledge_vault_settings ADD COLUMN "
                    "target_folder TEXT NOT NULL DEFAULT '工程成长伴侣'"
                )
                self.connection.execute("PRAGMA user_version=30")
        if version <= 30:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE local_explorations ADD COLUMN title TEXT"
                )
                self.connection.execute("PRAGMA user_version=31")

    def _backfill_publication_links(self) -> None:
        """Keep v11 approved publications subject to v12 project switches."""
        queries = {
            "project": "SELECT project_id FROM projects WHERE project_id=?",
            "feature": "SELECT project_id FROM features WHERE feature_id=?",
            "snapshot": "SELECT features.project_id FROM snapshots "
                        "JOIN features USING(feature_id) WHERE snapshot_id=?",
            "analysis": "SELECT features.project_id FROM analysis_runs "
                        "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
                        "WHERE analysis_id=?",
            "task": "SELECT features.project_id FROM growth_tasks "
                    "JOIN opportunities USING(opportunity_id) "
                    "JOIN analysis_runs USING(analysis_id) "
                    "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
                    "WHERE task_id=?",
            "attempt": "SELECT features.project_id FROM learning_attempts "
                       "JOIN growth_tasks USING(task_id) "
                       "JOIN opportunities USING(opportunity_id) "
                       "JOIN analysis_runs USING(analysis_id) "
                       "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
                       "WHERE attempt_id=?",
            "note": "SELECT features.project_id FROM engineering_notes "
                    "JOIN growth_tasks USING(task_id) "
                    "JOIN opportunities USING(opportunity_id) "
                    "JOIN analysis_runs USING(analysis_id) "
                    "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
                    "WHERE note_id=?",
        }
        queries["feedback"] = queries["attempt"]
        rows = self.connection.execute(
            "SELECT outbox.account_id, outbox.operation_id, outbox.target_type, "
            "outbox.target_id, accounts.cloud_origin FROM sync_outbox outbox "
            "JOIN sync_accounts accounts ON accounts.account_id=outbox.account_id "
            "LEFT JOIN sync_publication_links links "
            "ON links.account_id=outbox.account_id AND links.operation_id=outbox.operation_id "
            "WHERE links.operation_id IS NULL AND outbox.target_type IS NOT NULL"
        ).fetchall()
        for row in rows:
            kind, target_id = str(row["target_type"]), str(row["target_id"])
            if kind in {"topic_decision", "progress"}:
                event = self.connection.execute(
                    "SELECT entity_id FROM growth_events WHERE event_id=?", (target_id,)
                ).fetchone()
                if event is None:
                    continue
                target_id = str(event["entity_id"])
                kind = "task" if kind == "progress" else "proposal"
            if kind == "proposal":
                query = ("SELECT features.project_id FROM topic_proposals "
                         "JOIN opportunities USING(opportunity_id) "
                         "JOIN analysis_runs USING(analysis_id) "
                         "JOIN snapshots USING(snapshot_id) "
                         "JOIN features USING(feature_id) WHERE proposal_id=?")
            else:
                query = queries.get(kind)
            if query is None:
                continue
            project = self.connection.execute(query, (target_id,)).fetchone()
            if project is None:
                continue
            project_id = str(project["project_id"])
            owner = self.connection.execute(
                "SELECT cloud_account_id FROM project_policies WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if owner is None or owner["cloud_account_id"] not in {
                None, row["account_id"]
            }:
                raise RuntimeError("旧版本本机项目存在跨账号发布记录")
            self.connection.execute(
                "UPDATE project_policies SET cloud_allowed=1, "
                "cloud_account_id=?, cloud_origin=COALESCE(cloud_origin, ?), "
                "updated_at=? WHERE project_id=?",
                (row["account_id"], row["cloud_origin"], _now(), project_id),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO sync_publication_links VALUES (?, ?, ?)",
                (row["account_id"], row["operation_id"], project_id),
            )

    def close(self) -> None:
        self.connection.close()

    def recover_interrupted_analyses(self) -> int:
        rows = self.connection.execute(
            "SELECT analysis_id FROM analysis_runs "
            "WHERE status IN ('queued', 'running')"
        ).fetchall()
        if not rows:
            return 0
        reason = "应用上次运行中断；可沿原分析 ID 重试"
        with self.connection:
            for row in rows:
                analysis_id = str(row["analysis_id"])
                self.connection.execute(
                    "UPDATE analysis_runs SET status='failed', error=? WHERE analysis_id=?",
                    (reason, analysis_id),
                )
                self.connection.execute(
                    "UPDATE analysis_attempts SET status='failed', error=? "
                    "WHERE analysis_id=? AND status='running'",
                    (reason, analysis_id),
                )
                self.record_event(
                    _id(), "analysis", analysis_id, "analysis_interrupted", {}
                )
        return len(rows)

    def recover_interrupted_feedback(self) -> int:
        rows = self.connection.execute(
            "SELECT attempt_id FROM feedback_model_requests "
            "WHERE status IN ('queued', 'running')"
        ).fetchall()
        reason = "应用上次运行中断；可沿原回答重试反馈"
        with self.connection:
            for row in rows:
                attempt_id = str(row["attempt_id"])
                self.connection.execute(
                    "UPDATE feedback_model_requests SET status='failed', error=? "
                    "WHERE attempt_id=?", (reason, attempt_id),
                )
                self.connection.execute(
                    "UPDATE feedback_model_attempts SET status='failed', error=? "
                    "WHERE attempt_id=? AND status IN ('queued', 'running')",
                    (reason, attempt_id),
                )
                self.connection.execute(
                    "UPDATE learning_attempts SET status='feedback_failed', "
                    "feedback_error=? WHERE attempt_id=? AND status='feedback_pending'",
                    (reason, attempt_id),
                )
                self.record_event(
                    _id(), "attempt", attempt_id, "feedback_interrupted", {},
                )
        return len(rows)

    def __enter__(self) -> GrowthLocalRepository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def record_event(
        self,
        trace_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        payload: dict[str, Any],
        parent_event_id: str | None = None,
    ) -> str:
        event_id = _id()
        self.connection.execute(
            "INSERT INTO growth_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                trace_id,
                parent_event_id,
                entity_type,
                entity_id,
                event_type,
                _json(payload),
                _now(),
            ),
        )
        if event_type in {
            "feature_started", "topic_confirmed", "topic_rejected",
            "task_created", "answer_saved", "feedback_ready",
            "task_progress_changed", "note_saved",
        }:
            self.queue_enabled_publications(
                self._project_id_for_publication_entity(entity_type, entity_id)
            )
        return event_id

    def _project_id_for_publication_entity(self, entity_type: str, entity_id: str) -> str:
        queries = {
            "feature": "SELECT project_id FROM features WHERE feature_id=?",
            "topic_proposal": "SELECT features.project_id FROM topic_proposals "
                              "JOIN opportunities USING(opportunity_id) "
                              "JOIN analysis_runs USING(analysis_id) "
                              "JOIN snapshots USING(snapshot_id) "
                              "JOIN features USING(feature_id) WHERE proposal_id=?",
            "task": "SELECT features.project_id FROM growth_tasks "
                    "JOIN opportunities USING(opportunity_id) "
                    "JOIN analysis_runs USING(analysis_id) "
                    "JOIN snapshots USING(snapshot_id) "
                    "JOIN features USING(feature_id) WHERE task_id=?",
            "attempt": "SELECT features.project_id FROM learning_attempts "
                       "JOIN growth_tasks USING(task_id) "
                       "JOIN opportunities USING(opportunity_id) "
                       "JOIN analysis_runs USING(analysis_id) "
                       "JOIN snapshots USING(snapshot_id) "
                       "JOIN features USING(feature_id) WHERE attempt_id=?",
            "note": "SELECT features.project_id FROM engineering_notes "
                    "JOIN growth_tasks USING(task_id) "
                    "JOIN opportunities USING(opportunity_id) "
                    "JOIN analysis_runs USING(analysis_id) "
                    "JOIN snapshots USING(snapshot_id) "
                    "JOIN features USING(feature_id) WHERE note_id=?",
        }
        row = self.connection.execute(queries[entity_type], (entity_id,)).fetchone()
        if row is None:
            raise ValueError("本机成长记录的项目关联缺失")
        return str(row["project_id"])

    def require_project_permission(self, project_id: str, permission: str) -> None:
        if permission not in {"local_processing", "model_allowed"}:
            raise ValueError("未知项目权限")
        row = self.connection.execute(
            f"SELECT {permission} FROM project_policies WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row is None or not row[permission]:
            raise ValueError("项目尚未允许此类处理")

    def project_id_for_snapshot(self, snapshot_id: str) -> str:
        row = self.connection.execute(
            "SELECT features.project_id FROM snapshots JOIN features USING(feature_id) "
            "WHERE snapshots.snapshot_id=?", (snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError("快照不存在")
        return str(row["project_id"])

    def project_id_for_attempt(self, attempt_id: str) -> str:
        row = self.connection.execute(
            "SELECT features.project_id FROM learning_attempts "
            "JOIN growth_tasks USING(task_id) "
            "JOIN opportunities USING(opportunity_id) "
            "JOIN analysis_runs USING(analysis_id) "
            "JOIN snapshots USING(snapshot_id) "
            "JOIN features USING(feature_id) "
            "WHERE learning_attempts.attempt_id=?", (attempt_id,),
        ).fetchone()
        if row is None:
            raise ValueError("回答不存在")
        return str(row["project_id"])

    def set_project_policy(
        self, project_id: str, *, local_processing: bool,
        model_allowed: bool, cloud_allowed: bool,
    ) -> dict[str, Any]:
        with self.connection:
            existing = self.connection.execute(
                "SELECT cloud_allowed FROM project_policies WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if existing is None:
                raise ValueError("项目不存在")
            if cloud_allowed and not existing["cloud_allowed"]:
                raise ValueError("开启云同步需要先预览并批准发布内容")
            self.connection.execute(
                "UPDATE project_policies SET local_processing=?, model_allowed=?, "
                "cloud_allowed=?, updated_at=? WHERE project_id=?",
                (int(local_processing), int(model_allowed), int(cloud_allowed),
                 _now(), project_id),
            )
        return self.project_policy(project_id)

    def project_policy(self, project_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM project_policies WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise ValueError("项目不存在")
        return {**dict(row), **{
            name: bool(row[name])
            for name in ("local_processing", "model_allowed", "cloud_allowed")
        }}

    def grant_cloud_sync(self, project_id: str, account_id: str, origin: str) -> None:
        row = self.project_policy(project_id)
        if row["cloud_account_id"] not in {None, account_id}:
            raise ValueError("本机项目已绑定到另一云端账号")
        if row["cloud_origin"] not in {None, origin}:
            raise ValueError("本机项目已绑定到另一云服务地址")
        self.connection.execute(
            "UPDATE project_policies SET cloud_allowed=1, cloud_account_id=?, "
            "cloud_origin=?, last_publish_error=NULL, updated_at=? WHERE project_id=?",
            (account_id, origin, _now(), project_id),
        )

    def queue_enabled_publications(self, project_id: str) -> None:
        from .publish import publication_plan, queue_publication
        from .sync import GrowthSyncStore

        rows = self.connection.execute(
            "SELECT policies.project_id, policies.cloud_account_id, "
            "policies.cloud_origin, accounts.device_id FROM project_policies policies "
            "JOIN sync_accounts accounts ON accounts.account_id=policies.cloud_account_id "
            "WHERE policies.cloud_allowed=1 AND policies.project_id=?",
            (project_id,),
        ).fetchall()
        for row in rows:
            project_id = str(row["project_id"])
            self.connection.execute("SAVEPOINT growth_auto_publish")
            try:
                store = GrowthSyncStore(
                    self, uuid.UUID(row["cloud_account_id"]),
                    uuid.UUID(row["device_id"]), cloud_origin=row["cloud_origin"],
                )
                plan = publication_plan(self, project_id)
                queue_publication(store, plan, plan["digest"])
            except ValueError as exc:
                self.connection.execute("ROLLBACK TO SAVEPOINT growth_auto_publish")
                self.connection.execute("RELEASE SAVEPOINT growth_auto_publish")
                self.connection.execute(
                    "UPDATE project_policies SET last_publish_error=?, updated_at=? "
                    "WHERE project_id=?", (str(exc), _now(), project_id),
                )
            else:
                self.connection.execute("RELEASE SAVEPOINT growth_auto_publish")
                self.connection.execute(
                    "UPDATE project_policies SET last_publish_error=NULL WHERE project_id=?",
                    (project_id,),
                )

    def create_project(self, name: str, *, trace_id: str) -> str:
        project_id = _id()
        with self.connection:
            self.connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?)", (project_id, name, _now())
            )
            self.connection.execute(
                "INSERT INTO project_policies(project_id, updated_at) VALUES (?, ?)",
                (project_id, _now()),
            )
            self.record_event(trace_id, "project", project_id, "project_created", {"name": name})
        return project_id

    def bind_repo(self, project_id: str, path: Path, *, trace_id: str) -> str:
        root = repository_root(path)
        found = self.connection.execute(
            "SELECT binding_id FROM bindings WHERE project_id=? AND canonical_local_path=?",
            (project_id, str(root)),
        ).fetchone()
        if found:
            return str(found[0])
        binding_id = _id()
        with self.connection:
            self.connection.execute(
                "INSERT INTO bindings VALUES (?, ?, ?, ?, ?)",
                (binding_id, project_id, str(root), worktree_id(root), _now()),
            )
            # Absolute paths remain exclusively in this local database.
            self.record_event(
                trace_id, "binding", binding_id, "repo_bound", {"project_id": project_id}
            )
        return binding_id

    def start_feature(
        self, project_id: str, binding_id: str, label: str, base_ref: str, *, trace_id: str
    ) -> str:
        self.require_project_permission(project_id, "local_processing")
        binding = self.connection.execute(
            "SELECT canonical_local_path FROM bindings WHERE binding_id=? AND project_id=?",
            (binding_id, project_id),
        ).fetchone()
        if binding is None:
            raise ValueError("项目与本机仓库绑定不匹配")
        start_base_sha = git_text(
            Path(binding[0]), "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"
        )
        feature_id = _id()
        with self.connection:
            self.connection.execute(
                "INSERT INTO features VALUES (?, ?, ?, ?, ?, ?, ?)",
                (feature_id, project_id, label, base_ref, start_base_sha, "active", _now()),
            )
            self.record_event(
                trace_id,
                "feature",
                feature_id,
                "feature_started",
                {"project_id": project_id, "base_ref": base_ref, "start_base_sha": start_base_sha},
            )
        return feature_id

    def capture_snapshot(
        self,
        feature_id: str,
        binding_id: str,
        *,
        trace_id: str,
        selected_untracked: set[str] | None = None,
        commit_sha: str | None = None,
    ) -> str:
        row = self.connection.execute(
            "SELECT features.base_ref, features.project_id, bindings.canonical_local_path "
            "FROM features JOIN bindings ON bindings.project_id=features.project_id "
            "WHERE features.feature_id=? AND bindings.binding_id=?",
            (feature_id, binding_id),
        ).fetchone()
        if row is None:
            raise ValueError("feature 与本机仓库绑定不匹配")
        self.require_project_permission(str(row["project_id"]), "local_processing")
        if commit_sha:
            if selected_untracked:
                raise ValueError("提交快照不能包含未跟踪文件")
            root = Path(row[2])
            selected = git_text(root, "rev-parse", "--verify", "--end-of-options", f"{commit_sha}^{{commit}}")
            indexed = self.connection.execute(
                "SELECT 1 FROM git_history_commits WHERE binding_id=? "
                "AND commit_sha=? AND reachable=1", (binding_id, selected),
            ).fetchone()
            if indexed is None:
                raise ValueError("所选提交不在本机已扫描的 Git 引用中，请先扫描历史")
            capture: CapturedSnapshot = capture_commit(root, selected)
            scope = _json({"kind": "commit", "sha": selected})
        else:
            capture = capture_worktree(Path(row[2]), str(row[0]), selected_untracked)
            scope = _json({"kind": "worktree", "selected_untracked": capture.included_untracked})
        existing = self.connection.execute(
            "SELECT snapshot_id FROM snapshots WHERE feature_id=? AND resolved_base_sha=? "
            "AND head_sha=? AND effective_tree_hash=? AND diff_hash=? AND capture_scope=?",
            (
                feature_id,
                capture.resolved_base_sha,
                capture.head_sha,
                capture.effective_tree_hash,
                capture.diff_hash,
                scope,
            ),
        ).fetchone()
        if existing:
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO snapshot_bindings VALUES (?, ?, ?)",
                    (str(existing[0]), binding_id, _now()),
                )
            return str(existing[0])
        snapshot_id = _id()
        with self.connection:
            self.connection.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    feature_id,
                    capture.resolved_base_sha,
                    capture.head_sha,
                    capture.effective_tree_hash,
                    capture.diff_hash,
                    scope,
                    "local-v1",
                    capture.patch_text,
                    _now(),
                ),
            )
            self.connection.execute(
                "INSERT INTO snapshot_bindings VALUES (?, ?, ?)",
                (snapshot_id, binding_id, _now()),
            )
            for source in capture.changed_sources:
                self.connection.execute(
                    "INSERT INTO source_refs VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _id(),
                        snapshot_id,
                        source.relative_path,
                        source.blob_hash,
                        source.content,
                        int(source.deleted),
                    ),
                )
            self.record_event(
                trace_id,
                "snapshot",
                snapshot_id,
                "snapshot_captured",
                {
                    "feature_id": feature_id,
                    "base_sha": capture.resolved_base_sha,
                    "head_sha": capture.head_sha,
                    "effective_tree_hash": capture.effective_tree_hash,
                    "diff_hash": capture.diff_hash,
                    "source_count": len(capture.changed_sources),
                    "included_untracked": capture.included_untracked,
                    "filtered_paths": capture.filtered_paths,
                },
            )
        return snapshot_id

    def analyze_snapshot(
        self, snapshot_id: str, binding_id: str, request_id: str, *,
        trace_id: str, reanalysis: bool = False,
    ) -> str:
        analysis_id, created = self.queue_analysis(
            snapshot_id, binding_id, request_id,
            trace_id=trace_id, reanalysis=reanalysis,
        )
        if created:
            self.execute_queued_analysis(analysis_id, binding_id, trace_id=trace_id)
        return analysis_id

    def queue_analysis(
        self, snapshot_id: str, binding_id: str, request_id: str, *,
        trace_id: str, reanalysis: bool = False,
    ) -> tuple[str, bool]:
        mode = "dry_run"
        self.require_project_permission(
            self.project_id_for_snapshot(snapshot_id), "local_processing"
        )
        row = self.connection.execute(
            "SELECT snapshots.patch_text, bindings.canonical_local_path FROM snapshots "
            "JOIN snapshot_bindings USING (snapshot_id) "
            "JOIN bindings USING (binding_id) "
            "WHERE snapshots.snapshot_id=? AND bindings.binding_id=?",
            (snapshot_id, binding_id),
        ).fetchone()
        if row is None:
            raise ValueError("快照与本机仓库绑定不匹配")
        patch_text = str(row[0])
        fingerprint = hashlib.sha256(
            _json({"snapshot_id": snapshot_id, "patch": patch_text, "mode": mode}).encode()
        ).hexdigest()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT analysis_id, mode FROM analysis_runs "
                "WHERE snapshot_id=? AND request_id=?",
                (snapshot_id, request_id),
            ).fetchone()
            if existing:
                if existing["mode"] != mode:
                    raise ValueError("同一分析请求不能切换 dry_run/live 模式")
                return str(existing[0]), False
            previous = self.connection.execute(
                "SELECT analysis_id, input_fingerprint FROM analysis_runs "
                "WHERE snapshot_id=? AND mode=? ORDER BY created_at DESC LIMIT 1",
                (snapshot_id, mode),
            ).fetchone()
            if previous and not reanalysis and previous["input_fingerprint"] == fingerprint:
                return str(previous["analysis_id"]), False
            analysis_id = _id()
            self.connection.execute(
                "INSERT INTO analysis_runs (analysis_id, snapshot_id, request_id, "
                "input_fingerprint, mode, status, upstream_run_id, error, created_at, "
                "supersedes_analysis_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis_id,
                    snapshot_id,
                    request_id,
                    fingerprint,
                    mode,
                    "queued",
                    None,
                    None,
                    _now(),
                    str(previous["analysis_id"]) if previous else None,
                ),
            )
            self.record_event(
                trace_id,
                "analysis",
                analysis_id,
                "analysis_requested",
                {"snapshot_id": snapshot_id},
            )
        return analysis_id, True

    def analyze_live_opportunities(
        self, snapshot_id: str, binding_id: str, request_id: str,
        *, provider_name: str, source_ref_ids: list[str],
        approved_payload_hash: str, trace_id: str,
    ) -> str:
        analysis_id, created = self.queue_live_opportunities(
            snapshot_id, binding_id, request_id,
            provider_name=provider_name, source_ref_ids=source_ref_ids,
            approved_payload_hash=approved_payload_hash, trace_id=trace_id,
        )
        if created:
            self.execute_queued_analysis(analysis_id, binding_id, trace_id=trace_id)
        return analysis_id

    def queue_live_opportunities(
        self, snapshot_id: str, binding_id: str, request_id: str,
        *, provider_name: str, source_ref_ids: list[str],
        approved_payload_hash: str, trace_id: str, reanalysis: bool = False,
    ) -> tuple[str, bool]:
        from .model_opportunities import prepare_model_preview

        project_id = self.project_id_for_snapshot(snapshot_id)
        self.require_project_permission(project_id, "local_processing")
        self.require_project_permission(project_id, "model_allowed")
        preview = prepare_model_preview(
            self, snapshot_id, binding_id, provider_name, source_ref_ids,
        )
        if approved_payload_hash != preview.approval_hash:
            raise ValueError("可发送内容或模型配置已变化，请重新预览并确认")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT analysis_id, mode, input_fingerprint FROM analysis_runs "
                "WHERE snapshot_id=? AND request_id=?",
                (snapshot_id, request_id),
            ).fetchone()
            if existing:
                if (existing["mode"] != "live"
                        or existing["input_fingerprint"] != preview.approval_hash):
                    raise ValueError("同一分析请求不能改变模式或批准内容")
                return str(existing["analysis_id"]), False
            previous = self.connection.execute(
                "SELECT analysis_id, input_fingerprint FROM analysis_runs "
                "WHERE snapshot_id=? AND mode='live' ORDER BY created_at DESC LIMIT 1",
                (snapshot_id,),
            ).fetchone()
            if (previous and not reanalysis
                    and previous["input_fingerprint"] == preview.approval_hash):
                return str(previous["analysis_id"]), False
            analysis_id = _id()
            self.connection.execute(
                "INSERT INTO analysis_runs (analysis_id, snapshot_id, request_id, "
                "input_fingerprint, mode, status, created_at, supersedes_analysis_id) "
                "VALUES (?, ?, ?, ?, 'live', 'queued', ?, ?)",
                (analysis_id, snapshot_id, request_id, preview.approval_hash, _now(),
                 str(previous["analysis_id"]) if previous else None),
            )
            self.connection.execute(
                "INSERT INTO analysis_model_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis_id, binding_id, provider_name,
                    preview.provider_config_hash, _json(preview.source_ref_ids),
                    preview.approval_hash, preview.payload_text,
                    preview.selected_patch, _now(),
                ),
            )
            self.record_event(
                trace_id, "analysis", analysis_id, "analysis_requested",
                {"snapshot_id": snapshot_id, "mode": "live",
                 "source_ref_ids": preview.source_ref_ids},
            )
        return analysis_id, True

    def queue_retry_analysis(
        self, analysis_id: str, binding_id: str, *, trace_id: str,
    ) -> None:
        row = self.connection.execute(
            "SELECT analysis_runs.mode, analysis_model_requests.binding_id AS approved_binding "
            "FROM analysis_runs JOIN snapshot_bindings USING (snapshot_id) "
            "LEFT JOIN analysis_model_requests USING (analysis_id) "
            "WHERE analysis_runs.analysis_id=? AND snapshot_bindings.binding_id=?",
            (analysis_id, binding_id),
        ).fetchone()
        if row is None or (row["mode"] == "live" and row["approved_binding"] != binding_id):
            raise ValueError("分析与本机仓库绑定不匹配")
        with self.connection:
            claimed = self.connection.execute(
                "UPDATE analysis_runs SET status='queued', error=NULL "
                "WHERE analysis_id=? AND status='failed'", (analysis_id,),
            ).rowcount
            if not claimed:
                raise ValueError("只有失败分析可以显式重试")
            self.record_event(
                trace_id, "analysis", analysis_id, "analysis_retry_queued", {},
            )

    def reject_queued_analysis(self, analysis_id: str, reason: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE analysis_runs SET status='failed', error=? "
                "WHERE analysis_id=? AND status='queued'", (reason, analysis_id),
            )

    def execute_queued_analysis(
        self, analysis_id: str, binding_id: str, *, trace_id: str,
    ) -> str:
        row = self.connection.execute(
            "SELECT analysis_runs.mode, snapshots.patch_text, "
            "bindings.canonical_local_path, analysis_model_requests.binding_id AS approved_binding "
            "FROM analysis_runs JOIN snapshots USING (snapshot_id) "
            "JOIN snapshot_bindings USING (snapshot_id) "
            "JOIN bindings USING (binding_id) "
            "LEFT JOIN analysis_model_requests USING (analysis_id) "
            "WHERE analysis_runs.analysis_id=? AND bindings.binding_id=?",
            (analysis_id, binding_id),
        ).fetchone()
        if row is None or (row["mode"] == "live" and row["approved_binding"] != binding_id):
            raise ValueError("分析与本机仓库绑定不匹配")
        with self.connection:
            claimed = self.connection.execute(
                "UPDATE analysis_runs SET status='running' "
                "WHERE analysis_id=? AND status='queued'", (analysis_id,),
            ).rowcount
        if not claimed:
            return analysis_id
        if row["mode"] == "live":
            self._run_live_attempt(analysis_id, trace_id, binding_id=binding_id)
        else:
            self._run_analysis_attempt(
                analysis_id, str(row["patch_text"]),
                Path(row["canonical_local_path"]), trace_id, str(row["mode"]),
            )
        return analysis_id

    def retry_analysis(self, analysis_id: str, binding_id: str, *, trace_id: str) -> str:
        """Retry a failed run without changing its request or analysis identity."""
        self.queue_retry_analysis(analysis_id, binding_id, trace_id=trace_id)
        return self.execute_queued_analysis(analysis_id, binding_id, trace_id=trace_id)

    def _run_live_attempt(
        self, analysis_id: str, trace_id: str, *, binding_id: str | None = None,
    ) -> None:
        from .learning import GrowthLearningService
        from .model_opportunities import generate_opportunities, prepare_model_preview

        row = self.connection.execute(
            "SELECT analysis_runs.snapshot_id, analysis_model_requests.binding_id, "
            "analysis_model_requests.provider_name, "
            "analysis_model_requests.source_ref_ids_json, "
            "analysis_model_requests.approved_payload_hash, "
            "analysis_model_requests.approved_payload_text, "
            "analysis_model_requests.provider_config_hash, "
            "analysis_model_requests.selected_patch, bindings.canonical_local_path "
            "FROM analysis_runs JOIN analysis_model_requests USING (analysis_id) "
            "JOIN bindings ON bindings.binding_id=analysis_model_requests.binding_id "
            "WHERE analysis_runs.analysis_id=?",
            (analysis_id,),
        ).fetchone()
        if row is None or (binding_id is not None and binding_id != row["binding_id"]):
            raise ValueError("live 分析与本机仓库绑定不匹配")
        attempt_no = self.connection.execute(
            "SELECT COUNT(*) FROM analysis_attempts WHERE analysis_id=?", (analysis_id,)
        ).fetchone()[0] + 1
        attempt_id = _id()
        with self.connection:
            self.connection.execute(
                "INSERT INTO analysis_attempts VALUES (?, ?, ?, 'running', NULL, NULL, ?)",
                (attempt_id, analysis_id, attempt_no, _now()),
            )
            self.connection.execute(
                "UPDATE analysis_runs SET status='running', error=NULL WHERE analysis_id=?",
                (analysis_id,),
            )
            if attempt_no > 1:
                self.record_event(
                    trace_id, "analysis", analysis_id,
                    "analysis_retry_requested", {"attempt_no": attempt_no},
                )
        upstream_run_id: str | None = None
        try:
            self.require_project_permission(
                self.project_id_for_snapshot(str(row["snapshot_id"])), "model_allowed"
            )
            repo_path = Path(row["canonical_local_path"])
            self._assert_snapshot_current(analysis_id, repo_path)
            preview = prepare_model_preview(
                self, str(row["snapshot_id"]), str(row["binding_id"]),
                str(row["provider_name"]), json.loads(row["source_ref_ids_json"]),
            )
            if (
                preview.approval_hash != row["approved_payload_hash"]
                or preview.payload_text != row["approved_payload_text"]
                or preview.provider_config_hash != row["provider_config_hash"]
                or preview.selected_patch != row["selected_patch"]
            ):
                raise ValueError("批准内容已变化，请新建分析并重新确认")
            result = run_learn_pipeline(LearnRequest(
                workspace_root=repo_path,
                patch_text=preview.selected_patch,
                dry_run=True,
            ))
            if result.status != "dry_run":
                raise ValueError(f"AhaDiff 本机分析状态异常：{result.status}")
            upstream_run_id = result.run_id
            self._assert_snapshot_current(analysis_id, repo_path)
            started = time.monotonic()
            batch, response = generate_opportunities(repo_path, preview)
            elapsed_ms = round((time.monotonic() - started) * 1000)
            allowed_refs = {
                str(ref) for ref in preview.source_ref_ids
            }
            for opportunity in batch.opportunities:
                if not {str(ref) for ref in opportunity.source_refs} <= allowed_refs:
                    raise ValueError("模型机会引用了未批准的源码")
            with self.connection:
                self.connection.execute(
                    "UPDATE analysis_runs SET status='succeeded', upstream_run_id=?, "
                    "error=NULL WHERE analysis_id=?",
                    (upstream_run_id, analysis_id),
                )
                GrowthLearningService(self).save_opportunities(
                    analysis_id, batch, origin="live", trace_id=trace_id,
                    provider_name=preview.provider_name,
                    model_name=response.model_id,
                    provider_request_id=response.request_id,
                )
                self.connection.execute(
                    "INSERT INTO analysis_model_responses VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        analysis_id, response.content, response.request_id,
                        response.input_tokens, response.output_tokens,
                        elapsed_ms, _now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE analysis_attempts SET status='succeeded', upstream_run_id=? "
                    "WHERE attempt_id=?",
                    (upstream_run_id, attempt_id),
                )
                self.record_event(
                    trace_id, "analysis", analysis_id, "analysis_succeeded",
                    {
                        "attempt_no": attempt_no, "upstream_run_id": upstream_run_id,
                        "opportunity_count": len(batch.opportunities),
                        "provider_request_id": response.request_id,
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "elapsed_ms": elapsed_ms,
                    },
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with self.connection:
                self.connection.execute(
                    "UPDATE analysis_runs SET status='failed', upstream_run_id=?, error=? "
                    "WHERE analysis_id=?",
                    (upstream_run_id, error, analysis_id),
                )
                self.connection.execute(
                    "UPDATE analysis_attempts SET status='failed', upstream_run_id=?, error=? "
                    "WHERE attempt_id=?",
                    (upstream_run_id, error, attempt_id),
                )
                self.record_event(
                    trace_id, "analysis", analysis_id, "analysis_failed",
                    {"attempt_no": attempt_no, "upstream_run_id": upstream_run_id,
                     "error": error},
                )
            raise RuntimeError(error) from exc

    def _run_analysis_attempt(
        self, analysis_id: str, patch_text: str, repo_path: Path,
        trace_id: str, mode: str,
    ) -> None:
        attempt_no = (
            self.connection.execute(
                "SELECT COUNT(*) FROM analysis_attempts WHERE analysis_id=?", (analysis_id,)
            ).fetchone()[0]
            + 1
        )
        attempt_id = _id()
        with self.connection:
            self.connection.execute(
                "INSERT INTO analysis_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, analysis_id, attempt_no, "running", None, None, _now()),
            )
            self.connection.execute(
                "UPDATE analysis_runs SET status='running', error=NULL WHERE analysis_id=?",
                (analysis_id,),
            )
            if attempt_no > 1:
                self.record_event(
                    trace_id,
                    "analysis",
                    analysis_id,
                    "analysis_retry_requested",
                    {"attempt_no": attempt_no},
                )
        failure_exc: Exception | None = None
        try:
            if not patch_text:
                raise ValueError("此快照没有可分析的文本变化")
            if mode != "dry_run":
                raise ValueError("live 模型发送边界尚未批准")
            self._assert_snapshot_current(analysis_id, repo_path)
            result = run_learn_pipeline(
                LearnRequest(workspace_root=repo_path, patch_text=patch_text, dry_run=True)
            )
            self._assert_snapshot_current(analysis_id, repo_path)
            status = "succeeded" if result.status == "dry_run" else "failed"
            error = None if status == "succeeded" else f"AhaDiff status: {result.status}"
            upstream_run_id = result.run_id
        except Exception as exc:
            status, error, upstream_run_id = "failed", f"{type(exc).__name__}: {exc}", None
            failure_exc = exc
            if exc.__cause__ is not None:
                cause = exc.__cause__
                error += f"; caused by {type(cause).__name__}: {cause}"
        with self.connection:
            self.connection.execute(
                "UPDATE analysis_runs SET status=?, upstream_run_id=?, error=? WHERE analysis_id=?",
                (status, upstream_run_id, error, analysis_id),
            )
            self.connection.execute(
                "UPDATE analysis_attempts SET status=?, upstream_run_id=?, error=? "
                "WHERE attempt_id=?",
                (status, upstream_run_id, error, attempt_id),
            )
            self.record_event(
                trace_id,
                "analysis",
                analysis_id,
                f"analysis_{status}",
                {
                    "attempt_id": attempt_id,
                    "attempt_no": attempt_no,
                    "upstream_run_id": upstream_run_id,
                    "mode": mode,
                    "error": error,
                },
            )
        if status == "failed":
            raise RuntimeError(error) from failure_exc

    def _assert_snapshot_current(self, analysis_id: str, repo_path: Path) -> None:
        row = self.connection.execute(
            "SELECT snapshots.resolved_base_sha, snapshots.head_sha, "
            "snapshots.effective_tree_hash, snapshots.diff_hash, "
            "snapshots.capture_scope, features.base_ref "
            "FROM analysis_runs JOIN snapshots USING (snapshot_id) "
            "JOIN features USING (feature_id) WHERE analysis_runs.analysis_id=?",
            (analysis_id,),
        ).fetchone()
        assert row is not None
        scope = json.loads(row["capture_scope"])
        current = (capture_commit(repo_path, scope["sha"])
                   if scope["kind"] == "commit" else capture_worktree(
                       repo_path, str(row["base_ref"]),
                       set(scope["selected_untracked"]),
                   ))
        if (
            current.resolved_base_sha != row["resolved_base_sha"]
            or current.head_sha != row["head_sha"]
            or current.effective_tree_hash != row["effective_tree_hash"]
            or current.diff_hash != row["diff_hash"]
        ):
            raise ValueError("本机源码版本已变化，请重新捕获快照后分析")

    def export_chain(self, feature_id: str) -> dict[str, Any]:
        feature = self.connection.execute(
            "SELECT * FROM features WHERE feature_id=?", (feature_id,)
        ).fetchone()
        if feature is None:
            raise ValueError("feature 不存在")
        project = self.connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (feature["project_id"],)
        ).fetchone()
        snapshots = self.connection.execute(
            "SELECT * FROM snapshots WHERE feature_id=? ORDER BY created_at", (feature_id,)
        ).fetchall()
        snapshot_ids = [row["snapshot_id"] for row in snapshots]
        refs = self.connection.execute(
            "SELECT source_ref_id, snapshot_id, relative_path, blob_hash, deleted "
            "FROM source_refs WHERE snapshot_id IN "
            "(SELECT snapshot_id FROM snapshots WHERE feature_id=?)",
            (feature_id,),
        ).fetchall()
        analysis = self.connection.execute(
            "SELECT * FROM analysis_runs WHERE snapshot_id IN "
            "(SELECT snapshot_id FROM snapshots WHERE feature_id=?)",
            (feature_id,),
        ).fetchall()
        attempts = self.connection.execute(
            "SELECT * FROM analysis_attempts WHERE analysis_id IN "
            "(SELECT analysis_id FROM analysis_runs WHERE snapshot_id IN "
            "(SELECT snapshot_id FROM snapshots WHERE feature_id=?)) ORDER BY created_at",
            (feature_id,),
        ).fetchall()
        events = self.connection.execute(
            "SELECT * FROM growth_events ORDER BY created_at, rowid"
        ).fetchall()
        return {
            "project": dict(project),
            "feature": dict(feature),
            "snapshots": [dict(row) | {"patch_text": "<local_only>"} for row in snapshots],
            "source_refs": [dict(row) for row in refs],
            "analysis_runs": [dict(row) for row in analysis],
            "analysis_attempts": [dict(row) for row in attempts],
            "events": [dict(row) for row in events],
            "snapshot_ids": snapshot_ids,
        }
