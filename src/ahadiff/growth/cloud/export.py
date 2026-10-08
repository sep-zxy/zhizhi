"""Account-scoped, consistent growth-history export independent of ReMe."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import uuid

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .db import record_change

EXPORTED_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("projects", "growth_projects", ("project_id",)),
    ("features", "growth_features", ("feature_id",)),
    ("snapshots", "growth_snapshots", ("snapshot_id",)),
    ("source_refs", "growth_source_refs", ("source_ref_id",)),
    ("source_excerpts", "growth_source_excerpts", ("source_ref_id",)),
    ("analysis_runs", "growth_analysis_runs", ("analysis_id",)),
    ("opportunity_batches", "growth_opportunity_batches", ("analysis_id",)),
    ("opportunities", "growth_opportunities", ("opportunity_id",)),
    ("topic_proposals", "growth_topic_proposals", ("proposal_id",)),
    ("topics", "growth_topics", ("topic_id",)),
    ("topic_aliases", "growth_topic_aliases", ("alias_topic_id",)),
    ("topic_moves", "growth_topic_moves", ("move_id",)),
    ("topic_links", "growth_topic_links", ("topic_id", "entity_type", "entity_id")),
    ("modules", "growth_modules", ("module_id",)),
    ("tasks", "growth_tasks", ("task_id",)),
    ("task_drafts", "growth_task_drafts", ("task_id",)),
    ("attempts", "growth_learning_attempts", ("attempt_id",)),
    ("notes", "growth_notes", ("note_id",)),
    ("note_revisions", "growth_note_revisions", ("note_id", "revision")),
    ("note_conflicts", "growth_note_conflicts", ("conflict_id",)),
    ("timeline", "growth_timeline", ("evidence_id",)),
    ("review_schedules", "growth_review_schedules", ("task_id",)),
    ("review_events", "growth_review_events", ("review_id",)),
    ("chat_sessions", "growth_chat_sessions", ("session_id",)),
    ("chat_messages", "growth_chat_messages", ("message_id",)),
    ("chat_suggestions", "growth_chat_suggestions", ("suggestion_id",)),
    ("development_events", "growth_development_events", ("event_id",)),
    ("domain_events", "growth_domain_events", ("occurred_at", "event_id")),
    ("change_feed", "growth_change_feed", ("change_seq",)),
)


def _quote(text: str) -> str:
    return "\n".join("> " + line for line in text.splitlines()) or "> "


def _markdown(payload: dict[str, Any]) -> str:
    tables = payload["tables"]
    lines = ["# 工程成长记录导出", "",
             f"导出时间：{payload['created_at']}",
             f"同步水位：{payload['snapshot_seq']}",
             f"账号：{payload['account_id']}", "", "## 主题", ""]
    for topic in tables["topics"]:
        lines.extend([
            f"### {topic['title']}", "",
            f"ID：`{topic['topic_id']}`；状态：{topic['status']}；"
            f"父主题：`{topic['parent_topic_id'] or '无'}`", "",
        ])
    lines.extend(["## 笔记与修订", ""])
    revisions: dict[str, list[dict[str, Any]]] = {}
    for revision in tables["note_revisions"]:
        revisions.setdefault(revision["note_id"], []).append(revision)
    for note in tables["notes"]:
        lines.extend([
            f"### 笔记 `{note['note_id']}`", "",
            f"主题：`{note['topic_id']}`；卡片：`{note['task_id'] or '无'}`；"
            f"版本：{note['revision']}；删除：{note['deleted_at'] or '否'}；"
            f"作者：{note['author']}", "",
            _quote(note["content_text"]), "", "修订历史：", "",
        ])
        for revision in revisions.get(note["note_id"], []):
            lines.extend([f"- 版本 {revision['revision']}，哈希 `{revision['content_hash']}`",
                          _quote(revision["content_text"]), ""])
    lines.extend(["## 回答与反馈", ""])
    for attempt in tables["attempts"]:
        lines.extend([f"### 回答 `{attempt['attempt_id']}`", "",
                      f"卡片：`{attempt['task_id']}`；来源：{attempt['actor_origin']}；"
                      f"反馈来源：{attempt['feedback_origin'] or '无'}", "",
                      _quote(attempt["answer_text"]), ""])
    lines.extend(["## 完整结构化历史", "",
                  "以下 JSON 包含主题别名、合并/拆分关系、冲突分支、删除记录、"
                  "来源引用和同步事件，便于恢复或复核。", ""])
    for line in json.dumps(tables, ensure_ascii=False, indent=2).splitlines():
        lines.append("    " + line)
    lines.append("")
    return "\n".join(lines)


def create_export(
    conn: psycopg.Connection[dict[str, Any]], *, account_id: uuid.UUID,
    trace_id: uuid.UUID, export_id: uuid.UUID,
) -> tuple[int, dict[str, Any]]:
    """Hold the account sync head so all exported tables share one committed boundary."""
    head = conn.execute(
        "SELECT last_seq FROM growth_sync_heads WHERE account_id=%s FOR UPDATE",
        (account_id,),
    ).fetchone()
    watermark = int(head["last_seq"]) if head else 0
    tables: dict[str, list[dict[str, Any]]] = {}
    for name, table, order_columns in EXPORTED_TABLES:
        order = sql.SQL(", ").join(sql.Identifier(column) for column in order_columns)
        rows = conn.execute(
            sql.SQL("SELECT * FROM {} WHERE account_id=%s ORDER BY {}").format(
                sql.Identifier(table), order,
            ),
            (account_id,),
        ).fetchall()
        tables[name] = json.loads(json.dumps(rows, default=str))
    payload = {"schema_version": 1, "account_id": str(account_id),
               "export_id": str(export_id), "snapshot_seq": watermark,
               "created_at": datetime.now(UTC).isoformat(), "tables": tables}
    markdown = _markdown(payload)
    conn.execute(
        "INSERT INTO growth_exports "
        "(account_id, export_id, snapshot_seq, json_payload, markdown_text) "
        "VALUES (%s, %s, %s, %s, %s)",
        (account_id, export_id, watermark, Jsonb(payload), markdown),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="export", entity_id=export_id,
        event_type="growth_history_exported", revision=1,
        payload={"export_id": str(export_id), "snapshot_seq": watermark},
    )
    return 201, {"export_id": str(export_id), "snapshot_seq": watermark,
                 "change_seq": seq}
