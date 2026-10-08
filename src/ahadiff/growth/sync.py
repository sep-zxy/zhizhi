"""Durable local sync state, user drafts, and ordered cloud delivery."""

from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import httpx

    from .local import GrowthLocalRepository


SYNC_SCHEMA = """
CREATE TABLE sync_accounts (
  account_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
  cursor INTEGER NOT NULL DEFAULT 0, bootstrapped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sync_entities (
  account_id TEXT NOT NULL REFERENCES sync_accounts(account_id),
  entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
  deleted_at TEXT, PRIMARY KEY(account_id, entity_type, entity_id)
);
CREATE TABLE sync_outbox (
  account_id TEXT NOT NULL REFERENCES sync_accounts(account_id),
  operation_id TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
  payload_json TEXT NOT NULL, dependency_type TEXT, dependency_id TEXT,
  target_type TEXT, target_id TEXT,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending', 'acked', 'conflict', 'deleted')),
  response_json TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY(account_id, operation_id)
);
CREATE INDEX sync_outbox_pending ON sync_outbox(account_id, state, created_at);
CREATE TABLE sync_note_drafts (
  account_id TEXT NOT NULL REFERENCES sync_accounts(account_id),
  note_id TEXT NOT NULL, base_revision INTEGER NOT NULL,
  content_text TEXT NOT NULL, operation_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending', 'synced', 'conflict', 'recovery')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(account_id, operation_id)
);
"""

SYNC_V5_SCHEMA = """
CREATE TABLE sync_task_drafts (
  account_id TEXT NOT NULL REFERENCES sync_accounts(account_id),
  task_id TEXT NOT NULL,
  local_revision INTEGER NOT NULL CHECK(local_revision >= 0),
  content_text TEXT NOT NULL,
  submitted_attempt_id TEXT,
  latest_operation_id TEXT,
  state TEXT NOT NULL CHECK(state IN ('pending', 'synced', 'conflict', 'recovery')),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_id, task_id)
);
CREATE TABLE sync_pending_attempts (
  account_id TEXT NOT NULL REFERENCES sync_accounts(account_id),
  attempt_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  parent_attempt_id TEXT,
  answer_text TEXT NOT NULL,
  hint_level INTEGER NOT NULL CHECK(hint_level BETWEEN 0 AND 3),
  operation_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending', 'synced', 'conflict', 'recovery')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(account_id, attempt_id),
  UNIQUE(account_id, operation_id)
);
"""

SYNC_V11_SCHEMA = "ALTER TABLE sync_accounts ADD COLUMN cloud_origin TEXT;"

BOOTSTRAP_TYPES = {
    "projects": ("project", "project_id"),
    "features": ("feature", "feature_id"),
    "snapshots": ("snapshot", "snapshot_id"),
    "source_refs": ("source_ref", "source_ref_id"),
    "source_excerpts": ("source_excerpt", "source_ref_id"),
    "analysis_runs": ("analysis", "analysis_id"),
    "modules": ("module", "module_id"),
    "chat_sessions": ("chat_session", "session_id"),
    "chat_messages": ("chat_message", "message_id"),
    "chat_suggestions": ("chat_suggestion", "suggestion_id"),
    "development_events": ("development_event", "event_id"),
    "opportunities": ("opportunity", "opportunity_id"),
    "topic_proposals": ("topic_proposal", "proposal_id"),
    "topics": ("topic", "topic_id"),
    "topic_aliases": ("topic_alias", "alias_topic_id"),
    "topic_moves": ("topic_move", "move_id"),
    "topic_links": ("topic_link", "topic_id"),
    "tasks": ("task", "task_id"),
    "task_drafts": ("task_draft", "task_id"),
    "attempts": ("attempt", "attempt_id"),
    "notes": ("note", "note_id"),
    "note_revisions": ("note_revision", "note_id"),
    "note_conflicts": ("note_conflict", "conflict_id"),
    "review_schedules": ("review_schedule", "task_id"),
    "review_events": ("review_event", "review_id"),
}


def topic_link_sync_id(
    account_id: str, topic_id: str, entity_type: str, entity_id: str
) -> str:
    """Stable UUID for a topic link with a composite database key."""
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"growth-topic-link-v1:{account_id}:{topic_id}:{entity_type}:{entity_id}",
    ))


def note_revision_sync_id(note_id: str, revision: int) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"growth-note-revision-v1:{note_id}:{revision}",
    ))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SyncOriginMismatchError(ValueError):
    """A local account cache cannot silently switch to a different cloud origin."""


class GrowthSyncStore:
    def __init__(
        self, ledger: GrowthLocalRepository, account_id: uuid.UUID, device_id: uuid.UUID,
        *, cloud_origin: str | None = None,
    ):
        self.db = ledger.connection
        self.account_id = str(account_id)
        self.device_id = str(device_id)
        with nullcontext() if self.db.in_transaction else self.db:
            self.db.execute(
                "INSERT INTO sync_accounts(account_id, device_id) VALUES (?, ?) "
                "ON CONFLICT(account_id) DO NOTHING", (self.account_id, self.device_id),
            )
        existing = self.db.execute(
            "SELECT device_id, cloud_origin FROM sync_accounts WHERE account_id=?",
            (self.account_id,),
        ).fetchone()
        if existing["device_id"] != self.device_id:
            raise ValueError("本地同步库已绑定到另一设备")
        if cloud_origin is not None:
            if existing["cloud_origin"] not in {None, cloud_origin}:
                raise SyncOriginMismatchError("本地同步账号已绑定到另一云服务地址")
            with nullcontext() if self.db.in_transaction else self.db:
                self.db.execute(
                    "UPDATE sync_accounts SET cloud_origin=? "
                    "WHERE account_id=? AND cloud_origin IS NULL",
                    (cloud_origin, self.account_id),
                )

    def cursor(self) -> int:
        row = self.db.execute(
            "SELECT cursor FROM sync_accounts WHERE account_id=?", (self.account_id,)
        ).fetchone()
        return int(row["cursor"])

    def queue_note_revision(self, note_id: uuid.UUID, base_revision: int, content: str) -> str:
        if base_revision < 1 or not content or len(content) > 100000:
            raise ValueError("笔记修订参数无效")
        self._require_cached_note(note_id, base_revision)
        self._require_no_pending_note_operation(note_id)
        operation_id = str(uuid.uuid4())
        payload = {"operation_id": operation_id, "base_revision": base_revision,
                   "content_text": content}
        with self.db:
            self.db.execute(
                "INSERT INTO sync_note_drafts "
                "(account_id, note_id, base_revision, content_text, "
                "operation_id, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (self.account_id, str(note_id), base_revision, content,
                 operation_id, datetime.now(UTC).isoformat()),
            )
            self.queue_operation(
                operation_id, "PUT", f"/v1/notes/{note_id}/revisions", payload,
                "note", str(note_id), "note", str(note_id),
            )
            self._link_note_operation(note_id, operation_id)
        return operation_id

    def _require_cached_note(self, note_id: uuid.UUID, base_revision: int) -> None:
        row = self.db.execute(
            "SELECT revision, deleted_at FROM sync_entities WHERE account_id=? "
            "AND entity_type='note' AND entity_id=?",
            (self.account_id, str(note_id)),
        ).fetchone()
        if row is None or row["deleted_at"] is not None:
            raise ValueError("只能编辑已同步且未删除的笔记")
        if int(row["revision"]) != base_revision:
            raise ValueError("笔记基础版本已变化，请先查看当前版本")
        self._require_project_cloud_allowed(self._note_project_id(str(note_id)))

    def _cached_payload(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT payload_json FROM sync_entities WHERE account_id=? "
            "AND entity_type=? AND entity_id=?",
            (self.account_id, entity_type, entity_id),
        ).fetchone()
        return cast("dict[str, Any]", json.loads(row["payload_json"])) if row else {}

    def cached_entity(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        """Read one synchronized entity without exposing the SQLite representation."""
        return self._cached_payload(entity_type, entity_id)

    def _note_project_id(self, note_id: str) -> str | None:
        note = self._cached_payload("note", note_id)
        project_id = self._task_project_id(str(note.get("task_id")))
        if project_id is not None:
            return project_id
        for ref_id in note.get("source_refs") or []:
            source = self._cached_payload("source_ref", str(ref_id))
            snapshot = self._cached_payload("snapshot", str(source.get("snapshot_id")))
            if snapshot.get("project_id"):
                return cast("str", snapshot["project_id"])
        return None

    def _task_project_id(self, task_id: str) -> str | None:
        task = self._cached_payload("task", task_id)
        if task.get("module_id"):
            module = self._cached_payload("module", str(task["module_id"]))
            if module.get("project_id"):
                return cast("str", module["project_id"])
        opportunity = self._cached_payload(
            "opportunity", str(task.get("opportunity_id")),
        )
        analysis = self._cached_payload("analysis", str(opportunity.get("analysis_id")))
        snapshot = self._cached_payload("snapshot", str(analysis.get("snapshot_id")))
        return cast("str | None", snapshot.get("project_id"))

    def _require_project_cloud_allowed(self, project_id: str | None) -> None:
        if project_id is None:
            return
        policy = self.db.execute(
            "SELECT cloud_allowed, cloud_account_id FROM project_policies "
            "WHERE project_id=?", (project_id,),
        ).fetchone()
        if policy is not None and not policy["cloud_allowed"]:
            raise ValueError("项目已暂停云同步")
        if policy is not None and policy["cloud_account_id"] not in {
            None, self.account_id,
        }:
            raise ValueError("项目已绑定到另一云端账号")

    def _link_note_operation(self, note_id: uuid.UUID, operation_id: str) -> None:
        project_id = self._note_project_id(str(note_id))
        self._link_project_operation(project_id, operation_id)

    def _link_task_operation(self, task_id: uuid.UUID, operation_id: str) -> None:
        self._link_project_operation(self._task_project_id(str(task_id)), operation_id)

    def _link_project_operation(
        self, project_id: str | None, operation_id: str,
    ) -> None:
        if project_id is not None:
            self.db.execute(
                "INSERT INTO sync_publication_links "
                "(account_id, operation_id, project_id) "
                "SELECT ?, ?, project_id FROM project_policies WHERE project_id=?",
                (self.account_id, operation_id, project_id),
            )

    def note_workspace(self) -> list[dict[str, Any]]:
        """Return durable cloud notes with all original texts for offline review."""
        notes: list[dict[str, Any]] = []
        for row in self.db.execute(
            "SELECT entity_id, revision, deleted_at, payload_json FROM sync_entities "
            "WHERE account_id=? AND entity_type='note' ORDER BY entity_id",
            (self.account_id,),
        ):
            note_id = str(row["entity_id"])
            revisions = []
            for version in self.db.execute(
                "SELECT payload_json FROM sync_entities WHERE account_id=? "
                "AND entity_type='note_revision'",
                (self.account_id,),
            ):
                payload = json.loads(version["payload_json"])
                if payload.get("note_id") == note_id:
                    revisions.append({
                        "revision": int(payload["revision"]),
                        "content_text": payload["content_text"],
                        "source_conflict_ids": payload.get("source_conflict_ids", []),
                    })
            revisions.sort(key=lambda item: item["revision"])
            conflicts = []
            for conflict in self.db.execute(
                "SELECT entity_id, payload_json FROM sync_entities WHERE account_id=? "
                "AND entity_type='note_conflict'",
                (self.account_id,),
            ):
                payload = json.loads(conflict["payload_json"])
                if payload.get("note_id") == note_id:
                    conflicts.append({"conflict_id": conflict["entity_id"], **payload})
            drafts = []
            for draft in self.db.execute(
                "SELECT drafts.base_revision, drafts.content_text, drafts.operation_id, "
                "drafts.state, drafts.created_at, outbox.response_json "
                "FROM sync_note_drafts drafts LEFT JOIN sync_outbox outbox "
                "ON outbox.account_id=drafts.account_id "
                "AND outbox.operation_id=drafts.operation_id "
                "WHERE drafts.account_id=? AND drafts.note_id=? ORDER BY drafts.created_at",
                (self.account_id, note_id),
            ):
                drafts.append({**dict(draft), "response": json.loads(draft["response_json"])
                               if draft["response_json"] else None})
                drafts[-1].pop("response_json")
            notes.append({
                "account_id": self.account_id, "note_id": note_id,
                "project_id": self._note_project_id(note_id),
                "revision": int(row["revision"]), "deleted_at": row["deleted_at"],
                "note": json.loads(row["payload_json"]),
                "revisions": revisions, "conflicts": conflicts, "drafts": drafts,
                "pending_operations": [dict(operation) for operation in self.db.execute(
                    "SELECT operation_id, method FROM sync_outbox WHERE account_id=? "
                    "AND target_type='note' AND target_id=? AND state='pending'",
                    (self.account_id, note_id),
                )],
            })
        return notes

    def task_workspace(self) -> list[dict[str, Any]]:
        """Expose cached cards, submitted answers and durable offline drafts."""
        tasks: list[dict[str, Any]] = []
        for row in self.db.execute(
            "SELECT entity_id, revision, deleted_at, payload_json FROM sync_entities "
            "WHERE account_id=? AND entity_type='task' ORDER BY entity_id",
            (self.account_id,),
        ):
            task_id = str(row["entity_id"])
            task_payload = json.loads(row["payload_json"])
            approved_sources: list[dict[str, Any]] = []
            opportunity = self._cached_payload(
                "opportunity", str(task_payload.get("opportunity_id")),
            )
            for ref_id in task_payload.get("source_refs") or opportunity.get("source_refs") or []:
                source = self._cached_payload("source_ref", str(ref_id))
                excerpt = self._cached_payload("source_excerpt", str(ref_id))
                if not source:
                    continue
                valid_excerpt = bool(
                    excerpt and excerpt.get("source_ref_id") == str(ref_id)
                    and excerpt.get("snapshot_id") == source.get("snapshot_id")
                    and excerpt.get("blob_hash") == source.get("blob_hash")
                )
                approved_sources.append({
                    "source_ref_id": str(ref_id),
                    "relative_path": source.get("relative_path"),
                    "blob_hash": source.get("blob_hash"),
                    "snapshot_id": source.get("snapshot_id"),
                    "approved_excerpt": excerpt if valid_excerpt else None,
                })
            local = self.db.execute(
                "SELECT local_revision, content_text, submitted_attempt_id, "
                "latest_operation_id, state, updated_at FROM sync_task_drafts "
                "WHERE account_id=? AND task_id=?",
                (self.account_id, task_id),
            ).fetchone()
            pending_attempts = [dict(item) for item in self.db.execute(
                "SELECT attempt_id, parent_attempt_id, answer_text, hint_level, "
                "operation_id, state, created_at FROM sync_pending_attempts "
                "WHERE account_id=? AND task_id=? ORDER BY created_at",
                (self.account_id, task_id),
            )]
            attempts = []
            for attempt in self.db.execute(
                "SELECT payload_json FROM sync_entities WHERE account_id=? "
                "AND entity_type='attempt'",
                (self.account_id,),
            ):
                payload = json.loads(attempt["payload_json"])
                if payload.get("task_id") == task_id:
                    attempts.append({**payload, "attempt_id": payload.get("attempt_id")
                                     or payload["entity_id"]})
            tasks.append({
                "account_id": self.account_id, "task_id": task_id,
                "project_id": self._task_project_id(task_id),
                "revision": int(row["revision"]), "deleted_at": row["deleted_at"],
                "task": task_payload, "approved_sources": approved_sources,
                "remote_draft": self._cached_payload("task_draft", task_id) or None,
                "local_draft": dict(local) if local else None,
                "pending_attempts": pending_attempts, "attempts": attempts,
                "pending_operations": [dict(item) for item in self.db.execute(
                    "SELECT operation_id, method, path FROM sync_outbox WHERE account_id=? "
                    "AND dependency_type='task' AND dependency_id=? AND state='pending'",
                    (self.account_id, task_id),
                )],
                "blocked_operations": [{
                    "operation_id": item["operation_id"],
                    "path": item["path"],
                    "payload": json.loads(item["payload_json"]),
                    "response": json.loads(item["response_json"])
                    if item["response_json"] else None,
                } for item in self.db.execute(
                    "SELECT operation_id, path, payload_json, response_json "
                    "FROM sync_outbox WHERE account_id=? AND dependency_type='task' "
                    "AND dependency_id=? AND state='conflict' ORDER BY rowid",
                    (self.account_id, task_id),
                )],
            })
        return tasks

    def _require_no_pending_note_operation(self, note_id: uuid.UUID) -> None:
        pending = self.db.execute(
            "SELECT 1 FROM sync_outbox WHERE account_id=? AND target_type='note' "
            "AND target_id=? AND state='pending' LIMIT 1",
            (self.account_id, str(note_id)),
        ).fetchone()
        if pending is not None:
            raise ValueError("该笔记已有待同步操作")

    def queue_note_resolution(
        self, note_id: uuid.UUID, base_revision: int,
        conflict_ids: list[uuid.UUID], content: str,
    ) -> str:
        if not conflict_ids or not content or len(content) > 100000:
            raise ValueError("笔记合并参数无效")
        if len(set(conflict_ids)) != len(conflict_ids):
            raise ValueError("冲突 ID 重复")
        self._require_cached_note(note_id, base_revision)
        self._require_no_pending_note_operation(note_id)
        for conflict_id in conflict_ids:
            row = self.db.execute(
                "SELECT payload_json FROM sync_entities WHERE account_id=? "
                "AND entity_type='note_conflict' AND entity_id=?",
                (self.account_id, str(conflict_id)),
            ).fetchone()
            payload = json.loads(row["payload_json"]) if row else {}
            if payload.get("note_id") != str(note_id) or payload.get("resolved_revision"):
                raise ValueError("只能合并此笔记尚未解决的冲突")
        operation_id = str(uuid.uuid4())
        payload = {"operation_id": operation_id, "base_revision": base_revision,
                   "conflict_ids": [str(item) for item in conflict_ids],
                   "content_text": content}
        with self.db:
            self.db.execute(
                "INSERT INTO sync_note_drafts "
                "(account_id, note_id, base_revision, content_text, "
                "operation_id, state, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (self.account_id, str(note_id), base_revision, content,
                 operation_id, datetime.now(UTC).isoformat()),
            )
            self.queue_operation(
                operation_id, "POST", f"/v1/notes/{note_id}/resolve", payload,
                "note", str(note_id), "note", str(note_id),
            )
            self._link_note_operation(note_id, operation_id)
        return operation_id

    def queue_note_delete(self, note_id: uuid.UUID, base_revision: int) -> str:
        self._require_cached_note(note_id, base_revision)
        self._require_no_pending_note_operation(note_id)
        operation_id = str(uuid.uuid4())
        with self.db:
            self.queue_operation(
                operation_id, "DELETE", f"/v1/notes/{note_id}",
                {"operation_id": operation_id, "base_revision": base_revision},
                "note", str(note_id), "note", str(note_id),
            )
            self._link_note_operation(note_id, operation_id)
        return operation_id

    def _require_cached_task(self, task_id: uuid.UUID) -> None:
        row = self.db.execute(
            "SELECT deleted_at FROM sync_entities WHERE account_id=? "
            "AND entity_type='task' AND entity_id=?",
            (self.account_id, str(task_id)),
        ).fetchone()
        if row is None or row["deleted_at"] is not None:
            raise ValueError("只能离线编辑已同步且未删除的卡片")
        self._require_project_cloud_allowed(self._task_project_id(str(task_id)))

    def _current_task_draft(self, task_id: uuid.UUID) -> tuple[int, str | None]:
        local = self.db.execute(
            "SELECT local_revision, submitted_attempt_id, state "
            "FROM sync_task_drafts WHERE account_id=? AND task_id=?",
            (self.account_id, str(task_id)),
        ).fetchone()
        cached = self.db.execute(
            "SELECT revision, payload_json FROM sync_entities WHERE account_id=? "
            "AND entity_type='task_draft' AND entity_id=?",
            (self.account_id, str(task_id)),
        ).fetchone()
        if local is not None:
            if local["state"] in {"conflict", "recovery"}:
                raise ValueError("卡片草稿存在待处理冲突")
            if cached is None or local["state"] == "pending" or (
                int(local["local_revision"]) >= int(cached["revision"])
            ):
                return int(local["local_revision"]), local["submitted_attempt_id"]
        if cached is None:
            return 0, None
        payload = json.loads(cached["payload_json"])
        return int(cached["revision"]), payload.get("submitted_attempt_id")

    def queue_task_draft(
        self, task_id: uuid.UUID, content: str, *,
        after_attempt_id: uuid.UUID | None = None,
    ) -> str:
        if len(content) > 20000:
            raise ValueError("回答草稿过长")
        self._require_cached_task(task_id)
        base_revision, submitted = self._current_task_draft(task_id)
        if submitted != (str(after_attempt_id) if after_attempt_id else None):
            raise ValueError("新草稿必须从最近一次已提交回答继续")
        operation_id = str(uuid.uuid4())
        payload = {"operation_id": operation_id, "base_revision": base_revision,
                   "content_text": content,
                   "after_attempt_id": str(after_attempt_id) if after_attempt_id else None}
        with self.db:
            self.db.execute(
                "INSERT INTO sync_task_drafts "
                "(account_id, task_id, local_revision, content_text, "
                "submitted_attempt_id, latest_operation_id, state, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, 'pending', ?) "
                "ON CONFLICT(account_id, task_id) DO UPDATE SET "
                "local_revision=excluded.local_revision, "
                "content_text=excluded.content_text, submitted_attempt_id=NULL, "
                "latest_operation_id=excluded.latest_operation_id, "
                "state='pending', updated_at=excluded.updated_at",
                (self.account_id, str(task_id), base_revision + 1, content,
                 operation_id, datetime.now(UTC).isoformat()),
            )
            self.queue_operation(operation_id, "PUT", f"/v1/tasks/{task_id}/draft", payload,
                        "task", str(task_id), "task_draft", str(task_id))
            self._link_task_operation(task_id, operation_id)
        return operation_id

    def rebase_task_draft(
        self, task_id: uuid.UUID, content: str, *,
        after_attempt_id: uuid.UUID | None = None,
    ) -> str:
        if len(content) > 20000:
            raise ValueError("回答草稿过长")
        self._require_cached_task(task_id)
        local = self.db.execute(
            "SELECT state FROM sync_task_drafts WHERE account_id=? AND task_id=?",
            (self.account_id, str(task_id)),
        ).fetchone()
        if local is None or local["state"] != "conflict":
            raise ValueError("没有需要重新整理的冲突草稿")
        pending = self.db.execute(
            "SELECT 1 FROM sync_outbox WHERE account_id=? AND dependency_type='task' "
            "AND dependency_id=? AND state='pending' LIMIT 1",
            (self.account_id, str(task_id)),
        ).fetchone()
        if pending is not None:
            raise ValueError("请先处理此卡片待同步操作")
        remote = self._cached_payload("task_draft", str(task_id))
        base_revision = int(remote.get("revision", 0))
        expected_parent = remote.get("submitted_attempt_id")
        if expected_parent != (str(after_attempt_id) if after_attempt_id else None):
            raise ValueError("请先核对云端已提交回答")
        operation_id = str(uuid.uuid4())
        payload = {"operation_id": operation_id, "base_revision": base_revision,
                   "content_text": content,
                   "after_attempt_id": str(after_attempt_id) if after_attempt_id else None}
        with self.db:
            self.db.execute(
                "UPDATE sync_task_drafts SET local_revision=?, content_text=?, "
                "submitted_attempt_id=NULL, latest_operation_id=?, state='pending', "
                "updated_at=? WHERE account_id=? AND task_id=?",
                (base_revision + 1, content, operation_id,
                 datetime.now(UTC).isoformat(), self.account_id, str(task_id)),
            )
            self.queue_operation(
                operation_id, "PUT", f"/v1/tasks/{task_id}/draft", payload,
                "task", str(task_id), "task_draft", str(task_id),
            )
            self._link_task_operation(task_id, operation_id)
        return operation_id

    def queue_answer(
        self, task_id: uuid.UUID, answer_text: str, *,
        parent_attempt_id: uuid.UUID | None = None, hint_level: int = 0,
    ) -> tuple[str, str]:
        if not answer_text.strip() or len(answer_text) > 20000 or not 0 <= hint_level <= 3:
            raise ValueError("回答正文或提示程度无效")
        self._require_cached_task(task_id)
        if parent_attempt_id is not None:
            parent_id = str(parent_attempt_id)
            parent = self.db.execute(
                "SELECT payload_json FROM sync_entities WHERE account_id=? "
                "AND entity_type='attempt' AND entity_id=?",
                (self.account_id, parent_id),
            ).fetchone()
            parent_task = (
                json.loads(parent["payload_json"]).get("task_id")
                if parent is not None else None
            )
            if parent_task is None:
                pending_parent = self.db.execute(
                    "SELECT task_id FROM sync_pending_attempts "
                    "WHERE account_id=? AND attempt_id=?",
                    (self.account_id, parent_id),
                ).fetchone()
                parent_task = pending_parent["task_id"] if pending_parent else None
            if parent_task != str(task_id):
                raise ValueError("修正回答的父回答不属于此卡片")
        base_revision, _ = self._current_task_draft(task_id)
        attempt_id, operation_id = str(uuid.uuid4()), str(uuid.uuid4())
        payload = {"operation_id": operation_id, "attempt_id": attempt_id,
                   "parent_attempt_id": str(parent_attempt_id) if parent_attempt_id else None,
                   "answer_text": answer_text, "hint_level": hint_level,
                   "actor_origin": "user"}
        created_at = datetime.now(UTC).isoformat()
        with self.db:
            self.db.execute(
                "INSERT INTO sync_pending_attempts VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (self.account_id, attempt_id, str(task_id), payload["parent_attempt_id"],
                 answer_text, hint_level, operation_id, created_at),
            )
            self.db.execute(
                "INSERT INTO sync_task_drafts "
                "(account_id, task_id, local_revision, content_text, "
                "submitted_attempt_id, latest_operation_id, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?) "
                "ON CONFLICT(account_id, task_id) DO UPDATE SET "
                "local_revision=excluded.local_revision, "
                "content_text=excluded.content_text, "
                "submitted_attempt_id=excluded.submitted_attempt_id, "
                "latest_operation_id=excluded.latest_operation_id, "
                "state='pending', updated_at=excluded.updated_at",
                (self.account_id, str(task_id), base_revision + 1, answer_text,
                 attempt_id, operation_id, created_at),
            )
            self.queue_operation(operation_id, "POST", f"/v1/tasks/{task_id}/attempts", payload,
                        "task", str(task_id), "attempt", attempt_id)
            self._link_task_operation(task_id, operation_id)
        return attempt_id, operation_id

    def queue_task_progress(
        self, task_id: uuid.UUID, base_revision: int,
        progress: str,
    ) -> str:
        if base_revision < 1 or progress not in {
            "in_progress", "paused", "completed", "dismissed"
        }:
            raise ValueError("卡片进度或版本无效")
        self._require_cached_task(task_id)
        operation_id = str(uuid.uuid4())
        payload = {"operation_id": operation_id, "base_revision": base_revision,
                   "progress": progress}
        with self.db:
            self.queue_operation(operation_id, "PATCH", f"/v1/tasks/{task_id}/progress", payload,
                        "task", str(task_id), "task", str(task_id))
            self._link_task_operation(task_id, operation_id)
        return operation_id

    def queue_review(
        self, task_id: uuid.UUID, *, operation_id: uuid.UUID, review_id: uuid.UUID,
        base_revision: int, answer_text: str, answer: str, hint_level: int,
    ) -> str:
        if (not answer_text.strip() or len(answer_text) > 20000
                or answer not in {"wrong", "hard", "good", "easy"}
                or not 0 <= hint_level <= 3 or base_revision < 1):
            raise ValueError("复习答案或评分无效")
        self._require_cached_task(task_id)
        schedule = self.db.execute(
            "SELECT revision,payload_json FROM sync_entities WHERE account_id=? "
            "AND entity_type='review_schedule' AND entity_id=?",
            (self.account_id, str(task_id)),
        ).fetchone()
        if schedule is None or int(schedule["revision"]) != base_revision:
            raise ValueError("复习版本已变化，请先同步")
        if json.loads(schedule["payload_json"]).get("status") != "active":
            raise ValueError("复习计划未启用")
        payload = {
            "operation_id": str(operation_id), "review_id": str(review_id),
            "base_revision": base_revision, "answer_text": answer_text,
            "answer": answer, "hint_level": hint_level, "actor_origin": "user",
        }
        encoded = _json(payload)
        existing = self.db.execute(
            "SELECT payload_json FROM sync_outbox WHERE account_id=? AND operation_id=?",
            (self.account_id, str(operation_id)),
        ).fetchone()
        if existing:
            if existing["payload_json"] != encoded:
                raise ValueError("同一复习操作不能更换答案")
            return str(operation_id)
        pending = self.db.execute(
            "SELECT 1 FROM sync_outbox WHERE account_id=? AND target_type='review_schedule' "
            "AND target_id=? AND state IN ('pending','conflict') LIMIT 1",
            (self.account_id, str(task_id)),
        ).fetchone()
        if pending:
            raise ValueError("此卡已有待同步的复习答案")
        with self.db:
            self.queue_operation(
                str(operation_id), "POST", f"/v1/tasks/{task_id}/reviews", payload,
                "task", str(task_id), "review_schedule", str(task_id),
            )
            self._link_task_operation(task_id, str(operation_id))
        return str(operation_id)

    def queue_operation(
        self, operation_id: str, method: str, path: str, payload: dict[str, Any],
        dependency_type: str | None, dependency_id: str | None,
        target_type: str | None, target_id: str | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO sync_outbox "
            "(account_id, operation_id, method, path, payload_json, dependency_type, "
            "dependency_id, target_type, target_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.account_id, operation_id, method, path, _json(payload),
             dependency_type, dependency_id, target_type, target_id,
             datetime.now(UTC).isoformat()),
        )

    def queue_source_excerpt(
        self, *, project_id: str, source_ref_id: str, payload: dict[str, Any],
    ) -> dict[str, str]:
        """Queue one explicitly reviewed source slice after its metadata is synced."""
        self._require_project_cloud_allowed(project_id)
        source = self._cached_payload("source_ref", source_ref_id)
        if source.get("source_ref_id") != source_ref_id:
            raise ValueError("源码引用尚未同步到此账号")
        operation_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"growth-source-excerpt-v1:{self.account_id}:{source_ref_id}",
        ))
        expected = _json(payload | {"operation_id": operation_id})
        with self.db:
            existing = self.db.execute(
                "SELECT payload_json,state FROM sync_outbox WHERE account_id=? "
                "AND operation_id=?", (self.account_id, operation_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != expected:
                    raise ValueError("已批准片段不能静默更换；请创建新快照")
                return {"operation_id": operation_id, "state": existing["state"]}
            self.queue_operation(
                operation_id, "POST", "/v1/source-excerpts",
                payload | {"operation_id": operation_id},
                "source_ref", source_ref_id, "source_excerpt", source_ref_id,
            )
            self._link_project_operation(project_id, operation_id)
        return {"operation_id": operation_id, "state": "pending"}

    def pending(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM sync_outbox WHERE account_id=? AND state='pending' "
            "ORDER BY rowid", (self.account_id,),
        )]

    def publication_enabled(self, operation_id: str) -> bool:
        row = self.db.execute(
            "SELECT policies.cloud_allowed FROM sync_publication_links links "
            "JOIN project_policies policies ON policies.project_id=links.project_id "
            "WHERE links.account_id=? AND links.operation_id=?",
            (self.account_id, operation_id),
        ).fetchone()
        return row is None or bool(row["cloud_allowed"])

    def resolved_conflict_in_pending_operation(
        self, operation: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not str(operation["path"]).endswith("/resolve"):
            return None
        payload = json.loads(operation["payload_json"])
        for conflict_id in payload["conflict_ids"]:
            conflict = self._cached_payload("note_conflict", str(conflict_id))
            if conflict.get("resolved_revision"):
                return {"code": "CONFLICT_ALREADY_RESOLVED",
                        "conflict_id": str(conflict_id),
                        "resolved_revision": conflict["resolved_revision"]}
        return None

    def apply_bootstrap(self, snapshot: dict[str, Any]) -> None:
        watermark = int(snapshot["high_watermark"])
        entities = snapshot["entities"]
        with self.db:
            self.db.execute(
                "DELETE FROM sync_entities WHERE account_id=?", (self.account_id,)
            )
            for name, (entity_type, id_field) in BOOTSTRAP_TYPES.items():
                for entity in entities[name]:
                    entity_id = str(entity[id_field])
                    if name == "note_revisions":
                        entity_id = note_revision_sync_id(entity_id, int(entity["revision"]))
                    elif name == "topic_links":
                        entity_id = topic_link_sync_id(
                            self.account_id, entity_id,
                            str(entity["entity_type"]), str(entity["entity_id"]),
                        )
                    self.db.execute(
                        "INSERT INTO sync_entities VALUES (?, ?, ?, ?, ?, ?)",
                        (self.account_id, entity_type, entity_id,
                         int(entity.get("revision", 1)), _json(entity),
                         entity.get("deleted_at")),
                    )
            self.db.execute(
                "UPDATE sync_accounts SET cursor=?, bootstrapped=1 WHERE account_id=?",
                (watermark, self.account_id),
            )
            for row in self.db.execute(
                "SELECT entity_id FROM sync_entities "
                "WHERE account_id=? AND entity_type='note' AND deleted_at IS NOT NULL",
                (self.account_id,),
            ):
                self._mark_deleted_draft(str(row["entity_id"]))

    def _mark_deleted_draft(self, note_id: str) -> None:
        self.db.execute(
            "UPDATE sync_note_drafts SET state='recovery' "
            "WHERE account_id=? AND note_id=? AND state IN ('pending', 'conflict')",
            (self.account_id, note_id),
        )
        self.db.execute(
            "UPDATE sync_outbox SET state='deleted' "
            "WHERE account_id=? AND target_type='note' AND target_id=? AND state='pending'",
            (self.account_id, note_id),
        )

    def apply_changes(self, page: dict[str, Any]) -> None:
        if int(page["next_after"]) < self.cursor():
            raise ValueError("同步游标不能倒退")
        with self.db:
            cursor = self.cursor()
            for change in page["changes"]:
                seq = int(change["change_seq"])
                if seq <= cursor:
                    continue
                if seq != cursor + 1:
                    raise ValueError("同步变更出现缺口")
                entity_type = str(change["entity_type"])
                entity_id = str(change["entity_id"])
                current = self.db.execute(
                    "SELECT revision, payload_json FROM sync_entities "
                    "WHERE account_id=? AND entity_type=? AND entity_id=?",
                    (self.account_id, entity_type, entity_id),
                ).fetchone()
                payload: dict[str, Any] = (
                    cast("dict[str, Any]", json.loads(current["payload_json"]))
                    if current else {}
                )
                payload.update(change["payload"])
                if (entity_type == "task"
                        and change.get("event_type") == "task_progress_changed"
                        and "to" in change["payload"]):
                    payload["progress"] = change["payload"]["to"]
                if (entity_type == "development_event"
                        and change.get("event_type") == "development_event_target_confirmed"
                        and payload.get("target_device_id")):
                    payload["status"] = "targeted"
                payload["revision"] = change["revision"]
                payload["entity_id"] = entity_id
                deleted_at = change["deleted_at"]
                if deleted_at:
                    payload["deleted_at"] = deleted_at
                self.db.execute(
                    "INSERT INTO sync_entities "
                    "(account_id, entity_type, entity_id, revision, payload_json, deleted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, entity_type, entity_id) DO UPDATE SET "
                    "revision=excluded.revision, payload_json=excluded.payload_json, "
                    "deleted_at=excluded.deleted_at",
                    (self.account_id, entity_type, entity_id, change["revision"],
                     _json(payload), deleted_at),
                )
                if entity_type == "note" and deleted_at:
                    self._mark_deleted_draft(entity_id)
                cursor = seq
            if cursor != int(page["next_after"]):
                raise ValueError("同步分页游标与内容不一致")
            self.db.execute(
                "UPDATE sync_accounts SET cursor=? WHERE account_id=?", (cursor, self.account_id)
            )

    def settle(self, operation_id: str, status: int, response: dict[str, Any]) -> None:
        state = "acked" if 200 <= status < 300 else (
            "conflict" if status == 409 else "deleted" if status == 410 else None
        )
        if state is None:
            raise ValueError("只有确定的业务结果才能结算 outbox")
        with self.db:
            operation = self.db.execute(
                "SELECT rowid, dependency_type, dependency_id FROM sync_outbox "
                "WHERE account_id=? AND operation_id=?",
                (self.account_id, operation_id),
            ).fetchone()
            self.db.execute(
                "UPDATE sync_outbox SET state=?, response_json=? "
                "WHERE account_id=? AND operation_id=? AND state='pending'",
                (state, _json(response), self.account_id, operation_id),
            )
            self.db.execute(
                "UPDATE sync_note_drafts SET state=? "
                "WHERE account_id=? AND operation_id=?",
                ({"acked": "synced", "conflict": "conflict", "deleted": "recovery"}[state],
                 self.account_id, operation_id),
            )
            self.db.execute(
                "UPDATE sync_task_drafts SET state=? "
                "WHERE account_id=? AND latest_operation_id=?",
                ({"acked": "synced", "conflict": "conflict", "deleted": "recovery"}[state],
                 self.account_id, operation_id),
            )
            self.db.execute(
                "UPDATE sync_pending_attempts SET state=? "
                "WHERE account_id=? AND operation_id=?",
                ({"acked": "synced", "conflict": "conflict", "deleted": "recovery"}[state],
                 self.account_id, operation_id),
            )
            if (state == "conflict" and operation is not None
                    and operation["dependency_type"] == "task"):
                blocked = {
                    "code": "PRIOR_TASK_OPERATION_CONFLICT",
                    "blocked_by": operation_id,
                }
                for later in self.db.execute(
                    "SELECT operation_id FROM sync_outbox WHERE account_id=? "
                    "AND dependency_type='task' AND dependency_id=? "
                    "AND rowid>? AND state='pending' ORDER BY rowid",
                    (self.account_id, operation["dependency_id"], operation["rowid"]),
                ).fetchall():
                    later_id = later["operation_id"]
                    self.db.execute(
                        "UPDATE sync_outbox SET state='conflict', response_json=? "
                        "WHERE account_id=? AND operation_id=?",
                        (_json(blocked), self.account_id, later_id),
                    )
                    self.db.execute(
                        "UPDATE sync_task_drafts SET state='conflict' "
                        "WHERE account_id=? AND latest_operation_id=?",
                        (self.account_id, later_id),
                    )
                    self.db.execute(
                        "UPDATE sync_pending_attempts SET state='conflict' "
                        "WHERE account_id=? AND operation_id=?",
                        (self.account_id, later_id),
                    )


class GrowthSyncClient:
    def __init__(self, store: GrowthSyncStore, client: httpx.AsyncClient, token: str):
        self.store = store
        self.client = client
        self.headers = {
            "Authorization": f"Bearer {token}", "X-Device-Id": store.device_id,
        }

    async def bootstrap(self) -> None:
        response = await self.client.get("/v1/sync/bootstrap", headers=self.headers)
        response.raise_for_status()
        self.store.apply_bootstrap(response.json())

    async def pull(self, *, limit: int = 100) -> int:
        count = 0
        boundary: int | None = None
        while True:
            cursor = self.store.cursor()
            query = {"after": cursor, "limit": limit}
            if boundary is not None:
                query["until"] = boundary
            response = await self.client.get(
                "/v1/sync/changes", params=query, headers=self.headers
            )
            response.raise_for_status()
            page = response.json()
            boundary = int(page["high_watermark"])
            self.store.apply_changes(page)
            count += len(page["changes"])
            if not page["has_more"]:
                return count

    async def acknowledge(self) -> int:
        """Confirm the durable local cursor with a stable retry identifier."""
        cursor = self.store.cursor()
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"growth-sync-ack-v1:{self.store.account_id}:{self.store.device_id}:{cursor}",
        )
        response = await self.client.post(
            "/v1/sync/ack", headers=self.headers,
            json={"operation_id": str(operation_id), "last_change_seq": cursor},
        )
        response.raise_for_status()
        return int(response.json()["last_change_seq"])

    async def push(self) -> int:
        count = 0
        for operation in self.store.pending():
            if not self.store.publication_enabled(operation["operation_id"]):
                continue
            resolved = self.store.resolved_conflict_in_pending_operation(operation)
            if resolved is not None:
                self.store.settle(operation["operation_id"], 409, resolved)
                break
            dependency_type = operation["dependency_type"]
            if dependency_type is not None:
                entity = self.store.db.execute(
                    "SELECT deleted_at FROM sync_entities "
                    "WHERE account_id=? AND entity_type=? AND entity_id=?",
                    (self.store.account_id, dependency_type, operation["dependency_id"]),
                ).fetchone()
                if entity is None:
                    break
                if entity["deleted_at"] is not None:
                    self.store.settle(operation["operation_id"], 410,
                                      {"code": "ENTITY_DELETED"})
                    continue
            response = await self.client.request(
                operation["method"], operation["path"],
                headers=self.headers, json=json.loads(operation["payload_json"]),
            )
            valid_conflict = (
                response.status_code == 409
                and response.json().get("code") in {
                    "REVISION_CONFLICT", "DRAFT_SUBMITTED", "INVALID_DRAFT_PARENT",
                    "SOURCE_EXCERPT_MISMATCH",
                }
            )
            if valid_conflict or response.status_code == 410 or 200 <= response.status_code < 300:
                self.store.settle(operation["operation_id"], response.status_code,
                                  response.json())
                count += 1
                if valid_conflict:
                    break
            elif response.status_code >= 500 or response.status_code in {408, 429}:
                break
            else:
                response.raise_for_status()
        return count

    async def push_batch(self, *, limit: int = 100) -> int:
        """Upload a contiguous outbox prefix, preserving each original operation ID."""
        if not 1 <= limit <= 100:
            raise ValueError("批量上传数量必须在 1 到 100 之间")
        ready: list[dict[str, Any]] = []
        for operation in self.store.pending():
            if len(ready) >= limit:
                break
            if not self.store.publication_enabled(operation["operation_id"]):
                continue
            resolved = self.store.resolved_conflict_in_pending_operation(operation)
            if resolved is not None:
                self.store.settle(operation["operation_id"], 409, resolved)
                break
            dependency_type = operation["dependency_type"]
            if dependency_type is not None:
                entity = self.store.db.execute(
                    "SELECT deleted_at FROM sync_entities "
                    "WHERE account_id=? AND entity_type=? AND entity_id=?",
                    (self.store.account_id, dependency_type,
                     operation["dependency_id"]),
                ).fetchone()
                if entity is None:
                    break
                if entity["deleted_at"] is not None:
                    self.store.settle(operation["operation_id"], 410,
                                      {"code": "ENTITY_DELETED"})
                    continue
            ready.append(operation)
        if not ready:
            return 0
        response = await self.client.post(
            "/v1/sync/operations", headers=self.headers,
            json={"operations": [
                {"method": row["method"], "path": row["path"],
                 "payload": json.loads(row["payload_json"])}
                for row in ready
            ]},
        )
        if response.status_code >= 500 or response.status_code in {408, 429}:
            return 0
        response.raise_for_status()
        raw_results: Any = response.json().get("results")
        if not isinstance(raw_results, list):
            raise ValueError("批量同步响应数量无效")
        items = cast("list[Any]", raw_results)
        if (not 1 <= len(items) <= len(ready)
                or any(not isinstance(item, dict) for item in items)):
            raise ValueError("批量同步响应数量无效")
        results = cast("list[dict[str, Any]]", items)
        count = 0
        for operation, result in zip(ready, results, strict=False):
            if result.get("operation_id") != operation["operation_id"]:
                raise ValueError("批量同步响应操作 ID 不匹配")
            status = int(result["status"])
            raw_body: Any = result["response"]
            if not isinstance(raw_body, dict):
                raise ValueError("批量同步响应正文无效")
            body = cast("dict[str, Any]", raw_body)
            if not (200 <= status < 300 or status == 410 or
                    status == 409 and body.get("code") in {
                        "REVISION_CONFLICT", "DRAFT_SUBMITTED",
                        "INVALID_DRAFT_PARENT", "SOURCE_EXCERPT_MISMATCH",
                    }):
                raise ValueError(f"批量同步操作失败：{status} {body.get('code')}")
            self.store.settle(operation["operation_id"], status, body)
            count += 1
            if status >= 400:
                break
        return count


async def connect_and_sync(
    ledger: GrowthLocalRepository, client: httpx.AsyncClient, token: str,
    *, publish_project_id: str | None = None,
    approved_digest: str | None = None,
    refresh_history: bool = False,
) -> dict[str, Any]:
    """Verify the account, retain a local device ID, then resume its durable sync state."""
    auth_header = {"Authorization": f"Bearer {token}"}
    identity = await client.get("/v1/account", headers=auth_header)
    identity.raise_for_status()
    account_id = uuid.UUID(identity.json()["account_id"])
    row = ledger.connection.execute(
        "SELECT device_id, bootstrapped FROM sync_accounts WHERE account_id=?",
        (str(account_id),),
    ).fetchone()
    device_id = uuid.UUID(row["device_id"]) if row is not None else uuid.uuid4()
    store = GrowthSyncStore(
        ledger, account_id, device_id, cloud_origin=str(client.base_url).rstrip("/")
    )
    queued = 0
    if publish_project_id is not None:
        from .publish import publication_plan, queue_publication

        if approved_digest is None:
            raise ValueError("发布本机项目需要预览摘要与明确批准")
        with ledger.connection:
            ledger.connection.execute("BEGIN IMMEDIATE")
            ledger.grant_cloud_sync(
                publish_project_id, str(account_id), str(client.base_url).rstrip("/")
            )
            queued = queue_publication(
                store, publication_plan(ledger, publish_project_id), approved_digest
            )
    registration_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"growth-device-registration-v1:{account_id}:{device_id}",
    )
    registration = await client.post(
        "/v1/devices", headers=auth_header | {"X-Device-Id": str(device_id)},
        json={"operation_id": str(registration_id), "device_id": str(device_id)},
    )
    registration.raise_for_status()
    sync = GrowthSyncClient(store, client, token)
    if refresh_history or row is None or not row["bootstrapped"]:
        await sync.bootstrap()
        pulled = 0
    else:
        pulled = await sync.pull()
    uploaded = await sync.push()
    pulled += await sync.pull()
    acknowledged = await sync.acknowledge()
    from .knowledge_sync import sync_knowledge

    knowledge = await sync_knowledge(ledger, client, token, account_id, device_id)
    counts = {
        item["state"]: int(item["total"])
        for item in ledger.connection.execute(
            "SELECT state, COUNT(*) AS total FROM sync_outbox "
            "WHERE account_id=? GROUP BY state", (str(account_id),),
        )
    }
    return {
        "account_id": str(account_id), "device_id": str(device_id),
        "cursor": store.cursor(), "acknowledged_cursor": acknowledged,
        "pulled": pulled, "uploaded": uploaded,
        "queued": queued,
        "outbox": {state: counts.get(state, 0) for state in (
            "pending", "acked", "conflict", "deleted"
        )},
        **knowledge,
    }
