"""Build an explicit, immutable cloud publication from the local growth ledger."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from ahadiff.safety.redact import scan_text_for_secrets

from .cloud.models import (
    AnalysisResult,
    AttemptCreate,
    AttemptFeedback,
    FeatureCreate,
    NoteCreate,
    ProjectCreate,
    SnapshotCreate,
    TaskCreate,
    TaskProgressUpdate,
    TopicCreate,
    TopicDecision,
)

if TYPE_CHECKING:
    from .local import GrowthLocalRepository
    from .sync import GrowthSyncStore


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _operation_id(kind: str, source_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"growth-local-publish-v1:{kind}:{source_id}"))


def _row(db: Any, query: str, key: str) -> dict[str, Any]:
    found = db.execute(query, (key,)).fetchone()
    if found is None:
        raise ValueError("本机成长记录的关联对象缺失")
    return dict(found)


def publication_plan(ledger: GrowthLocalRepository, project_id: str) -> dict[str, Any]:
    """Return exactly the ordered request bodies proposed for cloud upload."""
    db = ledger.connection
    project = _row(db, "SELECT * FROM projects WHERE project_id=?", project_id)
    operations: list[dict[str, Any]] = []
    has_outbox = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_outbox'"
    ).fetchone() is not None

    def add(kind: str, source_id: str, method: str, path: str,
            payload: dict[str, Any], model: Any) -> None:
        body = model.model_validate(payload).model_dump(mode="json", exclude_none=True)
        if has_outbox:
            queued = db.execute(
                "SELECT method,path,payload_json,target_type,target_id FROM sync_outbox "
                "WHERE operation_id=?", (body["operation_id"],),
            ).fetchone()
            if queued is not None:
                if (queued["method"] != method or queued["path"] != path
                        or queued["target_type"] != kind
                        or queued["target_id"] != source_id):
                    raise ValueError("已入队发布操作的来源或目标发生变化")
                body = model.model_validate(
                    json.loads(queued["payload_json"])
                ).model_dump(mode="json", exclude_none=True)
        encoded = _json(body)
        if re.search(r"(?i)(?:[a-z]:[\\/]|/(?:home|users)/[^/\s]+)", encoded):
            raise ValueError("待同步内容包含本机绝对路径")
        if scan_text_for_secrets(encoded, source_name="growth_cloud_payload"):
            raise ValueError("待同步内容疑似包含密钥；请先检查本机记录")
        operations.append({"method": method, "path": path, "payload": body,
                           "kind": kind, "source_id": source_id})

    add("project", project_id, "POST", "/v1/projects", {
        "operation_id": _operation_id("project", project_id),
        "project_id": project_id, "name": project["name"],
        "sync_policy": {"cloud_allowed": True, "model_allowed": False},
    }, ProjectCreate)
    features = [dict(row) for row in db.execute(
        "SELECT * FROM features WHERE project_id=? ORDER BY created_at, rowid", (project_id,)
    )]
    for feature in features:
        feature_id = feature["feature_id"]
        add("feature", feature_id, "POST", "/v1/features", {
            "operation_id": _operation_id("feature", feature_id),
            "feature_id": feature_id, "project_id": project_id,
            "label": feature["label"], "base_ref": feature["base_ref"],
            "start_base_sha": feature["start_base_sha"],
        }, FeatureCreate)

    for topic in db.execute(
        "SELECT DISTINCT topics.topic_id, topics.title, topics.created_at "
        "FROM local_explorations AS explorations "
        "JOIN growth_topics AS topics ON topics.topic_id=explorations.topic_id "
        "WHERE explorations.project_id=? AND topics.status='active' "
        "ORDER BY topics.created_at, topics.topic_id", (project_id,),
    ):
        topic_id = topic["topic_id"]
        add("exploration_topic", topic_id, "POST", "/v1/topics", {
            "operation_id": _operation_id("exploration_topic", topic_id),
            "topic_id": topic_id, "title": topic["title"],
        }, TopicCreate)

    for snapshot in db.execute(
        "SELECT snapshots.* FROM snapshots JOIN features USING(feature_id) "
        "WHERE features.project_id=? ORDER BY snapshots.created_at, snapshots.rowid",
        (project_id,),
    ):
        snapshot_id = snapshot["snapshot_id"]
        feature_id = snapshot["feature_id"]
        refs = [dict(row) for row in db.execute(
            "SELECT source_ref_id, relative_path, blob_hash FROM source_refs "
            "WHERE snapshot_id=? ORDER BY relative_path", (snapshot_id,),
        )]
        add("snapshot", snapshot_id, "POST", "/v1/snapshots", {
            "operation_id": _operation_id("snapshot", snapshot_id),
            "project_id": project_id, "feature_id": feature_id,
            "snapshot_id": snapshot_id,
            "resolved_base_sha": snapshot["resolved_base_sha"],
            "head_sha": snapshot["head_sha"],
            "effective_tree_hash": snapshot["effective_tree_hash"],
            "diff_hash": snapshot["diff_hash"],
            "capture_scope": json.loads(snapshot["capture_scope"]),
            "privacy_policy_version": snapshot["privacy_policy_version"],
            "source_refs": refs,
        }, SnapshotCreate)

    opportunity_ids: set[str] = set()
    for analysis in db.execute(
        "SELECT analysis_runs.*, snapshots.feature_id, snapshots.resolved_base_sha, "
        "snapshots.head_sha, snapshots.effective_tree_hash, snapshots.diff_hash, "
        "snapshots.capture_scope, snapshots.privacy_policy_version "
        "FROM analysis_runs JOIN snapshots USING(snapshot_id) "
        "JOIN features USING(feature_id) WHERE features.project_id=? "
        "AND analysis_runs.status='succeeded' "
        "ORDER BY analysis_runs.created_at, analysis_runs.rowid", (project_id,),
    ):
        batch = db.execute(
            "SELECT * FROM opportunity_batches WHERE analysis_id=?",
            (analysis["analysis_id"],),
        ).fetchone()
        if batch is None:
            continue
        refs = [dict(row) for row in db.execute(
            "SELECT source_ref_id, relative_path, blob_hash FROM source_refs "
            "WHERE snapshot_id=? ORDER BY relative_path", (analysis["snapshot_id"],)
        )]
        opportunities: list[dict[str, Any]] = []
        for opportunity in db.execute(
            "SELECT * FROM opportunities WHERE analysis_id=? ORDER BY ordinal",
            (analysis["analysis_id"],),
        ):
            opportunity_ids.add(opportunity["opportunity_id"])
            proposals = [dict(row) for row in db.execute(
                "SELECT proposal_id, title FROM topic_proposals "
                "WHERE opportunity_id=? ORDER BY ordinal", (opportunity["opportunity_id"],)
            )]
            opportunities.append({
                "opportunity_id": opportunity["opportunity_id"],
                "title": opportunity["title"], "reason": opportunity["reason"],
                "learning_goal": opportunity["learning_goal"],
                "source_refs": json.loads(opportunity["source_refs_json"]),
                "estimated_minutes": opportunity["estimated_minutes"],
                "uncertainties": json.loads(opportunity["uncertainties_json"]),
                "topic_proposals": proposals,
            })
        analysis_id = analysis["analysis_id"]
        add("analysis", analysis_id, "POST", "/v1/analysis-results", {
            "operation_id": _operation_id("analysis", analysis_id),
            "project_id": project_id, "feature_id": analysis["feature_id"],
            "snapshot_id": analysis["snapshot_id"], "analysis_id": analysis_id,
            "resolved_base_sha": analysis["resolved_base_sha"],
            "head_sha": analysis["head_sha"],
            "effective_tree_hash": analysis["effective_tree_hash"],
            "diff_hash": analysis["diff_hash"],
            "capture_scope": json.loads(analysis["capture_scope"]),
            "privacy_policy_version": analysis["privacy_policy_version"],
            "input_fingerprint": analysis["input_fingerprint"],
            "mode": analysis["mode"], "status": analysis["status"],
            "upstream_run_id": analysis["upstream_run_id"],
            "source_refs": refs, "generation_origin": batch["origin"],
            "provider_name": batch["provider_name"],
            "model_name": batch["model_name"],
            "provider_request_id": batch["provider_request_id"],
            "opportunities": opportunities,
        }, AnalysisResult)

    task_revisions: dict[str, int] = {}
    task_ids: set[str] = set()
    for event in db.execute("SELECT * FROM growth_events ORDER BY created_at, rowid"):
        event_type, entity_id = event["event_type"], event["entity_id"]
        if event_type in {"topic_confirmed", "topic_rejected"}:
            proposal = _row(db, "SELECT * FROM topic_proposals WHERE proposal_id=?", entity_id)
            if proposal["opportunity_id"] not in opportunity_ids:
                continue
            add("topic_decision", event["event_id"], "POST",
                f"/v1/topic-proposals/{entity_id}/decision", {
                    "operation_id": _operation_id("event", event["event_id"]),
                    "decision": "confirm" if event_type == "topic_confirmed" else "reject",
                    "topic_id": proposal["topic_id"] if event_type == "topic_confirmed" else None,
                    "reuse_existing": bool(
                        json.loads(event["payload_json"]).get("reuse_existing", False)
                    ),
                }, TopicDecision)
        elif event_type == "task_created":
            task = _row(db, "SELECT * FROM growth_tasks WHERE task_id=?", entity_id)
            card = _row(db, "SELECT * FROM knowledge_cards WHERE card_id=?", entity_id)
            if task["opportunity_id"] not in opportunity_ids:
                continue
            task_ids.add(entity_id)
            task_revisions[entity_id] = 1
            add("task", entity_id, "POST", "/v1/tasks", {
                "operation_id": _operation_id("task", entity_id),
                "task_id": entity_id, "opportunity_id": task["opportunity_id"],
                "topic_id": task["topic_id"], "question": task["question"],
                "learning_goal": card["learning_goal"],
                "back_answer": card["back_answer"],
                "back_explanation": card["back_explanation"],
                "card_version": card["card_version"],
                "source_commit_shas": [row[0] for row in db.execute(
                    "SELECT commit_sha FROM card_source_commits WHERE card_id=? "
                    "ORDER BY created_at,commit_sha", (entity_id,),
                )],
                "followups": json.loads(task["followups_json"]),
                "module_id": task["module_id"],
                "module_index_revision": task["module_index_revision"],
            }, TaskCreate)
        elif event_type == "answer_saved":
            attempt = _row(db, "SELECT * FROM learning_attempts WHERE attempt_id=?", entity_id)
            task_id = attempt["task_id"]
            if task_id not in task_ids:
                continue
            add("attempt", entity_id, "POST", f"/v1/tasks/{task_id}/attempts", {
                "operation_id": _operation_id("attempt", entity_id),
                "attempt_id": entity_id,
                "parent_attempt_id": attempt["parent_attempt_id"],
                "answer_text": attempt["answer_text"],
                "hint_level": attempt["hint_level"],
                "actor_origin": attempt["actor_origin"],
            }, AttemptCreate)
            task_revisions[task_id] += 1
        elif event_type == "feedback_ready":
            attempt = _row(db, "SELECT * FROM learning_attempts WHERE attempt_id=?", entity_id)
            if attempt["task_id"] not in task_ids:
                continue
            add("feedback", entity_id, "POST", f"/v1/attempts/{entity_id}/feedback", {
                "operation_id": _operation_id("feedback", entity_id),
                "feedback_origin": attempt["feedback_origin"],
                "feedback": json.loads(attempt["feedback_json"]),
            }, AttemptFeedback)
        elif event_type == "task_progress_changed" and entity_id in task_ids:
            payload = json.loads(event["payload_json"])
            add("progress", event["event_id"], "PATCH",
                f"/v1/tasks/{entity_id}/progress", {
                    "operation_id": _operation_id("event", event["event_id"]),
                    "base_revision": task_revisions[entity_id],
                    "progress": payload["to"],
                }, TaskProgressUpdate)
            task_revisions[entity_id] += 1
        elif event_type == "note_saved":
            note = _row(db, "SELECT * FROM engineering_notes WHERE note_id=?", entity_id)
            if note["task_id"] not in task_ids:
                continue
            add("note", entity_id, "POST", "/v1/notes", {
                "operation_id": _operation_id("note", entity_id),
                "note_id": entity_id, "task_id": note["task_id"],
                "topic_id": note["topic_id"], "content_text": note["content_text"],
            }, NoteCreate)

    digest = hashlib.sha256(_json(operations).encode("utf-8")).hexdigest()
    return {"project_id": project_id, "digest": digest, "operations": operations,
            "count": len(operations)}


def queue_publication(store: GrowthSyncStore, plan: dict[str, Any],
                      approved_digest: str) -> int:
    """Persist the approved exact payloads; retries keep their original IDs."""
    if plan["digest"] != approved_digest:
        raise ValueError("待同步内容已变化，请重新预览并确认")
    owner = store.db.execute(
        "SELECT account_id FROM sync_outbox WHERE target_type='project' AND target_id=? "
        "LIMIT 1", (plan["project_id"],),
    ).fetchone()
    if owner is not None and owner["account_id"] != store.account_id:
        raise ValueError("本机项目已发布到另一云端账号")
    count = 0
    store.db.execute("SAVEPOINT growth_queue_publication")
    try:
        for operation in plan["operations"]:
            payload = operation["payload"]
            operation_id = payload["operation_id"]
            existing = store.db.execute(
                "SELECT payload_json FROM sync_outbox WHERE account_id=? AND operation_id=?",
                (store.account_id, operation_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != _json(payload):
                    raise ValueError("已入队操作内容变化，不能复用原幂等 ID")
                store.db.execute(
                    "INSERT OR IGNORE INTO sync_publication_links VALUES (?, ?, ?)",
                    (store.account_id, operation_id, plan["project_id"]),
                )
                continue
            store.queue_operation(operation_id, operation["method"], operation["path"],
                                  payload, None, None,
                                  operation["kind"], operation["source_id"])
            store.db.execute(
                "INSERT INTO sync_publication_links VALUES (?, ?, ?)",
                (store.account_id, operation_id, plan["project_id"]),
            )
            count += 1
    except Exception:
        store.db.execute("ROLLBACK TO SAVEPOINT growth_queue_publication")
        store.db.execute("RELEASE SAVEPOINT growth_queue_publication")
        raise
    store.db.execute("RELEASE SAVEPOINT growth_queue_publication")
    return count
