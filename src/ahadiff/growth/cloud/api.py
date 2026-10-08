"""Account-scoped cloud API for growth records; no local file or shell access."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from ahadiff.core.errors import InputError
from ahadiff.growth.sync import note_revision_sync_id, topic_link_sync_id
from ahadiff.review.scheduler import normalize_fsrs_state, review_fsrs_card, scheduler_version
from ahadiff.safety.redact import scan_text_for_secrets

from .auth import AuthenticationError, AuthVerifier
from .db import connect, enqueue_projection, payload_hash, record_change
from .export import create_export
from .memory import (ReMeAdapter, current_attempts_for_paths,
                     current_notes_for_paths, linked_objective_answers_for_notes)
from .models import (
    AnalysisResult,
    AttemptCreate,
    AttemptFeedback,
    ChatAssistantLive,
    ChatAssistantReplay,
    ChatStart,
    ChatSuggestionDecision,
    ChatUserMessage,
    DevelopmentEvent,
    DevelopmentEventClaim,
    DeviceCursorAck,
    DeviceRegistration,
    DeviceRevocation,
    ExportCreate,
    FeatureCreate,
    KnowledgeBundleUpsert,
    ModuleEdit,
    ModuleMap,
    NoteCreate,
    NoteDelete,
    NoteResolve,
    NoteRevision,
    OperationModel,
    ProjectCreate,
    ReviewSubmit,
    SnapshotCreate,
    SourceExcerptCreate,
    SyncOperationItem,
    SyncOperationsBatch,
    TaskCreate,
    TaskDraftUpdate,
    TaskProgressUpdate,
    TopicCreate,
    TopicDecision,
    TopicMerge,
    TopicSplit,
    TopicUpdate,
)

if TYPE_CHECKING:
    from starlette.requests import Request

    from ahadiff.review.schemas import ReviewAnswer


@dataclass(frozen=True)
class CloudSettings:
    database_url: str
    auth: AuthVerifier
    reme_url: str | None = None
    acceptance_mode: bool = False

    @classmethod
    def from_environment(cls) -> CloudSettings:
        issuer = os.environ["GROWTH_AUTH_ISSUER"].rstrip("/")
        return cls(
            database_url=os.environ["GROWTH_DATABASE_URL"],
            auth=AuthVerifier(
                issuer=issuer,
                audience=os.environ.get("GROWTH_AUTH_AUDIENCE", "authenticated"),
                jwks_url=os.environ.get("GROWTH_AUTH_JWKS_URL")
                or issuer + "/.well-known/jwks.json",
            ),
            reme_url=os.environ.get("GROWTH_REME_URL"),
        )


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        super().__init__(message)


def _body(model: type[OperationModel], value: Any) -> OperationModel:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise ApiError("INVALID_REQUEST", "请求字段不符合接口契约", 422) from exc


def _device_id(request: Request) -> uuid.UUID:
    try:
        return uuid.UUID(request.headers["X-Device-Id"])
    except (KeyError, ValueError) as exc:
        raise ApiError("DEVICE_REQUIRED", "需要已注册的设备 ID", 401) from exc


async def _account_id(request: Request, settings: CloudSettings) -> uuid.UUID:
    header = request.headers.get("Authorization", "")
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0] != "Bearer" or not parts[1]:
        raise ApiError("AUTH_REQUIRED", "需要用户访问令牌", 401)
    try:
        return await asyncio.to_thread(settings.auth.verify, parts[1])
    except AuthenticationError as exc:
        raise ApiError("AUTH_INVALID", "用户访问令牌验证失败", 401) from exc


ApplyOperation = Callable[
    [psycopg.Connection[Any], uuid.UUID, uuid.UUID], tuple[int, dict[str, Any]]
]


def _write_operation(
    settings: CloudSettings,
    *,
    account_id: uuid.UUID,
    device_id: uuid.UUID | None,
    trace_id: uuid.UUID,
    operation_id: uuid.UUID,
    endpoint: str,
    payload: dict[str, Any],
    apply: ApplyOperation,
) -> tuple[int, dict[str, Any]]:
    digest = payload_hash(endpoint, payload)
    try:
        with connect(settings.database_url) as conn:
            conn.execute(
                "INSERT INTO growth_accounts(account_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (account_id,),
            )
            if device_id is not None:
                device = conn.execute(
                    "SELECT revoked_at FROM growth_devices WHERE account_id=%s AND device_id=%s",
                    (account_id, device_id),
                ).fetchone()
                if device is None or device["revoked_at"] is not None:
                    raise ApiError("DEVICE_REVOKED", "设备未注册或已撤销", 403)
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{account_id}:{operation_id}",),
            )
            existing = conn.execute(
                "SELECT payload_hash, status_code, response FROM growth_sync_operations "
                "WHERE account_id=%s AND operation_id=%s",
                (account_id, operation_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"].strip() != digest:
                    raise ApiError("IDEMPOTENCY_KEY_REUSED", "同一操作 ID 对应了不同内容", 409)
                return int(existing["status_code"]), existing["response"]
            status, response = apply(conn, account_id, trace_id)
            response = {**response, "trace_id": str(trace_id)}
            conn.execute(
                "INSERT INTO growth_sync_operations "
                "(account_id, operation_id, payload_hash, status_code, response) "
                "VALUES (%s, %s, %s, %s, %s)",
                (account_id, operation_id, digest, status, Jsonb(response)),
            )
        return status, response
    except psycopg.errors.UniqueViolation as exc:
        raise ApiError("ENTITY_ALREADY_EXISTS", "对象 ID 已存在", 409) from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        raise ApiError("INVALID_REFERENCE", "引用的项目或对象不存在", 409) from exc


def _register_device(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    body: DeviceRegistration,
) -> tuple[int, dict[str, Any]]:
    row = conn.execute(
        "SELECT revoked_at FROM growth_devices WHERE account_id=%s AND device_id=%s",
        (account_id, body.device_id),
    ).fetchone()
    if row is not None and row["revoked_at"] is not None:
        raise ApiError("DEVICE_REVOKED", "已撤销设备不能重新注册", 403)
    conn.execute(
        "INSERT INTO growth_devices(account_id, device_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (account_id, body.device_id),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="device",
        entity_id=body.device_id,
        event_type="device_registered",
        revision=1,
        payload={"device_id": str(body.device_id)},
    )
    return 201, {"device_id": str(body.device_id), "change_seq": seq}


def _ack_device(
    conn: psycopg.Connection[Any], account_id: uuid.UUID,
    device_id: uuid.UUID, body: DeviceCursorAck,
) -> tuple[int, dict[str, Any]]:
    head = conn.execute(
        "SELECT last_seq FROM growth_sync_heads WHERE account_id=%s",
        (account_id,),
    ).fetchone()
    watermark = int(head["last_seq"]) if head else 0
    if body.last_change_seq > watermark:
        raise ApiError("INVALID_CURSOR", "设备确认游标超出当前水位", 422)
    updated = conn.execute(
        "UPDATE growth_devices SET last_change_seq=GREATEST(last_change_seq, %s), "
        "last_seen=now() WHERE account_id=%s AND device_id=%s "
        "RETURNING last_change_seq",
        (body.last_change_seq, account_id, device_id),
    ).fetchone()
    assert updated is not None
    return 200, {"device_id": str(device_id),
                 "last_change_seq": int(updated["last_change_seq"])}


def _revoke_device(
    conn: psycopg.Connection[Any], account_id: uuid.UUID,
    trace_id: uuid.UUID, requester: uuid.UUID,
    target: uuid.UUID, _body: DeviceRevocation,
) -> tuple[int, dict[str, Any]]:
    if target == requester:
        raise ApiError("SELF_REVOKE_FORBIDDEN", "请从另一台设备撤销当前设备", 409)
    revoked = conn.execute(
        "UPDATE growth_devices SET revoked_at=now() "
        "WHERE account_id=%s AND device_id=%s AND revoked_at IS NULL "
        "RETURNING revoked_at",
        (account_id, target),
    ).fetchone()
    if revoked is None:
        raise ApiError("DEVICE_NOT_FOUND", "目标设备不存在或已经撤销", 404)
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="device", entity_id=target,
        event_type="device_revoked", revision=2,
        payload={"device_id": str(target),
                 "revoked_at": revoked["revoked_at"].isoformat()},
    )
    return 200, {"device_id": str(target), "change_seq": seq,
                 "revoked_at": revoked["revoked_at"].isoformat()}


def _create_project(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    body: ProjectCreate,
) -> tuple[int, dict[str, Any]]:
    policy = body.sync_policy.model_dump()
    if not policy["cloud_allowed"]:
        raise ApiError("CLOUD_DISABLED", "此项目没有启用云同步", 409)
    conn.execute(
        "INSERT INTO growth_projects(account_id, project_id, name, sync_policy) "
        "VALUES (%s, %s, %s, %s)",
        (account_id, body.project_id, body.name, Jsonb(policy)),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="project",
        entity_id=body.project_id,
        event_type="project_created",
        revision=1,
        payload={"project_id": str(body.project_id), "name": body.name, "sync_policy": policy},
    )
    return 201, {"project_id": str(body.project_id), "revision": 1, "change_seq": seq}


def _create_topic(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    body: TopicCreate,
) -> tuple[int, dict[str, Any]]:
    conn.execute(
        "INSERT INTO growth_topics(account_id, topic_id, title) VALUES (%s, %s, %s)",
        (account_id, body.topic_id, body.title),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="topic", entity_id=body.topic_id,
        event_type="topic_user_created", revision=1,
        payload={"topic_id": str(body.topic_id), "title": body.title,
                 "status": "active", "actor_origin": "user"},
    )
    return 201, {"topic_id": str(body.topic_id), "revision": 1, "change_seq": seq}


def _start_chat(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    body: ChatStart,
) -> tuple[int, dict[str, Any]]:
    if body.project_id is not None:
        project = conn.execute(
            "SELECT sync_policy FROM growth_projects WHERE account_id=%s "
            "AND project_id=%s AND deleted_at IS NULL",
            (account_id, body.project_id),
        ).fetchone()
        if project is None:
            raise ApiError("PROJECT_NOT_FOUND", "聊天项目不存在", 404)
        if not project["sync_policy"].get("cloud_allowed", False):
            raise ApiError("CLOUD_DISABLED", "项目未开启云同步", 409)
    conn.execute(
        "INSERT INTO growth_chat_sessions(account_id, session_id, project_id) "
        "VALUES (%s, %s, %s)",
        (account_id, body.session_id, body.project_id),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="chat_session", entity_id=body.session_id,
        event_type="chat_started", revision=1,
        payload={"session_id": str(body.session_id),
                 "project_id": str(body.project_id) if body.project_id else None},
    )
    return 201, {"session_id": str(body.session_id), "change_seq": seq}


def _chat_user_message(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    session_id: uuid.UUID, body: ChatUserMessage,
) -> tuple[int, dict[str, Any]]:
    chat = conn.execute(
        "SELECT 1 FROM growth_chat_sessions WHERE account_id=%s AND session_id=%s",
        (account_id, session_id),
    ).fetchone()
    if chat is None:
        raise ApiError("CHAT_NOT_FOUND", "对话不存在", 404)
    conn.execute(
        "INSERT INTO growth_chat_messages "
        "(account_id, message_id, session_id, role, origin, content_text) "
        "VALUES (%s, %s, %s, 'user', 'user', %s)",
        (account_id, body.message_id, session_id, body.content_text),
    )
    conn.execute(
        "UPDATE growth_chat_sessions SET updated_at=now() "
        "WHERE account_id=%s AND session_id=%s", (account_id, session_id),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="chat_message", entity_id=body.message_id,
        event_type="chat_user_message", revision=1,
        payload={"session_id": str(session_id), "role": "user",
                 "content_text": body.content_text},
    )
    return 201, {"message_id": str(body.message_id), "change_seq": seq}


def _chat_assistant_message(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    session_id: uuid.UUID, body: ChatAssistantReplay | ChatAssistantLive,
    *, origin: str,
) -> tuple[int, dict[str, Any]]:
    chat = conn.execute(
        "SELECT 1 FROM growth_chat_sessions WHERE account_id=%s AND session_id=%s",
        (account_id, session_id),
    ).fetchone()
    if chat is None:
        raise ApiError("CHAT_NOT_FOUND", "对话不存在", 404)
    live = body if isinstance(body, ChatAssistantLive) else None
    conn.execute(
        "INSERT INTO growth_chat_messages "
        "(account_id, message_id, session_id, role, origin, content_text, "
        "provider_name, model_name, provider_request_id, request_payload_hash, "
        "response_payload_hash, input_tokens, output_tokens, elapsed_ms) "
        "VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (account_id, body.message_id, session_id, origin, body.content_text,
         live.provider_name if live else None, live.model_name if live else None,
         live.provider_request_id if live else None,
         live.request_payload_hash if live else None,
         live.response_payload_hash if live else None,
         live.input_tokens if live else None, live.output_tokens if live else None,
         live.elapsed_ms if live else None),
    )
    conn.execute(
        "UPDATE growth_chat_sessions SET updated_at=now() "
        "WHERE account_id=%s AND session_id=%s", (account_id, session_id),
    )
    provenance = (
        {"provider_name": body.provider_name, "model_name": body.model_name,
         "provider_request_id": body.provider_request_id,
         "input_tokens": body.input_tokens, "output_tokens": body.output_tokens,
         "elapsed_ms": body.elapsed_ms,
         "request_payload_hash": body.request_payload_hash,
         "response_payload_hash": body.response_payload_hash}
        if isinstance(body, ChatAssistantLive) else {}
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="chat_message", entity_id=body.message_id,
        event_type=f"chat_assistant_{origin}", revision=1,
        payload={"session_id": str(session_id), "role": "assistant",
                 "origin": origin, "content_text": body.content_text,
                 **provenance},
    )
    if body.suggestion is not None:
        conn.execute(
            "INSERT INTO growth_chat_suggestions "
            "(account_id, suggestion_id, session_id, assistant_message_id, title, reason) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (account_id, body.suggestion.suggestion_id, session_id, body.message_id,
             body.suggestion.title, body.suggestion.reason),
        )
        seq = record_change(
            conn, account_id=account_id, trace_id=trace_id,
            entity_type="chat_suggestion", entity_id=body.suggestion.suggestion_id,
            event_type="chat_topic_suggested", revision=1,
            payload={"session_id": str(session_id), "title": body.suggestion.title,
                     "origin": origin, **provenance},
        )
    return 201, {"message_id": str(body.message_id),
                 "suggestion_id": str(body.suggestion.suggestion_id)
                 if body.suggestion else None, "change_seq": seq}


def _decide_chat_suggestion(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    session_id: uuid.UUID, suggestion_id: uuid.UUID, body: ChatSuggestionDecision,
) -> tuple[int, dict[str, Any]]:
    suggestion = conn.execute(
        "SELECT status, title, topic_id FROM growth_chat_suggestions "
        "WHERE account_id=%s AND session_id=%s AND suggestion_id=%s FOR UPDATE",
        (account_id, session_id, suggestion_id),
    ).fetchone()
    if suggestion is None:
        raise ApiError("SUGGESTION_NOT_FOUND", "主题建议不存在", 404)
    status = "confirmed" if body.decision == "confirm" else "rejected"
    if suggestion["status"] != "pending":
        if suggestion["status"] != status or suggestion["topic_id"] != body.topic_id:
            raise ApiError("DECISION_CONFLICT", "主题建议已有不同决定", 409)
        return 200, {"suggestion_id": str(suggestion_id), "status": status,
                     "topic_id": str(body.topic_id) if body.topic_id else None}
    if body.topic_id is not None:
        conn.execute(
            "INSERT INTO growth_topics(account_id, topic_id, title) VALUES (%s, %s, %s)",
            (account_id, body.topic_id, suggestion["title"]),
        )
        record_change(
            conn, account_id=account_id, trace_id=trace_id,
            entity_type="topic", entity_id=body.topic_id,
            event_type="topic_user_created", revision=1,
            payload={"topic_id": str(body.topic_id), "title": suggestion["title"],
                     "status": "active", "actor_origin": "user"},
        )
    conn.execute(
        "UPDATE growth_chat_suggestions SET status=%s, topic_id=%s, decided_at=now() "
        "WHERE account_id=%s AND suggestion_id=%s",
        (status, body.topic_id, account_id, suggestion_id),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="chat_suggestion", entity_id=suggestion_id,
        event_type=f"chat_topic_{status}", revision=2,
        payload={"session_id": str(session_id),
                 "topic_id": str(body.topic_id) if body.topic_id else None},
    )
    return 200, {"suggestion_id": str(suggestion_id), "status": status,
                 "topic_id": str(body.topic_id) if body.topic_id else None,
                 "change_seq": seq}


def _update_topic(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    topic_id: uuid.UUID, body: TopicUpdate,
) -> tuple[int, dict[str, Any]]:
    topic = conn.execute(
        "SELECT revision FROM growth_topics WHERE account_id=%s AND topic_id=%s "
        "AND deleted_at IS NULL FOR UPDATE", (account_id, topic_id),
    ).fetchone()
    if topic is None:
        raise ApiError("TOPIC_NOT_FOUND", "主题不存在", 404)
    alias = conn.execute(
        "SELECT 1 FROM growth_topic_aliases WHERE account_id=%s AND alias_topic_id=%s",
        (account_id, topic_id),
    ).fetchone()
    if alias is not None:
        raise ApiError("TOPIC_MERGED", "已合并主题请修改目标主题", 409)
    if body.base_revision != topic["revision"]:
        return 409, {"code": "REVISION_CONFLICT",
                     "server_revision": topic["revision"]}
    revision = int(topic["revision"]) + 1
    conn.execute(
        "UPDATE growth_topics SET title=%s, status=%s, revision=%s, updated_at=now() "
        "WHERE account_id=%s AND topic_id=%s",
        (body.title, body.status, revision, account_id, topic_id),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="topic", entity_id=topic_id,
        event_type="topic_user_updated", revision=revision,
        payload={"title": body.title, "status": body.status},
    )
    return 200, {"topic_id": str(topic_id), "revision": revision,
                 "status": body.status, "change_seq": seq}


def _merge_topic(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    source_id: uuid.UUID, body: TopicMerge,
) -> tuple[int, dict[str, Any]]:
    target_id = body.target_topic_id
    if source_id == target_id:
        raise ApiError("INVALID_TOPIC_MERGE", "不能把主题合并到自身", 422)
    topics = conn.execute(
        "SELECT topic_id, revision, status FROM growth_topics "
        "WHERE account_id=%s AND topic_id=ANY(%s) AND deleted_at IS NULL "
        "ORDER BY topic_id FOR UPDATE",
        (account_id, [source_id, target_id]),
    ).fetchall()
    by_id = {row["topic_id"]: row for row in topics}
    if source_id not in by_id or target_id not in by_id:
        raise ApiError("TOPIC_NOT_FOUND", "来源或目标主题不存在", 404)
    aliases = conn.execute(
        "SELECT alias_topic_id FROM growth_topic_aliases WHERE account_id=%s "
        "AND alias_topic_id=ANY(%s)",
        (account_id, [source_id, target_id]),
    ).fetchall()
    if aliases:
        raise ApiError("TOPIC_MERGED", "请使用尚未合并的正式主题", 409)
    source = by_id[source_id]
    target = by_id[target_id]
    if source["status"] != "active" or target["status"] != "active":
        raise ApiError("TOPIC_ARCHIVED", "归档主题须先激活再合并", 409)
    if (source["revision"] != body.source_base_revision
            or target["revision"] != body.target_base_revision):
        return 409, {"code": "REVISION_CONFLICT",
                     "source_revision": source["revision"],
                     "target_revision": target["revision"]}
    descendant = conn.execute(
        "WITH RECURSIVE descendants(topic_id) AS ("
        "SELECT topic_id FROM growth_topics WHERE account_id=%s "
        "AND parent_topic_id=%s UNION ALL "
        "SELECT child.topic_id FROM growth_topics child JOIN descendants parent "
        "ON child.parent_topic_id=parent.topic_id WHERE child.account_id=%s) "
        "SELECT 1 FROM descendants WHERE topic_id=%s LIMIT 1",
        (account_id, source_id, account_id, target_id),
    ).fetchone()
    if descendant is not None:
        raise ApiError("TOPIC_CYCLE", "不能把父主题合并到其后代", 409)
    moves: list[dict[str, Any]] = []
    for entity_type, table, key in (("task", "growth_tasks", "task_id"),
                                    ("note", "growth_notes", "note_id")):
        rows = conn.execute(
            sql.SQL("SELECT {}, revision FROM {} WHERE account_id=%s AND topic_id=%s "
                    "ORDER BY {} FOR UPDATE").format(
                sql.Identifier(key), sql.Identifier(table), sql.Identifier(key)),
            (account_id, source_id),
        ).fetchall()
        for row in rows:
            entity_id = row[key]
            move_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO growth_topic_moves "
                "(account_id, move_id, entity_type, entity_id, from_topic_id, "
                "to_topic_id, operation_id, action) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'merge')",
                (account_id, move_id, entity_type, entity_id, source_id, target_id,
                 body.operation_id),
            )
            record_change(
                conn, account_id=account_id, trace_id=trace_id,
                entity_type="topic_move", entity_id=move_id,
                event_type="topic_move_created", revision=1,
                payload={"entity_type": entity_type, "entity_id": str(entity_id),
                         "from_topic_id": str(source_id), "to_topic_id": str(target_id),
                         "action": "merge"},
            )
            if entity_type == "note":
                note = conn.execute(
                    "SELECT content_text, content_hash FROM growth_notes "
                    "WHERE account_id=%s AND note_id=%s", (account_id, entity_id),
                ).fetchone()
                assert note is not None
                conn.execute(
                    "INSERT INTO growth_note_revisions "
                    "(account_id, note_id, revision, content_text, content_hash) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (account_id, entity_id, int(row["revision"]) + 1,
                     note["content_text"], note["content_hash"]),
                )
                enqueue_projection(
                    conn, account_id=account_id, source_type="note",
                    source_id=entity_id, source_version=int(row["revision"]) + 1,
                    action="upsert",
                )
            conn.execute(
                sql.SQL("UPDATE {} SET topic_id=%s, revision=revision+1, "
                        "updated_at=now() WHERE account_id=%s AND {}=%s").format(
                    sql.Identifier(table), sql.Identifier(key)),
                (target_id, account_id, entity_id),
            )
            record_change(
                conn, account_id=account_id, trace_id=trace_id,
                entity_type=entity_type, entity_id=entity_id,
                event_type="topic_merged_association",
                revision=int(row["revision"]) + 1,
                payload={"from_topic_id": str(source_id),
                         "topic_id": str(target_id), "move_id": str(move_id)},
            )
            moves.append({"entity_type": entity_type, "entity_id": str(entity_id),
                          "move_id": str(move_id)})
    conn.execute(
        "UPDATE growth_topic_proposals SET topic_id=%s WHERE account_id=%s "
        "AND topic_id=%s", (target_id, account_id, source_id),
    )
    conn.execute(
        "UPDATE growth_chat_suggestions SET topic_id=%s WHERE account_id=%s "
        "AND topic_id=%s", (target_id, account_id, source_id),
    )
    copied_links = conn.execute(
        "INSERT INTO growth_topic_links "
        "(account_id, topic_id, entity_type, entity_id, source_topic_id, operation_id) "
        "SELECT account_id, %s, entity_type, entity_id, source_topic_id, operation_id "
        "FROM growth_topic_links WHERE account_id=%s AND topic_id=%s "
        "ON CONFLICT DO NOTHING RETURNING topic_id, entity_type, entity_id, source_topic_id",
        (target_id, account_id, source_id),
    ).fetchall()
    for link in copied_links:
        record_change(
            conn, account_id=account_id, trace_id=trace_id,
            entity_type="topic_link",
            entity_id=uuid.UUID(topic_link_sync_id(
                str(account_id), str(link["topic_id"]), link["entity_type"],
                str(link["entity_id"]),
            )),
            event_type="topic_link_copied", revision=1,
            payload={"topic_id": str(link["topic_id"]),
                     "entity_type": link["entity_type"],
                     "entity_id": str(link["entity_id"]),
                     "source_topic_id": str(link["source_topic_id"])},
        )
    removed_links = conn.execute(
        "DELETE FROM growth_topic_links WHERE account_id=%s AND topic_id=%s "
        "RETURNING topic_id, entity_type, entity_id",
        (account_id, source_id),
    ).fetchall()
    for link in removed_links:
        record_change(
            conn, account_id=account_id, trace_id=trace_id,
            entity_type="topic_link",
            entity_id=uuid.UUID(topic_link_sync_id(
                str(account_id), str(link["topic_id"]), link["entity_type"],
                str(link["entity_id"]),
            )),
            event_type="topic_link_removed", revision=2, payload={},
            deleted_at=datetime.now(UTC),
        )
    children = conn.execute(
        "UPDATE growth_topics SET parent_topic_id=%s, revision=revision+1, "
        "updated_at=now() WHERE account_id=%s AND parent_topic_id=%s "
        "RETURNING topic_id, revision",
        (target_id, account_id, source_id),
    ).fetchall()
    for child in children:
        record_change(
            conn, account_id=account_id, trace_id=trace_id,
            entity_type="topic", entity_id=child["topic_id"],
            event_type="topic_parent_moved", revision=child["revision"],
            payload={"parent_topic_id": str(target_id)},
        )
    redirected_aliases = conn.execute(
        "UPDATE growth_topic_aliases SET canonical_topic_id=%s "
        "WHERE account_id=%s AND canonical_topic_id=%s "
        "RETURNING alias_topic_id",
        (target_id, account_id, source_id),
    ).fetchall()
    for alias in redirected_aliases:
        record_change(
            conn, account_id=account_id, trace_id=trace_id,
            entity_type="topic_alias", entity_id=alias["alias_topic_id"],
            event_type="topic_alias_redirected", revision=2,
            payload={"canonical_topic_id": str(target_id)},
        )
    conn.execute(
        "INSERT INTO growth_topic_aliases "
        "(account_id, alias_topic_id, canonical_topic_id, operation_id) "
        "VALUES (%s, %s, %s, %s)",
        (account_id, source_id, target_id, body.operation_id),
    )
    record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="topic_alias", entity_id=source_id,
        event_type="topic_alias_created", revision=1,
        payload={"canonical_topic_id": str(target_id)},
    )
    conn.execute(
        "UPDATE growth_topics SET status='archived', revision=revision+1, "
        "updated_at=now() WHERE account_id=%s AND topic_id=%s",
        (account_id, source_id),
    )
    conn.execute(
        "UPDATE growth_topics SET revision=revision+1, updated_at=now() "
        "WHERE account_id=%s AND topic_id=%s", (account_id, target_id),
    )
    record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="topic", entity_id=source_id,
        event_type="topic_merged_alias", revision=int(source["revision"]) + 1,
        payload={"canonical_topic_id": str(target_id), "moves": moves},
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="topic", entity_id=target_id,
        event_type="topic_merge_received", revision=int(target["revision"]) + 1,
        payload={"alias_topic_id": str(source_id), "moves": moves},
    )
    return 200, {"source_topic_id": str(source_id),
                 "target_topic_id": str(target_id), "moved": moves,
                 "source_revision": int(source["revision"]) + 1,
                 "target_revision": int(target["revision"]) + 1,
                 "change_seq": seq}


def _split_topic(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    parent_id: uuid.UUID, body: TopicSplit,
) -> tuple[int, dict[str, Any]]:
    parent = conn.execute(
        "SELECT revision, status FROM growth_topics WHERE account_id=%s "
        "AND topic_id=%s AND deleted_at IS NULL FOR UPDATE", (account_id, parent_id),
    ).fetchone()
    if parent is None:
        raise ApiError("TOPIC_NOT_FOUND", "父主题不存在", 404)
    if parent["status"] != "active":
        raise ApiError("TOPIC_ARCHIVED", "归档主题不能拆分", 409)
    if conn.execute("SELECT 1 FROM growth_topic_aliases WHERE account_id=%s "
                    "AND alias_topic_id=%s", (account_id, parent_id)).fetchone():
        raise ApiError("TOPIC_MERGED", "已合并主题不能拆分", 409)
    if body.base_revision != parent["revision"]:
        return 409, {"code": "REVISION_CONFLICT",
                     "server_revision": parent["revision"]}
    all_notes = {note_id for child in body.children for note_id in child.note_ids}
    all_tasks = {task_id for child in body.children for task_id in child.task_ids}
    for entity_type, table, key, ids in (
        ("note", "growth_notes", "note_id", all_notes),
        ("task", "growth_tasks", "task_id", all_tasks),
    ):
        if not ids:
            continue
        rows = conn.execute(
            sql.SQL("SELECT {} FROM {} WHERE account_id=%s AND topic_id=%s "
                    "AND deleted_at IS NULL AND {}=ANY(%s)").format(
                sql.Identifier(key), sql.Identifier(table), sql.Identifier(key)),
            (account_id, parent_id, list(ids)),
        ).fetchall()
        if {row[key] for row in rows} != ids:
            raise ApiError("INVALID_TOPIC_ASSIGNMENT",
                           f"{entity_type} 不属于当前父主题", 409)
    children_response: list[dict[str, Any]] = []
    for child in body.children:
        conn.execute(
            "INSERT INTO growth_topics "
            "(account_id, topic_id, title, parent_topic_id) VALUES (%s, %s, %s, %s)",
            (account_id, child.topic_id, child.title, parent_id),
        )
        for entity_type, ids in (("note", child.note_ids), ("task", child.task_ids)):
            for entity_id in ids:
                conn.execute(
                    "INSERT INTO growth_topic_links "
                    "(account_id, topic_id, entity_type, entity_id, "
                    "source_topic_id, operation_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (account_id, child.topic_id, entity_type, entity_id,
                     parent_id, body.operation_id),
                )
                record_change(
                    conn, account_id=account_id, trace_id=trace_id,
                    entity_type="topic_link",
                    entity_id=uuid.UUID(topic_link_sync_id(
                        str(account_id), str(child.topic_id), entity_type,
                        str(entity_id),
                    )),
                    event_type="topic_split_link_created", revision=1,
                    payload={"topic_id": str(child.topic_id),
                             "entity_type": entity_type, "entity_id": str(entity_id),
                             "source_topic_id": str(parent_id)},
                )
                move_id = uuid.uuid4()
                conn.execute(
                    "INSERT INTO growth_topic_moves "
                    "(account_id, move_id, entity_type, entity_id, from_topic_id, "
                    "to_topic_id, operation_id, action) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'split_link')",
                    (account_id, move_id, entity_type, entity_id,
                     parent_id, child.topic_id, body.operation_id),
                )
                record_change(
                    conn, account_id=account_id, trace_id=trace_id,
                    entity_type="topic_move", entity_id=move_id,
                    event_type="topic_move_created", revision=1,
                    payload={"entity_type": entity_type, "entity_id": str(entity_id),
                             "from_topic_id": str(parent_id),
                             "to_topic_id": str(child.topic_id),
                             "action": "split_link"},
                )
        record_change(
            conn, account_id=account_id, trace_id=trace_id,
            entity_type="topic", entity_id=child.topic_id,
            event_type="topic_split_child_created", revision=1,
            payload={"parent_topic_id": str(parent_id), "title": child.title,
                     "note_ids": [str(value) for value in child.note_ids],
                     "task_ids": [str(value) for value in child.task_ids]},
        )
        children_response.append({"topic_id": str(child.topic_id),
                                  "title": child.title})
    revision = int(parent["revision"]) + 1
    conn.execute(
        "UPDATE growth_topics SET revision=%s, updated_at=now() "
        "WHERE account_id=%s AND topic_id=%s", (revision, account_id, parent_id),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="topic", entity_id=parent_id,
        event_type="topic_user_split", revision=revision,
        payload={"children": children_response},
    )
    return 201, {"parent_topic_id": str(parent_id), "revision": revision,
                 "children": children_response, "change_seq": seq}


def _create_development_event(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    body: DevelopmentEvent,
) -> tuple[int, dict[str, Any]]:
    canonical = body.model_dump(mode="json", exclude={"operation_id"})
    digest = payload_hash("development_event", canonical)
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"development_event:{account_id}:{body.event_id}",),
    )
    existing = conn.execute(
        "SELECT payload_hash, response FROM growth_development_events "
        "WHERE account_id=%s AND event_id=%s FOR UPDATE",
        (account_id, body.event_id),
    ).fetchone()
    if existing is not None:
        if existing["payload_hash"].strip() != digest:
            raise ApiError("EVENT_ID_REUSED", "同一事件 ID 对应不同内容", 409)
        return 202, existing["response"]
    project = conn.execute(
        "SELECT sync_policy FROM growth_projects WHERE account_id=%s "
        "AND project_id=%s AND deleted_at IS NULL",
        (account_id, body.project_id),
    ).fetchone()
    if project is None:
        raise ApiError("PROJECT_NOT_FOUND", "开发事件项目不存在", 404)
    if not project["sync_policy"].get("cloud_allowed", False):
        raise ApiError("CLOUD_DISABLED", "项目未开启云同步", 409)
    if body.feature_id is not None:
        feature = conn.execute(
            "SELECT 1 FROM growth_features WHERE account_id=%s AND project_id=%s "
            "AND feature_id=%s AND deleted_at IS NULL",
            (account_id, body.project_id, body.feature_id),
        ).fetchone()
        if feature is None:
            raise ApiError("FEATURE_NOT_FOUND", "事件 feature 不属于项目", 404)
    if body.target_device_id is not None:
        device = conn.execute(
            "SELECT revoked_at FROM growth_devices WHERE account_id=%s AND device_id=%s",
            (account_id, body.target_device_id),
        ).fetchone()
        if device is None or device["revoked_at"] is not None:
            raise ApiError("DEVICE_REVOKED", "目标设备不存在或已撤销", 409)
    status = "targeted" if body.target_device_id else "pending_confirmation"
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="development_event", entity_id=body.event_id,
        event_type="development_event_received", revision=1,
        payload={"event_id": str(body.event_id), "project_id": str(body.project_id),
                 "feature_id": str(body.feature_id) if body.feature_id else None,
                 "target_device_id": str(body.target_device_id)
                 if body.target_device_id else None,
                 "expected_head_sha": body.expected_head_sha,
                 "status": status},
    )
    response = {"event_id": str(body.event_id), "status": status,
                "target_device_id": str(body.target_device_id)
                if body.target_device_id else None, "change_seq": seq}
    conn.execute(
        "INSERT INTO growth_development_events "
        "(account_id, event_id, schema_version, project_id, feature_id, event_type, "
        "occurred_at, source, target_device_id, expected_head_sha, status, "
        "payload_hash, response) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (account_id, body.event_id, body.schema_version, body.project_id,
         body.feature_id, body.type, body.occurred_at, body.source,
         body.target_device_id, body.expected_head_sha, status, digest, Jsonb(response)),
    )
    return 202, response


def _claim_development_event(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    device_id: uuid.UUID, event_id: uuid.UUID, body: DevelopmentEventClaim,
) -> tuple[int, dict[str, Any]]:
    event = conn.execute(
        "SELECT status, target_device_id, expected_head_sha "
        "FROM growth_development_events WHERE account_id=%s AND event_id=%s "
        "FOR UPDATE", (account_id, event_id),
    ).fetchone()
    if event is None:
        raise ApiError("EVENT_NOT_FOUND", "开发事件不存在", 404)
    if event["expected_head_sha"].strip() != body.expected_head_sha:
        return 409, {"code": "HEAD_MISMATCH", "event_id": str(event_id)}
    if event["target_device_id"] is not None:
        if event["target_device_id"] != device_id:
            raise ApiError("EVENT_ALREADY_TARGETED", "事件已指定其他设备", 409)
        return 200, {"event_id": str(event_id), "status": "targeted",
                     "target_device_id": str(device_id)}
    conn.execute(
        "UPDATE growth_development_events SET target_device_id=%s, status='targeted', "
        "updated_at=now() WHERE account_id=%s AND event_id=%s",
        (device_id, account_id, event_id),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="development_event", entity_id=event_id,
        event_type="development_event_target_confirmed", revision=2,
        payload={"event_id": str(event_id), "target_device_id": str(device_id),
                 "expected_head_sha": body.expected_head_sha,
                 "status": "targeted"},
    )
    return 200, {"event_id": str(event_id), "status": "targeted",
                 "target_device_id": str(device_id), "change_seq": seq}


def _create_feature(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    body: FeatureCreate,
) -> tuple[int, dict[str, Any]]:
    project = conn.execute(
        "SELECT 1 FROM growth_projects WHERE account_id=%s AND project_id=%s "
        "AND deleted_at IS NULL",
        (account_id, body.project_id),
    ).fetchone()
    if project is None:
        raise ApiError("PROJECT_NOT_FOUND", "项目不存在", 404)
    conn.execute(
        "INSERT INTO growth_features "
        "(account_id, project_id, feature_id, label, base_ref, start_base_sha) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            account_id,
            body.project_id,
            body.feature_id,
            body.label,
            body.base_ref,
            body.start_base_sha,
        ),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="feature",
        entity_id=body.feature_id,
        event_type="feature_started",
        revision=1,
        payload={
            "feature_id": str(body.feature_id),
            "project_id": str(body.project_id),
            "label": body.label,
            "base_ref": body.base_ref,
            "start_base_sha": body.start_base_sha,
        },
    )
    return 201, {"feature_id": str(body.feature_id), "revision": 1, "change_seq": seq}


def _map_module(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    body: ModuleMap,
) -> tuple[int, dict[str, Any]]:
    snapshot = conn.execute(
        "SELECT project_id, effective_tree_hash FROM growth_snapshots "
        "WHERE account_id=%s AND snapshot_id=%s AND deleted_at IS NULL",
        (account_id, body.snapshot_id),
    ).fetchone()
    if snapshot is None or snapshot["project_id"] != body.project_id:
        raise ApiError("SNAPSHOT_NOT_FOUND", "模块快照不属于当前项目", 404)
    if snapshot["effective_tree_hash"].strip() != body.effective_tree_hash:
        raise ApiError("STALE_INDEX", "索引源码指纹与快照不一致", 409)
    current = conn.execute(
        "SELECT * FROM growth_modules WHERE account_id=%s AND module_id=%s FOR UPDATE",
        (account_id, body.module_id),
    ).fetchone()
    flow = body.flow.model_dump(mode="json", by_alias=True)
    members = sorted(body.member_paths)
    if current is None:
        if body.base_revision is not None:
            return 409, {"code": "REVISION_CONFLICT", "server_revision": None}
        conn.execute(
            "INSERT INTO growth_modules "
            "(account_id, project_id, module_id, snapshot_id, index_revision, "
            "effective_tree_hash, name, member_paths, flow) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (account_id, body.project_id, body.module_id, body.snapshot_id,
             body.index_revision, body.effective_tree_hash, body.suggested_name,
             Jsonb(members), Jsonb(flow)),
        )
        revision = 1
        event_type = "module_mapped"
        pending = False
    else:
        if current["project_id"] != body.project_id:
            raise ApiError("MODULE_PROJECT_MISMATCH", "模块不属于当前项目", 409)
        if body.base_revision != current["revision"]:
            return 409, {"code": "REVISION_CONFLICT",
                         "server_revision": current["revision"]}
        revision = int(current["revision"]) + 1
        pending = bool(current["locked"] and members != sorted(current["member_paths"]))
        if pending:
            conn.execute(
                "UPDATE growth_modules SET pending_adjustment=%s, revision=%s, "
                "updated_at=now() WHERE account_id=%s AND module_id=%s",
                (Jsonb({"snapshot_id": str(body.snapshot_id),
                        "index_revision": body.index_revision,
                        "effective_tree_hash": body.effective_tree_hash,
                        "member_paths": members, "flow": flow}),
                 revision, account_id, body.module_id),
            )
            event_type = "module_adjustment_proposed"
        else:
            conn.execute(
                "UPDATE growth_modules SET snapshot_id=%s, index_revision=%s, "
                "effective_tree_hash=%s, member_paths=%s, flow=%s, "
                "pending_adjustment=NULL, revision=%s, updated_at=now() "
                "WHERE account_id=%s AND module_id=%s",
                (body.snapshot_id, body.index_revision, body.effective_tree_hash,
                 Jsonb(members), Jsonb(flow), revision, account_id, body.module_id),
            )
            event_type = "module_reindexed"
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="module", entity_id=body.module_id, event_type=event_type,
        revision=revision,
        payload={"module_id": str(body.module_id), "project_id": str(body.project_id),
                 "snapshot_id": str(body.snapshot_id),
                 "index_revision": body.index_revision, "pending_adjustment": pending},
    )
    return (201 if revision == 1 else 200), {
        "module_id": str(body.module_id), "revision": revision,
        "pending_adjustment": pending, "change_seq": seq,
    }


def _edit_module(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    module_id: uuid.UUID, body: ModuleEdit,
) -> tuple[int, dict[str, Any]]:
    current = conn.execute(
        "SELECT name, member_paths, flow, revision, pending_adjustment "
        "FROM growth_modules WHERE account_id=%s AND module_id=%s FOR UPDATE",
        (account_id, module_id),
    ).fetchone()
    if current is None:
        raise ApiError("MODULE_NOT_FOUND", "模块不存在", 404)
    if body.base_revision != current["revision"]:
        return 409, {"code": "REVISION_CONFLICT",
                     "server_revision": current["revision"]}
    members = sorted(body.member_paths)
    pending = current["pending_adjustment"]
    if body.resolve_pending and pending is None:
        raise ApiError("NO_PENDING_ADJUSTMENT", "模块没有待确认的边界变化", 409)
    available = pending if body.resolve_pending else current
    if not set(members) <= set(available["member_paths"]):
        raise ApiError("INVALID_MODULE_MEMBER", "模块成员不属于当前索引候选", 422)
    kept = {node["symbol"] for node in available["flow"]["nodes"]
            if node["file_path"] in members}
    flow = {**available["flow"],
            "nodes": [node for node in available["flow"]["nodes"]
                      if node["symbol"] in kept],
            "edges": [edge for edge in available["flow"]["edges"]
                      if edge["from"] in kept and edge["to"] in kept]}
    if not flow["nodes"]:
        raise ApiError("EMPTY_MODULE_FLOW", "模块至少保留一个有源码依据的节点", 422)
    revision = int(current["revision"]) + 1
    conn.execute(
        "UPDATE growth_modules SET name=%s, member_paths=%s, flow=%s, locked=%s, "
        "snapshot_id=CASE WHEN %s THEN %s ELSE snapshot_id END, "
        "index_revision=CASE WHEN %s THEN %s ELSE index_revision END, "
        "effective_tree_hash=CASE WHEN %s THEN %s ELSE effective_tree_hash END, "
        "pending_adjustment=CASE WHEN %s THEN NULL ELSE pending_adjustment END, "
        "revision=%s, updated_at=now() WHERE account_id=%s AND module_id=%s",
        (body.name, Jsonb(members), Jsonb(flow), body.locked,
         body.resolve_pending, available.get("snapshot_id"),
         body.resolve_pending, available.get("index_revision"),
         body.resolve_pending, available.get("effective_tree_hash"),
         body.resolve_pending, revision,
         account_id, module_id),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="module", entity_id=module_id, event_type="module_user_edited",
        revision=revision,
        payload={"module_id": str(module_id), "name": body.name,
                 "locked": body.locked, "member_paths": members,
                 "resolved_pending": body.resolve_pending},
    )
    return 200, {"module_id": str(module_id), "revision": revision,
                 "name": body.name, "locked": body.locked,
                 "pending_adjustment": bool(pending and not body.resolve_pending),
                 "change_seq": seq}


def _save_snapshot(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    body: SnapshotCreate,
) -> tuple[int, dict[str, Any]]:
    feature = conn.execute(
        "SELECT 1 FROM growth_features WHERE account_id=%s AND project_id=%s "
        "AND feature_id=%s AND deleted_at IS NULL",
        (account_id, body.project_id, body.feature_id),
    ).fetchone()
    if feature is None:
        raise ApiError("FEATURE_NOT_FOUND", "feature 不属于当前项目或账号", 404)
    allowed_refs = {source.source_ref_id for source in body.source_refs}
    if len(allowed_refs) != len(body.source_refs):
        raise ApiError("DUPLICATE_SOURCE_REF", "同一快照不能重复提交源码引用", 422)
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{account_id}:snapshot:{body.snapshot_id}",),
    )
    snapshot = conn.execute(
        "SELECT project_id, feature_id, resolved_base_sha, head_sha, "
        "effective_tree_hash, diff_hash, capture_scope, privacy_policy_version, deleted_at "
        "FROM growth_snapshots WHERE account_id=%s AND snapshot_id=%s FOR UPDATE",
        (account_id, body.snapshot_id),
    ).fetchone()
    if snapshot is not None and snapshot["deleted_at"] is not None:
        raise ApiError("ENTITY_DELETED", "快照已删除，不能重新分析", 410)
    if snapshot is None:
        conn.execute(
            "INSERT INTO growth_snapshots "
            "(account_id, project_id, feature_id, snapshot_id, resolved_base_sha, "
            "head_sha, effective_tree_hash, diff_hash, capture_scope, privacy_policy_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                account_id,
                body.project_id,
                body.feature_id,
                body.snapshot_id,
                body.resolved_base_sha,
                body.head_sha,
                body.effective_tree_hash,
                body.diff_hash,
                Jsonb(body.capture_scope.model_dump(exclude_none=True)),
                body.privacy_policy_version,
            ),
        )
        record_change(
            conn,
            account_id=account_id,
            trace_id=trace_id,
            entity_type="snapshot",
            entity_id=body.snapshot_id,
            event_type="snapshot_saved",
            revision=1,
            payload={
                "snapshot_id": str(body.snapshot_id),
                "feature_id": str(body.feature_id),
                "resolved_base_sha": body.resolved_base_sha,
                "head_sha": body.head_sha,
                "effective_tree_hash": body.effective_tree_hash,
                "diff_hash": body.diff_hash,
            },
        )
    elif (
        snapshot["project_id"] != body.project_id
        or snapshot["feature_id"] != body.feature_id
        or snapshot["resolved_base_sha"].strip() != body.resolved_base_sha
        or snapshot["head_sha"].strip() != body.head_sha
        or snapshot["effective_tree_hash"].strip() != body.effective_tree_hash
        or snapshot["diff_hash"].strip() != body.diff_hash
        or snapshot["capture_scope"] != body.capture_scope.model_dump(exclude_none=True)
        or snapshot["privacy_policy_version"] != body.privacy_policy_version
    ):
        raise ApiError("SNAPSHOT_MISMATCH", "同一快照 ID 的来源版本不能改变", 409)
    existing_refs = {
        row["source_ref_id"]: row
        for row in conn.execute(
            "SELECT source_ref_id, relative_path, blob_hash FROM growth_source_refs "
            "WHERE account_id=%s AND snapshot_id=%s",
            (account_id, body.snapshot_id),
        ).fetchall()
    }
    refs_by_path = {row["relative_path"]: row for row in existing_refs.values()}
    seen_paths: set[str] = set()
    for source in body.source_refs:
        if source.relative_path in seen_paths:
            raise ApiError("DUPLICATE_SOURCE_PATH", "同一快照不能重复提交源码路径", 422)
        seen_paths.add(source.relative_path)
        existing_source = existing_refs.get(source.source_ref_id)
        if existing_source is not None:
            if (
                existing_source["relative_path"] != source.relative_path
                or existing_source["blob_hash"].strip() != source.blob_hash
            ):
                raise ApiError("SOURCE_REF_MISMATCH", "已保存的源码引用不能改写", 409)
            continue
        if source.relative_path in refs_by_path:
            raise ApiError("SOURCE_REF_MISMATCH", "已保存的源码路径不能改绑引用 ID", 409)
        reused = conn.execute(
            "SELECT snapshot_id FROM growth_source_refs "
            "WHERE account_id=%s AND source_ref_id=%s",
            (account_id, source.source_ref_id),
        ).fetchone()
        if reused is not None:
            raise ApiError("SOURCE_REF_MISMATCH", "源码引用 ID 已属于其他快照", 409)
        conn.execute(
            "INSERT INTO growth_source_refs "
            "(account_id, snapshot_id, source_ref_id, relative_path, blob_hash) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                account_id,
                body.snapshot_id,
                source.source_ref_id,
                source.relative_path,
                source.blob_hash,
            ),
        )
        record_change(
            conn,
            account_id=account_id,
            trace_id=trace_id,
            entity_type="source_ref",
            entity_id=source.source_ref_id,
            event_type="source_ref_saved",
            revision=1,
            payload={
                "source_ref_id": str(source.source_ref_id),
                "snapshot_id": str(body.snapshot_id),
                "relative_path": source.relative_path,
                "blob_hash": source.blob_hash,
            },
        )
    return 201, {"snapshot_id": str(body.snapshot_id), "revision": 1}


def _save_source_excerpt(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    body: SourceExcerptCreate,
) -> tuple[int, dict[str, Any]]:
    """Store only a source slice that was independently approved for cloud sync."""
    source = conn.execute(
        "SELECT refs.snapshot_id, refs.relative_path, refs.blob_hash, refs.revision, "
        "projects.sync_policy FROM growth_source_refs refs "
        "JOIN growth_snapshots snapshots ON snapshots.account_id=refs.account_id "
        "AND snapshots.snapshot_id=refs.snapshot_id "
        "JOIN growth_projects projects ON projects.account_id=snapshots.account_id "
        "AND projects.project_id=snapshots.project_id "
        "WHERE refs.account_id=%s AND refs.source_ref_id=%s "
        "AND refs.deleted_at IS NULL AND snapshots.deleted_at IS NULL "
        "AND projects.deleted_at IS NULL FOR UPDATE OF refs",
        (account_id, body.source_ref_id),
    ).fetchone()
    if source is None or source["snapshot_id"] != body.snapshot_id:
        raise ApiError("SOURCE_REF_NOT_FOUND", "源码引用不属于当前账号或快照", 404)
    if not source["sync_policy"].get("cloud_allowed", False):
        raise ApiError("CLOUD_DISABLED", "此项目没有启用云同步", 409)
    text = body.content_text
    if (hashlib.sha256(text.encode("utf-8")).hexdigest() != body.content_hash
            or "\x00" in text or scan_text_for_secrets(text, source_name="approved_excerpt")
            or re.search(r"(?i)(?:[a-z]:[\\/]|/(?:home|users)/[^/\s]+)", text)):
        raise ApiError("UNSAFE_SOURCE_EXCERPT", "批准片段哈希不符或包含敏感内容", 422)
    existing = conn.execute(
        "SELECT excerpt_id, snapshot_id, start_line, end_line, content_hash "
        "FROM growth_source_excerpts WHERE account_id=%s AND source_ref_id=%s",
        (account_id, body.source_ref_id),
    ).fetchone()
    if existing is not None:
        if (existing["excerpt_id"] != body.excerpt_id
                or existing["snapshot_id"] != body.snapshot_id
                or existing["start_line"] != body.start_line
                or existing["end_line"] != body.end_line
                or existing["content_hash"].strip() != body.content_hash):
            raise ApiError("SOURCE_EXCERPT_MISMATCH", "已批准的片段不能静默替换", 409)
        return 200, {"excerpt_id": str(body.excerpt_id), "source_ref_id": str(body.source_ref_id),
                     "revision": 1, "reused": True}
    conn.execute(
        "INSERT INTO growth_source_excerpts "
        "(account_id,source_ref_id,excerpt_id,snapshot_id,blob_hash,start_line,end_line,"
        "content_text,content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (account_id, body.source_ref_id, body.excerpt_id, body.snapshot_id,
         source["blob_hash"], body.start_line, body.end_line, text, body.content_hash),
    )
    source_revision = int(source["revision"]) + 1
    conn.execute(
        "UPDATE growth_source_refs SET approved_excerpt_id=%s, revision=%s, "
        "updated_at=now() WHERE account_id=%s AND source_ref_id=%s",
        (body.excerpt_id, source_revision, account_id, body.source_ref_id),
    )
    record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="source_ref", entity_id=body.source_ref_id,
        event_type="source_ref_excerpt_linked", revision=source_revision,
        payload={"source_ref_id": str(body.source_ref_id),
                 "snapshot_id": str(body.snapshot_id),
                 "relative_path": source["relative_path"],
                 "blob_hash": source["blob_hash"].strip(),
                 "approved_excerpt_id": str(body.excerpt_id)},
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="source_excerpt", entity_id=body.source_ref_id,
        event_type="source_excerpt_approved", revision=1,
        payload={"excerpt_id": str(body.excerpt_id),
                 "source_ref_id": str(body.source_ref_id),
                 "snapshot_id": str(body.snapshot_id),
                 "relative_path": source["relative_path"],
                 "blob_hash": source["blob_hash"].strip(),
                 "start_line": body.start_line, "end_line": body.end_line,
                 "content_text": text, "content_hash": body.content_hash},
    )
    return 201, {"excerpt_id": str(body.excerpt_id),
                 "source_ref_id": str(body.source_ref_id), "revision": 1,
                 "change_seq": seq}


def _save_analysis_result(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    body: AnalysisResult,
) -> tuple[int, dict[str, Any]]:
    allowed_refs = {source.source_ref_id for source in body.source_refs}
    for opportunity in body.opportunities:
        if not set(opportunity.source_refs) <= allowed_refs:
            raise ApiError("INVALID_SOURCE_REF", "机会引用不属于本快照", 422)
    _save_snapshot(conn, account_id, trace_id, body)
    conn.execute(
        "INSERT INTO growth_analysis_runs "
        "(account_id, snapshot_id, analysis_id, input_fingerprint, mode, status, upstream_run_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            account_id,
            body.snapshot_id,
            body.analysis_id,
            body.input_fingerprint,
            body.mode,
            body.status,
            body.upstream_run_id,
        ),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="analysis",
        entity_id=body.analysis_id,
        event_type="analysis_result_saved",
        revision=1,
        payload={
            "analysis_id": str(body.analysis_id),
            "snapshot_id": str(body.snapshot_id),
            "mode": body.mode,
            "status": body.status,
            "upstream_run_id": body.upstream_run_id,
        },
    )
    output_payload = [item.model_dump(mode="json") for item in body.opportunities]
    output_hash = hashlib.sha256(
        json.dumps(output_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    conn.execute(
        "INSERT INTO growth_opportunity_batches "
        "(account_id, analysis_id, output_hash, origin, provider_name, model_name, "
        "provider_request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            account_id,
            body.analysis_id,
            output_hash,
            body.generation_origin,
            body.provider_name,
            body.model_name,
            body.provider_request_id,
        ),
    )
    for ordinal, opportunity in enumerate(body.opportunities):
        conn.execute(
            "INSERT INTO growth_opportunities "
            "(account_id, opportunity_id, analysis_id, ordinal, title, reason, learning_goal, "
            "source_refs, estimated_minutes, uncertainties) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                account_id,
                opportunity.opportunity_id,
                body.analysis_id,
                ordinal,
                opportunity.title,
                opportunity.reason,
                opportunity.learning_goal,
                Jsonb([str(ref) for ref in opportunity.source_refs]),
                opportunity.estimated_minutes,
                Jsonb(opportunity.uncertainties),
            ),
        )
        seq = record_change(
            conn,
            account_id=account_id,
            trace_id=trace_id,
            entity_type="opportunity",
            entity_id=opportunity.opportunity_id,
            event_type="opportunity_saved",
            revision=1,
            payload={"analysis_id": str(body.analysis_id), "title": opportunity.title},
        )
        for proposal_ordinal, proposal in enumerate(opportunity.topic_proposals):
            conn.execute(
                "INSERT INTO growth_topic_proposals "
                "(account_id, proposal_id, opportunity_id, ordinal, title) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    account_id,
                    proposal.proposal_id,
                    opportunity.opportunity_id,
                    proposal_ordinal,
                    proposal.title,
                ),
            )
            seq = record_change(
                conn,
                account_id=account_id,
                trace_id=trace_id,
                entity_type="topic_proposal",
                entity_id=proposal.proposal_id,
                event_type="topic_proposed",
                revision=1,
                payload={
                    "opportunity_id": str(opportunity.opportunity_id),
                    "title": proposal.title,
                },
            )
    return 201, {
        "snapshot_id": str(body.snapshot_id),
        "analysis_id": str(body.analysis_id),
        "revision": 1,
        "change_seq": seq,
    }


def _decide_topic(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    proposal_id: uuid.UUID,
    body: TopicDecision,
) -> tuple[int, dict[str, Any]]:
    row = conn.execute(
        "SELECT title, status, topic_id FROM growth_topic_proposals "
        "WHERE account_id=%s AND proposal_id=%s FOR UPDATE",
        (account_id, proposal_id),
    ).fetchone()
    if row is None:
        raise ApiError("PROPOSAL_NOT_FOUND", "主题建议不存在", 404)
    expected = "confirmed" if body.decision == "confirm" else "rejected"
    if row["status"] != "pending":
        previous = conn.execute(
            "SELECT payload FROM growth_change_feed WHERE account_id=%s "
            "AND entity_type='topic_proposal' AND entity_id=%s "
            "AND event_type='topic_confirmed' ORDER BY change_seq DESC LIMIT 1",
            (account_id, proposal_id),
        ).fetchone()
        reused = bool(previous["payload"].get("reuse_existing", False)) if previous else False
        if (row["status"] != expected or row["topic_id"] != body.topic_id
                or (body.decision == "confirm" and reused != body.reuse_existing)):
            raise ApiError("DECISION_CONFLICT", "主题建议已有不同决定", 409)
        return 200, {
            "proposal_id": str(proposal_id),
            "topic_id": str(body.topic_id) if body.topic_id else None,
            "status": expected,
        }
    if body.topic_id:
        if body.reuse_existing:
            topic = conn.execute(
                "SELECT status FROM growth_topics WHERE account_id=%s AND topic_id=%s "
                "AND deleted_at IS NULL",
                (account_id, body.topic_id),
            ).fetchone()
            alias = conn.execute(
                "SELECT 1 FROM growth_topic_aliases WHERE account_id=%s "
                "AND alias_topic_id=%s",
                (account_id, body.topic_id),
            ).fetchone()
            if topic is None or topic["status"] != "active" or alias is not None:
                raise ApiError("TOPIC_NOT_ACTIVE", "已有主题不存在、已归档或已合并", 409)
        else:
            conn.execute(
                "INSERT INTO growth_topics(account_id, topic_id, title) VALUES (%s, %s, %s)",
                (account_id, body.topic_id, row["title"]),
            )
            record_change(
                conn, account_id=account_id, trace_id=trace_id,
                entity_type="topic", entity_id=body.topic_id,
                event_type="topic_user_created", revision=1,
                payload={"topic_id": str(body.topic_id), "title": row["title"],
                         "status": "active", "actor_origin": "user"},
            )
    conn.execute(
        "UPDATE growth_topic_proposals SET status=%s, topic_id=%s, decided_at=now() "
        "WHERE account_id=%s AND proposal_id=%s",
        (expected, body.topic_id, account_id, proposal_id),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="topic_proposal",
        entity_id=proposal_id,
        event_type=f"topic_{expected}",
        revision=2,
        payload={"topic_id": str(body.topic_id) if body.topic_id else None,
                 "reuse_existing": body.reuse_existing},
    )
    return 200, {
        "proposal_id": str(proposal_id),
        "topic_id": str(body.topic_id) if body.topic_id else None,
        "status": expected,
        "change_seq": seq,
    }


def _create_task(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    body: TaskCreate,
) -> tuple[int, dict[str, Any]]:
    opportunity = conn.execute(
        "SELECT growth_opportunities.source_refs, growth_opportunities.estimated_minutes, "
        "growth_opportunities.learning_goal, "
        "growth_analysis_runs.snapshot_id FROM growth_opportunities "
        "JOIN growth_analysis_runs ON "
        "growth_analysis_runs.account_id=growth_opportunities.account_id "
        "AND growth_analysis_runs.analysis_id=growth_opportunities.analysis_id "
        "WHERE growth_opportunities.account_id=%s "
        "AND growth_opportunities.opportunity_id=%s",
        (account_id, body.opportunity_id),
    ).fetchone()
    if opportunity is None:
        raise ApiError("OPPORTUNITY_NOT_FOUND", "机会不存在", 404)
    confirmed = conn.execute(
        "SELECT 1 FROM growth_topic_proposals "
        "WHERE account_id=%s AND opportunity_id=%s AND topic_id=%s AND status='confirmed'",
        (account_id, body.opportunity_id, body.topic_id),
    ).fetchone()
    if confirmed is None:
        raise ApiError("TOPIC_NOT_CONFIRMED", "主题尚未由用户确认", 409)
    if body.module_id is not None:
        module = conn.execute(
            "SELECT snapshot_id, index_revision, member_paths FROM growth_modules "
            "WHERE account_id=%s AND module_id=%s",
            (account_id, body.module_id),
        ).fetchone()
        if module is None:
            raise ApiError("MODULE_NOT_FOUND", "模块不存在", 404)
        if (module["snapshot_id"] != opportunity["snapshot_id"]
                or module["index_revision"].strip() != body.module_index_revision):
            raise ApiError("STALE_MODULE", "卡片机会与模块索引不是同一源码版本", 409)
        refs = conn.execute(
            "SELECT source_ref_id, relative_path FROM growth_source_refs "
            "WHERE account_id=%s AND snapshot_id=%s",
            (account_id, module["snapshot_id"]),
        ).fetchall()
        grounded = {str(row["source_ref_id"]) for row in refs
                    if row["relative_path"] in module["member_paths"]}
        if not set(opportunity["source_refs"]) & grounded:
            raise ApiError("MODULE_SOURCE_MISMATCH", "学习机会没有引用模块源码", 409)
    conn.execute(
        "INSERT INTO growth_tasks "
        "(account_id, task_id, opportunity_id, topic_id, question, followups, source_refs, "
        "estimated_minutes, module_id, module_index_revision, learning_goal, "
        "back_answer, back_explanation, card_version, source_commit_shas) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            account_id,
            body.task_id,
            body.opportunity_id,
            body.topic_id,
            body.question,
            Jsonb(body.followups),
            Jsonb(opportunity["source_refs"]),
            opportunity["estimated_minutes"],
            body.module_id,
            body.module_index_revision,
            body.learning_goal or opportunity["learning_goal"],
            body.back_answer,
            body.back_explanation,
            body.card_version,
            Jsonb(body.source_commit_shas),
        ),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="task",
        entity_id=body.task_id,
        event_type="task_created",
        revision=1,
        payload={
            "opportunity_id": str(body.opportunity_id),
            "topic_id": str(body.topic_id),
            "question": body.question,
            "card_id": str(body.task_id),
            "learning_goal": body.learning_goal or opportunity["learning_goal"],
            "back_answer": body.back_answer,
            "back_explanation": body.back_explanation,
            "card_version": body.card_version,
            "source_commit_shas": body.source_commit_shas,
            "module_id": str(body.module_id) if body.module_id else None,
            "module_index_revision": body.module_index_revision,
        },
    )
    return 201, {"task_id": str(body.task_id), "revision": 1, "change_seq": seq}


def _save_task_draft(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    task_id: uuid.UUID,
    body: TaskDraftUpdate,
) -> tuple[int, dict[str, Any]]:
    task = conn.execute(
        "SELECT progress FROM growth_tasks WHERE account_id=%s AND task_id=%s FOR UPDATE",
        (account_id, task_id),
    ).fetchone()
    if task is None:
        raise ApiError("TASK_NOT_FOUND", "卡片不存在", 404)
    if task["progress"] in {"completed", "dismissed"}:
        raise ApiError("TASK_CLOSED", "卡片已结束，需要先重新打开", 409)
    existing = conn.execute(
        "SELECT revision, submitted_attempt_id FROM growth_task_drafts "
        "WHERE account_id=%s AND task_id=%s FOR UPDATE",
        (account_id, task_id),
    ).fetchone()
    revision = int(existing["revision"]) if existing else 0
    if revision != body.base_revision:
        return 409, {"code": "REVISION_CONFLICT", "task_id": str(task_id),
                     "server_revision": revision}
    submitted = existing["submitted_attempt_id"] if existing else None
    if submitted is not None and body.after_attempt_id != submitted:
        return 409, {"code": "DRAFT_SUBMITTED", "task_id": str(task_id),
                     "submitted_attempt_id": str(submitted),
                     "server_revision": revision}
    if submitted is None and body.after_attempt_id is not None:
        return 409, {"code": "INVALID_DRAFT_PARENT", "task_id": str(task_id),
                     "server_revision": revision}
    if existing is None:
        conn.execute(
            "INSERT INTO growth_task_drafts(account_id, task_id, content_text) "
            "VALUES (%s, %s, %s)",
            (account_id, task_id, body.content_text),
        )
    else:
        conn.execute(
            "UPDATE growth_task_drafts SET revision=revision+1, content_text=%s, "
            "submitted_attempt_id=NULL, updated_at=now() "
            "WHERE account_id=%s AND task_id=%s",
            (body.content_text, account_id, task_id),
        )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="task_draft", entity_id=task_id,
        event_type="task_draft_saved", revision=revision + 1,
        payload={"task_id": str(task_id), "content_text": body.content_text,
                 "submitted_attempt_id": None},
    )
    return 200, {"task_id": str(task_id), "revision": revision + 1,
                 "submitted_attempt_id": None, "change_seq": seq}


def _save_attempt(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    task_id: uuid.UUID,
    body: AttemptCreate,
) -> tuple[int, dict[str, Any]]:
    task = conn.execute(
        "SELECT progress FROM growth_tasks WHERE account_id=%s AND task_id=%s FOR UPDATE",
        (account_id, task_id),
    ).fetchone()
    if task is None:
        raise ApiError("TASK_NOT_FOUND", "卡片不存在", 404)
    if task["progress"] in {"completed", "dismissed"}:
        raise ApiError("TASK_CLOSED", "卡片已结束，需要先重新打开", 409)
    if body.parent_attempt_id:
        parent = conn.execute(
            "SELECT 1 FROM growth_learning_attempts "
            "WHERE account_id=%s AND task_id=%s AND attempt_id=%s",
            (account_id, task_id, body.parent_attempt_id),
        ).fetchone()
        if parent is None:
            raise ApiError("INVALID_PARENT_ATTEMPT", "修正回答不属于此卡片", 409)
    answer_hash = hashlib.sha256(body.answer_text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO growth_learning_attempts "
        "(account_id, attempt_id, task_id, parent_attempt_id, answer_text, answer_hash, "
        "hint_level, actor_origin) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            account_id,
            body.attempt_id,
            task_id,
            body.parent_attempt_id,
            body.answer_text,
            answer_hash,
            body.hint_level,
            body.actor_origin,
        ),
    )
    task_updated = conn.execute(
        "UPDATE growth_tasks SET progress='in_progress', revision=revision+1, updated_at=now() "
        "WHERE account_id=%s AND task_id=%s RETURNING revision",
        (account_id, task_id),
    ).fetchone()
    assert task_updated is not None
    draft = conn.execute(
        "SELECT revision FROM growth_task_drafts WHERE account_id=%s AND task_id=%s FOR UPDATE",
        (account_id, task_id),
    ).fetchone()
    draft_revision = int(draft["revision"]) + 1 if draft else 1
    if draft:
        conn.execute(
            "UPDATE growth_task_drafts SET revision=%s, content_text=%s, "
            "submitted_attempt_id=%s, updated_at=now() "
            "WHERE account_id=%s AND task_id=%s",
            (draft_revision, body.answer_text, body.attempt_id, account_id, task_id),
        )
    else:
        conn.execute(
            "INSERT INTO growth_task_drafts "
            "(account_id, task_id, content_text, submitted_attempt_id) "
            "VALUES (%s, %s, %s, %s)",
            (account_id, task_id, body.answer_text, body.attempt_id),
        )
    record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="task_draft", entity_id=task_id,
        event_type="task_draft_submitted", revision=draft_revision,
        payload={"task_id": str(task_id), "content_text": body.answer_text,
                 "submitted_attempt_id": str(body.attempt_id)},
    )
    conn.execute(
        "INSERT INTO growth_timeline "
        "(account_id, evidence_id, task_id, source_type, source_id, evidence_label, actor_origin) "
        "VALUES (%s, %s, %s, 'attempt', %s, 'practicing', %s)",
        (account_id, uuid.uuid4(), task_id, body.attempt_id, body.actor_origin),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="attempt",
        entity_id=body.attempt_id,
        event_type="answer_saved",
        revision=1,
        payload={
            "task_id": str(task_id),
            "parent_attempt_id": str(body.parent_attempt_id) if body.parent_attempt_id else None,
            "answer_hash": answer_hash,
            "answer_text": body.answer_text,
            "hint_level": body.hint_level,
            "actor_origin": body.actor_origin,
        },
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="task", entity_id=task_id,
        event_type="task_answer_submitted", revision=int(task_updated["revision"]),
        payload={"progress": "in_progress", "latest_attempt_id": str(body.attempt_id)},
    )
    enqueue_projection(
        conn, account_id=account_id, source_type="attempt",
        source_id=body.attempt_id, source_version=1, action="upsert",
    )
    return 202, {"attempt_id": str(body.attempt_id), "status": "accepted", "change_seq": seq}


def _set_task_progress(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    task_id: uuid.UUID,
    body: TaskProgressUpdate,
) -> tuple[int, dict[str, Any]]:
    task = conn.execute(
        "SELECT progress, revision FROM growth_tasks WHERE account_id=%s AND task_id=%s FOR UPDATE",
        (account_id, task_id),
    ).fetchone()
    if task is None:
        raise ApiError("TASK_NOT_FOUND", "卡片不存在", 404)
    revision = int(task["revision"])
    if revision != body.base_revision:
        return 409, {"code": "REVISION_CONFLICT", "task_id": str(task_id),
                     "server_revision": revision}
    current = str(task["progress"])
    allowed = {
        "ready": {"in_progress", "dismissed"},
        "in_progress": {"paused", "completed", "dismissed"},
        "paused": {"in_progress", "dismissed"},
        "completed": {"in_progress"},
        "dismissed": {"in_progress"},
    }
    if body.progress not in allowed[current]:
        raise ApiError("INVALID_PROGRESS", "卡片进度转换不合法", 409)
    conn.execute(
        "UPDATE growth_tasks SET progress=%s, revision=%s, updated_at=now() "
        "WHERE account_id=%s AND task_id=%s",
        (body.progress, revision + 1, account_id, task_id),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="task",
        entity_id=task_id,
        event_type="task_progress_changed",
        revision=revision + 1,
        payload={"from": current, "to": body.progress, "progress": body.progress},
    )
    if body.progress == "completed":
        due_at = datetime.now(UTC) + timedelta(days=2)
        scheduled = conn.execute(
            "INSERT INTO growth_review_schedules "
            "(account_id, task_id, fsrs_state, scheduler_version, due_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING revision",
            (account_id, task_id, normalize_fsrs_state(None, now=due_at),
             scheduler_version(), due_at),
        ).fetchone()
        if scheduled is not None:
            seq = record_change(
                conn, account_id=account_id, trace_id=trace_id,
                entity_type="review_schedule", entity_id=task_id,
                event_type="review_scheduled", revision=1,
                payload={"task_id": str(task_id), "due_at": due_at.isoformat(),
                         "scheduler_version": scheduler_version()},
            )
    return 200, {
        "task_id": str(task_id),
        "progress": body.progress,
        "revision": revision + 1,
        "change_seq": seq,
    }


def _submit_review(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    task_id: uuid.UUID, body: ReviewSubmit, reviewed_at: datetime,
) -> tuple[int, dict[str, Any]]:
    schedule = conn.execute(
        "SELECT revision, fsrs_state, scheduler_version, due_at, status "
        "FROM growth_review_schedules WHERE account_id=%s AND task_id=%s FOR UPDATE",
        (account_id, task_id),
    ).fetchone()
    if schedule is None:
        raise ApiError("REVIEW_NOT_SCHEDULED", "卡片尚未安排复习", 404)
    if schedule["status"] != "active":
        raise ApiError("REVIEW_SUSPENDED", "复习已暂停", 409)
    revision = int(schedule["revision"])
    if body.base_revision != revision:
        return 409, {"code": "REVISION_CONFLICT", "task_id": str(task_id),
                     "server_revision": revision}
    if reviewed_at < schedule["due_at"]:
        return 409, {"code": "REVIEW_NOT_DUE", "task_id": str(task_id),
                     "due_at": schedule["due_at"].isoformat()}
    try:
        update = review_fsrs_card(
            fsrs_state=schedule["fsrs_state"],
            answer=cast("ReviewAnswer", body.answer),
            peeked_this_session=body.hint_level > 0,
            reviewed_at=reviewed_at,
            enable_fuzzing=False,
        )
    except InputError as exc:
        raise ApiError("INVALID_REVIEW_RATING", "使用提示后不能评价为轻松或良好", 422) from exc
    due_after = datetime.fromisoformat(update.due_date.replace("Z", "+00:00"))
    conn.execute(
        "INSERT INTO growth_review_events "
        "(account_id, review_id, task_id, schedule_revision, answer_text, answer, hint_level, "
        "actor_origin, rating, due_before, due_after, reviewed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (account_id, body.review_id, task_id, revision + 1, body.answer_text, body.answer,
         body.hint_level, body.actor_origin, update.rating, schedule["due_at"],
         due_after, reviewed_at),
    )
    conn.execute(
        "UPDATE growth_review_schedules SET revision=%s, fsrs_state=%s, "
        "due_at=%s, last_hint_level=%s, last_review_id=%s, updated_at=now() "
        "WHERE account_id=%s AND task_id=%s",
        (revision + 1, update.fsrs_state, due_after, body.hint_level,
         body.review_id, account_id, task_id),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="review_schedule", entity_id=task_id,
        event_type="review_recorded", revision=revision + 1,
        payload={"review_id": str(body.review_id), "due_at": due_after.isoformat(),
                 "hint_level": body.hint_level, "rating": update.rating,
                 "actor_origin": body.actor_origin},
    )
    return 200, {"review_id": str(body.review_id), "task_id": str(task_id),
                 "revision": revision + 1, "due_at": due_after.isoformat(),
                 "hint_level": body.hint_level, "change_seq": seq}


def _save_feedback(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    attempt_id: uuid.UUID,
    body: AttemptFeedback,
) -> tuple[int, dict[str, Any]]:
    attempt = conn.execute(
        "SELECT growth_learning_attempts.status, growth_tasks.source_refs, "
        "growth_opportunity_batches.origin AS opportunity_origin "
        "FROM growth_learning_attempts "
        "JOIN growth_tasks ON growth_tasks.account_id=growth_learning_attempts.account_id "
        "AND growth_tasks.task_id=growth_learning_attempts.task_id "
        "JOIN growth_opportunities ON growth_opportunities.account_id=growth_tasks.account_id "
        "AND growth_opportunities.opportunity_id=growth_tasks.opportunity_id "
        "JOIN growth_opportunity_batches ON "
        "growth_opportunity_batches.account_id=growth_opportunities.account_id "
        "AND growth_opportunity_batches.analysis_id=growth_opportunities.analysis_id "
        "WHERE growth_learning_attempts.account_id=%s AND growth_learning_attempts.attempt_id=%s",
        (account_id, attempt_id),
    ).fetchone()
    if attempt is None:
        raise ApiError("ATTEMPT_NOT_FOUND", "回答不存在", 404)
    if body.feedback_origin == "live" and attempt["opportunity_origin"] != "live":
        raise ApiError("LIVE_ORIGIN_INVALID", "回放机会不能标为 live 反馈", 409)
    if not {str(ref) for ref in body.feedback.source_refs} <= set(attempt["source_refs"]):
        raise ApiError("INVALID_SOURCE_REF", "反馈引用了未验证的源码", 422)
    if attempt["status"] == "feedback_ready":
        raise ApiError("FEEDBACK_ALREADY_SAVED", "反馈已保存，不能覆盖", 409)
    conn.execute(
        "UPDATE growth_learning_attempts SET status='feedback_ready', feedback=%s, "
        "feedback_origin=%s, feedback_at=now() WHERE account_id=%s AND attempt_id=%s",
        (
            Jsonb(body.feedback.model_dump(mode="json")),
            body.feedback_origin,
            account_id,
            attempt_id,
        ),
    )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="attempt",
        entity_id=attempt_id,
        event_type="feedback_ready",
        revision=2,
        payload={"feedback_origin": body.feedback_origin,
                 "feedback": body.feedback.model_dump(mode="json"),
                 "status": "feedback_ready"},
    )
    enqueue_projection(
        conn, account_id=account_id, source_type="attempt",
        source_id=attempt_id, source_version=2, action="upsert",
    )
    return 200, {"attempt_id": str(attempt_id), "status": "feedback_ready", "change_seq": seq}


def _save_note(
    conn: psycopg.Connection[Any],
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    body: NoteCreate,
) -> tuple[int, dict[str, Any]]:
    if body.task_id is None:
        topic = conn.execute(
            "SELECT status FROM growth_topics WHERE account_id=%s AND topic_id=%s "
            "AND deleted_at IS NULL",
            (account_id, body.topic_id),
        ).fetchone()
        if topic is None or topic["status"] != "active":
            raise ApiError("TOPIC_NOT_FOUND", "主题不存在或已归档", 404)
        alias = conn.execute(
            "SELECT 1 FROM growth_topic_aliases WHERE account_id=%s "
            "AND alias_topic_id=%s", (account_id, body.topic_id),
        ).fetchone()
        if alias is not None:
            raise ApiError("TOPIC_MERGED", "旧主题已合并，请使用目标主题", 409)
        source_refs: list[str] = []
    else:
        task = conn.execute(
            "SELECT topic_id, source_refs FROM growth_tasks "
            "WHERE account_id=%s AND task_id=%s",
            (account_id, body.task_id),
        ).fetchone()
        if task is None or task["topic_id"] != body.topic_id:
            raise ApiError("TASK_TOPIC_MISMATCH", "笔记关联的卡片和主题不匹配", 409)
        source_refs = task["source_refs"]
    content_hash = hashlib.sha256(body.content_text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO growth_notes "
        "(account_id, note_id, task_id, topic_id, author, content_text, content_hash, source_refs) "
        "VALUES (%s, %s, %s, %s, 'user', %s, %s, %s)",
        (
            account_id,
            body.note_id,
            body.task_id,
            body.topic_id,
            body.content_text,
            content_hash,
            Jsonb(source_refs),
        ),
    )
    conn.execute(
        "INSERT INTO growth_note_revisions "
        "(account_id, note_id, revision, content_text, content_hash) "
        "VALUES (%s, %s, 1, %s, %s)",
        (account_id, body.note_id, body.content_text, content_hash),
    )
    if body.task_id is not None:
        conn.execute(
            "INSERT INTO growth_timeline "
            "(account_id, evidence_id, task_id, source_type, source_id, "
            "evidence_label, actor_origin) "
            "VALUES (%s, %s, %s, 'note', %s, 'encountered', 'user')",
            (account_id, uuid.uuid4(), body.task_id, body.note_id),
        )
    seq = record_change(
        conn,
        account_id=account_id,
        trace_id=trace_id,
        entity_type="note",
        entity_id=body.note_id,
        event_type="note_saved",
        revision=1,
        payload={
            "task_id": str(body.task_id) if body.task_id else None,
            "topic_id": str(body.topic_id),
            "content_hash": content_hash,
            "content_text": body.content_text,
            "source_refs": source_refs,
            "author": "user",
        },
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="note_revision",
        entity_id=uuid.UUID(note_revision_sync_id(str(body.note_id), 1)),
        event_type="note_revision_saved", revision=1,
        payload={"note_id": str(body.note_id), "content_text": body.content_text,
                 "content_hash": content_hash, "source_conflict_ids": []},
    )
    enqueue_projection(
        conn, account_id=account_id, source_type="note", source_id=body.note_id,
        source_version=1, action="upsert",
    )
    return 201, {"note_id": str(body.note_id), "revision": 1, "change_seq": seq}


def _note_row(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, note_id: uuid.UUID
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT revision, content_text, content_hash, deleted_at FROM growth_notes "
        "WHERE account_id=%s AND note_id=%s FOR UPDATE",
        (account_id, note_id),
    ).fetchone()
    if row is None:
        raise ApiError("NOTE_NOT_FOUND", "笔记不存在", 404)
    return row


def _note_revision(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    note_id: uuid.UUID, body: NoteRevision,
) -> tuple[int, dict[str, Any]]:
    note = _note_row(conn, account_id, note_id)
    if note["deleted_at"] is not None:
        return 410, {
            "code": "ENTITY_DELETED", "note_id": str(note_id), "revision": note["revision"]
        }
    if body.base_revision != note["revision"]:
        base = conn.execute(
            "SELECT 1 FROM growth_note_revisions "
            "WHERE account_id=%s AND note_id=%s AND revision=%s",
            (account_id, note_id, body.base_revision),
        ).fetchone()
        if base is None:
            raise ApiError("INVALID_BASE_REVISION", "笔记基础版本不存在", 422)
        conflict_id = uuid.uuid4()
        proposed_hash = hashlib.sha256(body.content_text.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO growth_note_conflicts "
            "(account_id, conflict_id, note_id, base_revision, observed_server_revision, "
            "proposed_text, proposed_hash) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (account_id, conflict_id, note_id, body.base_revision, note["revision"],
             body.content_text, proposed_hash),
        )
        seq = record_change(
            conn, account_id=account_id, trace_id=trace_id, entity_type="note_conflict",
            entity_id=conflict_id, event_type="note_conflict_saved", revision=1,
            payload={"note_id": str(note_id), "base_revision": body.base_revision,
                     "observed_server_revision": note["revision"],
                     "proposed_text": body.content_text, "proposed_hash": proposed_hash},
        )
        return 409, {"code": "REVISION_CONFLICT", "note_id": str(note_id),
                     "conflict_id": str(conflict_id), "server_revision": note["revision"],
                     "change_seq": seq}
    revision = int(note["revision"]) + 1
    content_hash = hashlib.sha256(body.content_text.encode("utf-8")).hexdigest()
    conn.execute(
        "UPDATE growth_notes SET revision=%s, content_text=%s, content_hash=%s, "
        "updated_at=now() WHERE account_id=%s AND note_id=%s",
        (revision, body.content_text, content_hash, account_id, note_id),
    )
    conn.execute(
        "INSERT INTO growth_note_revisions "
        "(account_id, note_id, revision, content_text, content_hash) "
        "VALUES (%s, %s, %s, %s, %s)",
        (account_id, note_id, revision, body.content_text, content_hash),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id, entity_type="note",
        entity_id=note_id, event_type="note_revised", revision=revision,
        payload={"content_text": body.content_text, "content_hash": content_hash},
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="note_revision",
        entity_id=uuid.UUID(note_revision_sync_id(str(note_id), revision)),
        event_type="note_revision_saved", revision=revision,
        payload={"note_id": str(note_id), "content_text": body.content_text,
                 "content_hash": content_hash, "source_conflict_ids": []},
    )
    enqueue_projection(
        conn, account_id=account_id, source_type="note", source_id=note_id,
        source_version=revision, action="upsert",
    )
    return 200, {"note_id": str(note_id), "revision": revision, "change_seq": seq}


def _resolve_note(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    note_id: uuid.UUID, body: NoteResolve,
) -> tuple[int, dict[str, Any]]:
    note = _note_row(conn, account_id, note_id)
    if note["deleted_at"] is not None:
        return 410, {
            "code": "ENTITY_DELETED", "note_id": str(note_id), "revision": note["revision"]
        }
    if body.base_revision != note["revision"]:
        return _note_revision(
            conn, account_id, trace_id, note_id,
            NoteRevision(
                operation_id=body.operation_id, base_revision=body.base_revision,
                content_text=body.content_text,
            ),
        )
    ids = list(dict.fromkeys(body.conflict_ids))
    if len(ids) != len(body.conflict_ids):
        raise ApiError("INVALID_CONFLICTS", "冲突 ID 重复", 422)
    conflicts = conn.execute(
        "SELECT conflict_id FROM growth_note_conflicts "
        "WHERE account_id=%s AND note_id=%s AND conflict_id=ANY(%s) "
        "AND resolved_revision IS NULL FOR UPDATE",
        (account_id, note_id, ids),
    ).fetchall()
    if len(conflicts) != len(ids):
        raise ApiError("INVALID_CONFLICTS", "冲突不存在或已经解决", 422)
    revision = int(note["revision"]) + 1
    content_hash = hashlib.sha256(body.content_text.encode("utf-8")).hexdigest()
    conn.execute(
        "UPDATE growth_notes SET revision=%s, content_text=%s, content_hash=%s, "
        "updated_at=now() WHERE account_id=%s AND note_id=%s",
        (revision, body.content_text, content_hash, account_id, note_id),
    )
    conn.execute(
        "INSERT INTO growth_note_revisions "
        "(account_id, note_id, revision, content_text, content_hash, source_conflict_ids) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (account_id, note_id, revision, body.content_text, content_hash,
         Jsonb([str(item) for item in ids])),
    )
    conn.execute(
        "UPDATE growth_note_conflicts SET resolved_revision=%s "
        "WHERE account_id=%s AND note_id=%s AND conflict_id=ANY(%s)",
        (revision, account_id, note_id, ids),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id, entity_type="note",
        entity_id=note_id, event_type="note_conflict_resolved", revision=revision,
        payload={"content_text": body.content_text, "content_hash": content_hash,
                 "source_conflict_ids": [str(item) for item in ids]},
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="note_revision",
        entity_id=uuid.UUID(note_revision_sync_id(str(note_id), revision)),
        event_type="note_revision_saved", revision=revision,
        payload={"note_id": str(note_id), "content_text": body.content_text,
                 "content_hash": content_hash,
                 "source_conflict_ids": [str(item) for item in ids]},
    )
    for conflict_id in ids:
        seq = record_change(
            conn, account_id=account_id, trace_id=trace_id,
            entity_type="note_conflict", entity_id=conflict_id,
            event_type="note_conflict_resolved", revision=2,
            payload={"note_id": str(note_id), "resolved_revision": revision},
        )
    enqueue_projection(
        conn, account_id=account_id, source_type="note", source_id=note_id,
        source_version=revision, action="upsert",
    )
    return 200, {"note_id": str(note_id), "revision": revision, "change_seq": seq}


def _delete_note(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    note_id: uuid.UUID, body: NoteDelete,
) -> tuple[int, dict[str, Any]]:
    note = _note_row(conn, account_id, note_id)
    if note["deleted_at"] is not None:
        return 410, {
            "code": "ENTITY_DELETED", "note_id": str(note_id), "revision": note["revision"]
        }
    if body.base_revision != note["revision"]:
        return 409, {"code": "REVISION_CONFLICT", "note_id": str(note_id),
                     "server_revision": note["revision"]}
    revision = int(note["revision"]) + 1
    deleted = conn.execute(
        "UPDATE growth_notes SET revision=%s, deleted_at=now(), updated_at=now() "
        "WHERE account_id=%s AND note_id=%s RETURNING deleted_at",
        (revision, account_id, note_id),
    ).fetchone()
    assert deleted is not None
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id, entity_type="note",
        entity_id=note_id, event_type="note_deleted", revision=revision,
        payload={"note_id": str(note_id)}, deleted_at=deleted["deleted_at"],
    )
    enqueue_projection(
        conn, account_id=account_id, source_type="note", source_id=note_id,
        source_version=revision, action="delete",
    )
    return 200, {"note_id": str(note_id), "revision": revision, "change_seq": seq,
                 "deleted_at": deleted["deleted_at"].isoformat()}


def _save_knowledge_bundle(
    conn: psycopg.Connection[Any], account_id: uuid.UUID, trace_id: uuid.UUID,
    card_id: uuid.UUID, body: KnowledgeBundleUpsert,
) -> tuple[int, dict[str, Any]]:
    if body.bundle.get("card_id") != str(card_id):
        raise ApiError("INVALID_REQUEST", "知识卡身份不一致", 422)
    canonical = json.dumps(body.bundle, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    if len(canonical.encode("utf-8")) > 4 * 1024 * 1024:
        raise ApiError("PAYLOAD_TOO_LARGE", "知识卡同步内容超出 4 MB", 413)
    owned = conn.execute(
        "SELECT project_id FROM growth_projects WHERE account_id=%s "
        "AND project_id=ANY(%s) AND deleted_at IS NULL",
        (account_id, body.project_ids),
    ).fetchall()
    if {row["project_id"] for row in owned} != set(body.project_ids):
        raise ApiError("INVALID_REFERENCE", "知识卡来源项目未发布到此账号", 409)
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{account_id}:{card_id}:knowledge-bundle",),
    )
    current = conn.execute(
        "SELECT revision FROM growth_knowledge_bundles "
        "WHERE account_id=%s AND card_id=%s FOR UPDATE",
        (account_id, card_id),
    ).fetchone()
    revision = int(current["revision"]) if current else 0
    if revision != body.base_revision:
        return 409, {"code": "REVISION_CONFLICT", "card_id": str(card_id),
                     "server_revision": revision}
    next_revision = revision + 1
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO growth_knowledge_bundles "
        "(account_id,card_id,revision,project_ids,bundle,payload_sha256) "
        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (account_id,card_id) "
        "DO UPDATE SET revision=EXCLUDED.revision,project_ids=EXCLUDED.project_ids,"
        "bundle=EXCLUDED.bundle,payload_sha256=EXCLUDED.payload_sha256,updated_at=now()",
        (account_id, card_id, next_revision, body.project_ids, Jsonb(body.bundle), digest),
    )
    seq = record_change(
        conn, account_id=account_id, trace_id=trace_id,
        entity_type="knowledge_bundle", entity_id=card_id,
        event_type="knowledge_bundle_saved", revision=next_revision,
        payload={"card_id": str(card_id), "payload_sha256": digest},
    )
    return (201 if revision == 0 else 200), {
        "card_id": str(card_id), "revision": next_revision,
        "payload_sha256": digest, "change_seq": seq,
    }


def _sync_read(
    settings: CloudSettings, account_id: uuid.UUID, device_id: uuid.UUID,
    *, after: int | None = None, until: int | None = None, limit: int = 100,
) -> dict[str, Any]:
    with connect(settings.database_url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        device = conn.execute(
            "SELECT revoked_at FROM growth_devices WHERE account_id=%s AND device_id=%s",
            (account_id, device_id),
        ).fetchone()
        if device is None or device["revoked_at"] is not None:
            raise ApiError("DEVICE_REVOKED", "设备未注册或已撤销", 403)
        head = conn.execute(
            "SELECT last_seq FROM growth_sync_heads WHERE account_id=%s", (account_id,)
        ).fetchone()
        watermark = int(head["last_seq"]) if head else 0
        if after is None:
            entities: dict[str, Any] = {}
            for name, table, id_column in (
                ("projects", "growth_projects", "project_id"),
                ("features", "growth_features", "feature_id"),
                ("snapshots", "growth_snapshots", "snapshot_id"),
                ("source_refs", "growth_source_refs", "source_ref_id"),
                ("source_excerpts", "growth_source_excerpts", "source_ref_id"),
                ("analysis_runs", "growth_analysis_runs", "analysis_id"),
                ("modules", "growth_modules", "module_id"),
                ("chat_sessions", "growth_chat_sessions", "session_id"),
                ("chat_messages", "growth_chat_messages", "message_id"),
                ("chat_suggestions", "growth_chat_suggestions", "suggestion_id"),
                ("development_events", "growth_development_events", "event_id"),
                ("opportunities", "growth_opportunities", "opportunity_id"),
                ("topic_proposals", "growth_topic_proposals", "proposal_id"),
                ("topics", "growth_topics", "topic_id"),
                ("topic_aliases", "growth_topic_aliases", "alias_topic_id"),
                ("topic_moves", "growth_topic_moves", "move_id"),
                ("topic_links", "growth_topic_links", "topic_id, entity_type, entity_id"),
                ("tasks", "growth_tasks", "task_id"),
                ("task_drafts", "growth_task_drafts", "task_id"),
                ("attempts", "growth_learning_attempts", "attempt_id"),
                ("notes", "growth_notes", "note_id"),
                ("note_revisions", "growth_note_revisions", "note_id, revision"),
                ("note_conflicts", "growth_note_conflicts", "conflict_id"),
                ("review_schedules", "growth_review_schedules", "task_id"),
                ("review_events", "growth_review_events", "review_id"),
            ):
                # Table and order expressions are fixed server-side, never caller SQL.
                order = sql.SQL(", ").join(
                    sql.Identifier(part.strip()) for part in id_column.split(",")
                )
                entities[name] = conn.execute(
                    sql.SQL("SELECT * FROM {} WHERE account_id=%s ORDER BY {}").format(
                        sql.Identifier(table), order
                    ),
                    (account_id,),
                ).fetchall()
            return json.loads(json.dumps(
                {"high_watermark": watermark, "entities": entities}, default=str
            ))
        if after < 0 or until is not None and (until < after or until > watermark):
            raise ApiError("INVALID_CURSOR", "同步游标超出有效范围", 422)
        if after > watermark:
            raise ApiError("INVALID_CURSOR", "同步游标超出当前水位", 422)
        boundary = watermark if until is None else until
        rows = conn.execute(
            "SELECT change_seq, entity_type, entity_id, event_type, revision, "
            "deleted_at, payload FROM growth_change_feed "
            "WHERE account_id=%s AND change_seq>%s AND change_seq<=%s "
            "ORDER BY change_seq LIMIT %s",
            (account_id, after, boundary, limit + 1),
        ).fetchall()
        page = rows[:limit]
        next_after = int(page[-1]["change_seq"]) if page else after
        return json.loads(json.dumps({
            "high_watermark": boundary, "next_after": next_after,
            "has_more": len(rows) > limit, "changes": page,
        }, default=str))


def _sync_batch_operation(item: SyncOperationItem) -> tuple[OperationModel, ApplyOperation]:
    """Permit only offline-safe business writes through the batch endpoint."""
    if item.method == "POST" and item.path == "/v1/source-excerpts":
        excerpt = cast("SourceExcerptCreate", _body(SourceExcerptCreate, item.payload))
        return excerpt, lambda conn, account, trace: _save_source_excerpt(
            conn, account, trace, excerpt
        )
    if item.method == "POST" and item.path == "/v1/notes":
        note = cast("NoteCreate", _body(NoteCreate, item.payload))
        return note, lambda conn, account, trace: _save_note(conn, account, trace, note)
    deleted_note = re.fullmatch(r"/v1/notes/([0-9a-f-]{36})", item.path)
    if item.method == "DELETE" and deleted_note is not None:
        try:
            note_id = uuid.UUID(deleted_note.group(1))
        except ValueError as exc:
            raise ApiError("BATCH_OPERATION_FORBIDDEN", "批量同步对象 ID 无效", 422) from exc
        if str(note_id) != deleted_note.group(1):
            raise ApiError("BATCH_OPERATION_FORBIDDEN", "批量同步对象 ID 格式无效", 422)
        deletion = cast("NoteDelete", _body(NoteDelete, item.payload))
        return deletion, lambda conn, account, trace: _delete_note(
            conn, account, trace, note_id, deletion
        )
    match = re.fullmatch(
        r"/v1/(tasks|notes)/([0-9a-f-]{36})/(draft|attempts|progress|reviews|revisions)",
        item.path,
    )
    if match is None:
        raise ApiError("BATCH_OPERATION_FORBIDDEN", "批量同步不允许此路径", 422)
    try:
        entity_id = uuid.UUID(match.group(2))
    except ValueError as exc:
        raise ApiError("BATCH_OPERATION_FORBIDDEN", "批量同步对象 ID 无效", 422) from exc
    if str(entity_id) != match.group(2):
        raise ApiError("BATCH_OPERATION_FORBIDDEN", "批量同步对象 ID 格式无效", 422)
    target = (match.group(1), match.group(3), item.method)
    if target == ("tasks", "draft", "PUT"):
        draft = cast("TaskDraftUpdate", _body(TaskDraftUpdate, item.payload))
        return draft, lambda conn, account, trace: _save_task_draft(
            conn, account, trace, entity_id, draft
        )
    if target == ("tasks", "attempts", "POST"):
        attempt = cast("AttemptCreate", _body(AttemptCreate, item.payload))
        return attempt, lambda conn, account, trace: _save_attempt(
            conn, account, trace, entity_id, attempt
        )
    if target == ("tasks", "progress", "PATCH"):
        progress = cast("TaskProgressUpdate", _body(TaskProgressUpdate, item.payload))
        return progress, lambda conn, account, trace: _set_task_progress(
            conn, account, trace, entity_id, progress
        )
    if target == ("tasks", "reviews", "POST"):
        review = cast("ReviewSubmit", _body(ReviewSubmit, item.payload))
        return review, lambda conn, account, trace: _submit_review(
            conn, account, trace, entity_id, review, datetime.now(UTC)
        )
    if target == ("notes", "revisions", "PUT"):
        revision = cast("NoteRevision", _body(NoteRevision, item.payload))
        return revision, lambda conn, account, trace: _note_revision(
            conn, account, trace, entity_id, revision
        )
    raise ApiError("BATCH_OPERATION_FORBIDDEN", "批量同步不允许此方法", 422)


def create_app(settings: CloudSettings) -> Starlette:
    async def api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return JSONResponse(
            {
                "code": exc.code,
                "message": exc.message,
                "trace_id": str(request.state.trace_id),
                "retryable": exc.retryable,
            },
            status_code=exc.status,
        )

    async def write_endpoint(
        request: Request,
        model: type[OperationModel],
        handler: Callable[..., tuple[int, dict[str, Any]]],
        *,
        requires_device: bool = True,
    ) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request) if requires_device else None
        try:
            raw = await request.json()
        except ValueError as exc:
            raise ApiError("INVALID_JSON", "请求正文不是 JSON", 400) from exc
        body = _body(model, raw)
        payload = body.model_dump(mode="json")
        status, response = await asyncio.to_thread(
            lambda: _write_operation(
                settings,
                account_id=account_id,
                device_id=device_id,
                trace_id=request.state.trace_id,
                operation_id=body.operation_id,
                endpoint=request.url.path,
                payload=payload,
                apply=lambda conn, account, trace: handler(conn, account, trace, body),
            )
        )
        return JSONResponse(response, status_code=status)

    async def devices(request: Request) -> JSONResponse:
        return await write_endpoint(
            request, DeviceRegistration, _register_device, requires_device=False
        )

    async def device_ack(request: Request) -> JSONResponse:
        device_id = _device_id(request)

        def handle(conn: psycopg.Connection[Any], account: uuid.UUID,
                   _trace: uuid.UUID,
                   body: DeviceCursorAck) -> tuple[int, dict[str, Any]]:
            return _ack_device(conn, account, device_id, body)

        return await write_endpoint(request, DeviceCursorAck, handle)

    async def device_revoke(request: Request) -> JSONResponse:
        requester = _device_id(request)
        target = request.path_params["device_id"]

        def handle(conn: psycopg.Connection[Any], account: uuid.UUID,
                   trace: uuid.UUID,
                   body: DeviceRevocation) -> tuple[int, dict[str, Any]]:
            return _revoke_device(conn, account, trace, requester, target, body)

        return await write_endpoint(request, DeviceRevocation, handle)

    async def projects(request: Request) -> JSONResponse:
        return await write_endpoint(request, ProjectCreate, _create_project)

    async def topics_create(request: Request) -> JSONResponse:
        return await write_endpoint(request, TopicCreate, _create_topic)

    async def topics_list(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read() -> dict[str, Any]:
            with connect(settings.database_url) as conn:
                rows = conn.execute(
                    "SELECT topics.topic_id, topics.title, topics.status, "
                    "topics.revision, topics.parent_topic_id, "
                    "aliases.canonical_topic_id "
                    "FROM growth_topics topics LEFT JOIN growth_topic_aliases aliases "
                    "ON aliases.account_id=topics.account_id "
                    "AND aliases.alias_topic_id=topics.topic_id "
                    "WHERE topics.account_id=%s AND topics.deleted_at IS NULL "
                    "ORDER BY topics.updated_at DESC, topics.topic_id DESC LIMIT 200",
                    (account_id,),
                ).fetchall()
                return {"topics": json.loads(json.dumps(rows, default=str))}

        return JSONResponse(await asyncio.to_thread(read))

    async def topic_update(request: Request) -> JSONResponse:
        topic_id = request.path_params["topic_id"]

        def handle(conn: psycopg.Connection[Any], account: uuid.UUID,
                   trace: uuid.UUID, body: TopicUpdate) -> tuple[int, dict[str, Any]]:
            return _update_topic(conn, account, trace, topic_id, body)

        return await write_endpoint(request, TopicUpdate, handle)

    async def topic_merge(request: Request) -> JSONResponse:
        source_id = request.path_params["topic_id"]

        def handle(conn: psycopg.Connection[Any], account: uuid.UUID,
                   trace: uuid.UUID, body: TopicMerge) -> tuple[int, dict[str, Any]]:
            return _merge_topic(conn, account, trace, source_id, body)

        return await write_endpoint(request, TopicMerge, handle)

    async def topic_split(request: Request) -> JSONResponse:
        parent_id = request.path_params["topic_id"]

        def handle(conn: psycopg.Connection[Any], account: uuid.UUID,
                   trace: uuid.UUID, body: TopicSplit) -> tuple[int, dict[str, Any]]:
            return _split_topic(conn, account, trace, parent_id, body)

        return await write_endpoint(request, TopicSplit, handle)

    async def topic_read(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        topic_id = request.path_params["topic_id"]
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read() -> dict[str, Any]:
            with connect(settings.database_url) as conn:
                alias = conn.execute(
                    "SELECT canonical_topic_id FROM growth_topic_aliases "
                    "WHERE account_id=%s AND alias_topic_id=%s",
                    (account_id, topic_id),
                ).fetchone()
                canonical = alias["canonical_topic_id"] if alias else topic_id
                topic = conn.execute(
                    "SELECT * FROM growth_topics WHERE account_id=%s AND topic_id=%s "
                    "AND deleted_at IS NULL", (account_id, canonical),
                ).fetchone()
                if topic is None:
                    raise ApiError("TOPIC_NOT_FOUND", "主题不存在", 404)
                notes = conn.execute(
                    "SELECT DISTINCT n.* FROM growth_notes n "
                    "LEFT JOIN growth_topic_links l ON l.account_id=n.account_id "
                    "AND l.entity_type='note' AND l.entity_id=n.note_id "
                    "AND l.topic_id=%s "
                    "WHERE n.account_id=%s AND n.deleted_at IS NULL "
                    "AND (n.topic_id=%s OR l.topic_id IS NOT NULL) ORDER BY n.note_id",
                    (canonical, account_id, canonical),
                ).fetchall()
                tasks = conn.execute(
                    "SELECT DISTINCT t.* FROM growth_tasks t "
                    "LEFT JOIN growth_topic_links l ON l.account_id=t.account_id "
                    "AND l.entity_type='task' AND l.entity_id=t.task_id "
                    "AND l.topic_id=%s "
                    "WHERE t.account_id=%s AND t.deleted_at IS NULL "
                    "AND (t.topic_id=%s OR l.topic_id IS NOT NULL) ORDER BY t.task_id",
                    (canonical, account_id, canonical),
                ).fetchall()
                children = conn.execute(
                    "SELECT topic_id, title, status, revision FROM growth_topics "
                    "WHERE account_id=%s AND parent_topic_id=%s ORDER BY topic_id",
                    (account_id, canonical),
                ).fetchall()
                moves = conn.execute(
                    "SELECT move_id, entity_type, entity_id, from_topic_id, to_topic_id, "
                    "action, moved_at FROM growth_topic_moves WHERE account_id=%s "
                    "AND (from_topic_id=%s OR to_topic_id=%s) ORDER BY moved_at, move_id",
                    (account_id, canonical, canonical),
                ).fetchall()
                return json.loads(json.dumps({
                    "requested_topic_id": str(topic_id),
                    "canonical_topic_id": str(canonical), "is_alias": alias is not None,
                    "topic": topic, "notes": notes, "tasks": tasks,
                    "children": children, "moves": moves,
                }, default=str))

        return JSONResponse(await asyncio.to_thread(read))

    async def chats_start(request: Request) -> JSONResponse:
        return await write_endpoint(request, ChatStart, _start_chat)

    async def chats_list(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read() -> dict[str, Any]:
            with connect(settings.database_url) as conn:
                rows = conn.execute(
                    "SELECT session_id, project_id, created_at, updated_at "
                    "FROM growth_chat_sessions WHERE account_id=%s "
                    "ORDER BY updated_at DESC, session_id DESC LIMIT 100",
                    (account_id,),
                ).fetchall()
                return {"sessions": json.loads(json.dumps(rows, default=str))}

        return JSONResponse(await asyncio.to_thread(read))

    async def chat_user_message(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]

        def handle(conn: psycopg.Connection[Any], account: uuid.UUID,
                   trace: uuid.UUID, body: ChatUserMessage) -> tuple[int, dict[str, Any]]:
            return _chat_user_message(conn, account, trace, session_id, body)

        return await write_endpoint(request, ChatUserMessage, handle)

    async def chat_assistant_replay(request: Request) -> JSONResponse:
        if not settings.acceptance_mode:
            raise ApiError("REPLAY_FORBIDDEN", "固定对话回复只允许在验收环境使用", 403)
        session_id = request.path_params["session_id"]

        def handle(conn: psycopg.Connection[Any], account: uuid.UUID,
                   trace: uuid.UUID, body: ChatAssistantReplay) -> tuple[int, dict[str, Any]]:
            return _chat_assistant_message(
                conn, account, trace, session_id, body, origin="replay",
            )

        return await write_endpoint(request, ChatAssistantReplay, handle)

    async def chat_assistant_live(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]

        def handle(conn: psycopg.Connection[Any], account: uuid.UUID,
                   trace: uuid.UUID, body: ChatAssistantLive) -> tuple[int, dict[str, Any]]:
            return _chat_assistant_message(
                conn, account, trace, session_id, body, origin="live",
            )

        return await write_endpoint(request, ChatAssistantLive, handle)

    async def chat_suggestion_decision(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        suggestion_id = request.path_params["suggestion_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: ChatSuggestionDecision,
        ) -> tuple[int, dict[str, Any]]:
            return _decide_chat_suggestion(conn, account, trace, session_id,
                                           suggestion_id, body)

        return await write_endpoint(request, ChatSuggestionDecision, handle)

    async def chat_read(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        session_id = request.path_params["session_id"]
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read() -> dict[str, Any]:
            with connect(settings.database_url) as conn:
                session = conn.execute(
                    "SELECT * FROM growth_chat_sessions WHERE account_id=%s "
                    "AND session_id=%s", (account_id, session_id),
                ).fetchone()
                if session is None:
                    raise ApiError("CHAT_NOT_FOUND", "对话不存在", 404)
                messages = conn.execute(
                    "SELECT * FROM growth_chat_messages WHERE account_id=%s "
                    "AND session_id=%s ORDER BY created_at, message_id",
                    (account_id, session_id),
                ).fetchall()
                suggestions = conn.execute(
                    "SELECT * FROM growth_chat_suggestions WHERE account_id=%s "
                    "AND session_id=%s ORDER BY suggestion_id",
                    (account_id, session_id),
                ).fetchall()
                return json.loads(json.dumps({"session": session,
                                               "messages": messages,
                                               "suggestions": suggestions},
                                              default=str))

        return JSONResponse(await asyncio.to_thread(read))

    async def development_events(request: Request) -> JSONResponse:
        return await write_endpoint(request, DevelopmentEvent, _create_development_event)

    async def development_event_claim(request: Request) -> JSONResponse:
        event_id = request.path_params["event_id"]
        device_id = _device_id(request)

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: DevelopmentEventClaim,
        ) -> tuple[int, dict[str, Any]]:
            return _claim_development_event(conn, account, trace, device_id,
                                            event_id, body)

        return await write_endpoint(request, DevelopmentEventClaim, handle)

    async def development_inbox(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read_inbox() -> list[dict[str, Any]]:
            with connect(settings.database_url) as conn:
                rows = conn.execute(
                    "SELECT event_id, schema_version, project_id, feature_id, "
                    "event_type, occurred_at, source, target_device_id, "
                    "expected_head_sha, status FROM growth_development_events "
                    "WHERE account_id=%s AND (target_device_id=%s OR "
                    "status='pending_confirmation') ORDER BY created_at, event_id",
                    (account_id, device_id),
                ).fetchall()
                return json.loads(json.dumps(rows, default=str))

        return JSONResponse({"events": await asyncio.to_thread(read_inbox)})

    async def exports_create(request: Request) -> JSONResponse:
        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: ExportCreate,
        ) -> tuple[int, dict[str, Any]]:
            return create_export(conn, account_id=account, trace_id=trace,
                                 export_id=body.export_id)

        return await write_endpoint(request, ExportCreate, handle)

    async def export_read(request: Request) -> JSONResponse | PlainTextResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        export_id = request.path_params["export_id"]
        kind = request.query_params.get("format", "json")
        if kind not in {"json", "markdown"}:
            raise ApiError("INVALID_EXPORT_FORMAT", "导出格式须为 json 或 markdown", 422)
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read() -> dict[str, Any]:
            with connect(settings.database_url) as conn:
                row = conn.execute(
                    "SELECT json_payload, markdown_text FROM growth_exports "
                    "WHERE account_id=%s AND export_id=%s", (account_id, export_id),
                ).fetchone()
                if row is None:
                    raise ApiError("EXPORT_NOT_FOUND", "导出记录不存在", 404)
                return row

        row = await asyncio.to_thread(read)
        if kind == "json":
            return JSONResponse(row["json_payload"], headers={
                "Content-Disposition": f'attachment; filename="growth-{export_id}.json"',
            })
        return PlainTextResponse(row["markdown_text"], media_type="text/markdown",
                                 headers={
                                     "Content-Disposition":
                                     f'attachment; filename="growth-{export_id}.md"',
                                 })

    async def features(request: Request) -> JSONResponse:
        return await write_endpoint(request, FeatureCreate, _create_feature)

    async def snapshots(request: Request) -> JSONResponse:
        return await write_endpoint(request, SnapshotCreate, _save_snapshot)

    async def source_excerpts(request: Request) -> JSONResponse:
        return await write_endpoint(request, SourceExcerptCreate, _save_source_excerpt)

    async def analysis_results(request: Request) -> JSONResponse:
        return await write_endpoint(request, AnalysisResult, _save_analysis_result)

    async def module_map(request: Request) -> JSONResponse:
        return await write_endpoint(request, ModuleMap, _map_module)

    async def module_edit(request: Request) -> JSONResponse:
        module_id = request.path_params["module_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: ModuleEdit,
        ) -> tuple[int, dict[str, Any]]:
            return _edit_module(conn, account, trace, module_id, body)

        return await write_endpoint(request, ModuleEdit, handle)

    async def modules_for_project(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        project_id = request.path_params["project_id"]
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read_modules() -> list[dict[str, Any]]:
            with connect(settings.database_url) as conn:
                project = conn.execute(
                    "SELECT 1 FROM growth_projects WHERE account_id=%s AND project_id=%s "
                    "AND deleted_at IS NULL", (account_id, project_id),
                ).fetchone()
                if project is None:
                    raise ApiError("PROJECT_NOT_FOUND", "项目不存在", 404)
                rows = conn.execute(
                    "SELECT * FROM growth_modules WHERE account_id=%s AND project_id=%s "
                    "ORDER BY module_id", (account_id, project_id),
                ).fetchall()
                return json.loads(json.dumps(rows, default=str))

        return JSONResponse({"modules": await asyncio.to_thread(read_modules)})

    async def module_opportunities(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        module_id = request.path_params["module_id"]
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read_opportunities() -> list[dict[str, Any]]:
            with connect(settings.database_url) as conn:
                module = conn.execute(
                    "SELECT snapshot_id, member_paths FROM growth_modules "
                    "WHERE account_id=%s AND module_id=%s",
                    (account_id, module_id),
                ).fetchone()
                if module is None:
                    raise ApiError("MODULE_NOT_FOUND", "模块不存在", 404)
                refs = conn.execute(
                    "SELECT source_ref_id, relative_path FROM growth_source_refs "
                    "WHERE account_id=%s AND snapshot_id=%s",
                    (account_id, module["snapshot_id"]),
                ).fetchall()
                grounded = {str(ref["source_ref_id"]) for ref in refs
                            if ref["relative_path"] in module["member_paths"]}
                rows = conn.execute(
                    "SELECT o.opportunity_id, o.title, o.reason, o.learning_goal, "
                    "o.source_refs, o.estimated_minutes, o.uncertainties "
                    "FROM growth_opportunities o JOIN growth_analysis_runs a "
                    "ON a.account_id=o.account_id AND a.analysis_id=o.analysis_id "
                    "WHERE o.account_id=%s AND a.snapshot_id=%s ORDER BY o.ordinal",
                    (account_id, module["snapshot_id"]),
                ).fetchall()
                matching = [row for row in rows if grounded & set(row["source_refs"])]
                return json.loads(json.dumps(matching, default=str))

        return JSONResponse({"opportunities": await asyncio.to_thread(read_opportunities)})

    async def topic_decision(request: Request) -> JSONResponse:
        proposal_id = request.path_params["proposal_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID, body: TopicDecision
        ) -> tuple[int, dict[str, Any]]:
            return _decide_topic(conn, account, trace, proposal_id, body)

        return await write_endpoint(request, TopicDecision, handle)

    async def tasks(request: Request) -> JSONResponse:
        return await write_endpoint(request, TaskCreate, _create_task)

    async def task_progress(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]

        def handle(
            conn: psycopg.Connection[Any],
            account: uuid.UUID,
            trace: uuid.UUID,
            body: TaskProgressUpdate,
        ) -> tuple[int, dict[str, Any]]:
            return _set_task_progress(conn, account, trace, task_id, body)

        return await write_endpoint(request, TaskProgressUpdate, handle)

    async def task_journey(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        task_id = request.path_params["task_id"]
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read() -> dict[str, Any]:
            with connect(settings.database_url) as conn:
                task = conn.execute(
                    "SELECT task_id, task_id AS card_id, question, learning_goal, "
                    "back_answer, back_explanation, card_version, source_commit_shas, "
                    "progress, revision, topic_id "
                    "FROM growth_tasks WHERE account_id=%s AND task_id=%s "
                    "AND deleted_at IS NULL", (account_id, task_id),
                ).fetchone()
                if task is None:
                    raise ApiError("TASK_NOT_FOUND", "学习任务不存在", 404)
                timeline = conn.execute(
                    "SELECT e.evidence_id, e.source_type, e.source_id, "
                    "e.evidence_label, e.actor_origin, e.created_at, "
                    "CASE WHEN e.source_type='attempt' THEN a.answer_text "
                    "ELSE n.content_text END AS content_text "
                    "FROM growth_timeline e LEFT JOIN growth_learning_attempts a "
                    "ON a.account_id=e.account_id AND a.attempt_id=e.source_id "
                    "AND e.source_type='attempt' LEFT JOIN growth_notes n "
                    "ON n.account_id=e.account_id AND n.note_id=e.source_id "
                    "AND e.source_type='note' WHERE e.account_id=%s AND e.task_id=%s "
                    "ORDER BY e.created_at,e.evidence_id", (account_id, task_id),
                ).fetchall()
                progress = conn.execute(
                    "SELECT event_type,payload,committed_at FROM growth_change_feed "
                    "WHERE account_id=%s AND entity_type='task' AND entity_id=%s "
                    "AND event_type='task_progress_changed' "
                    "ORDER BY change_seq", (account_id, task_id),
                ).fetchall()
                review = conn.execute(
                    "SELECT due_at,status,revision FROM growth_review_schedules "
                    "WHERE account_id=%s AND task_id=%s", (account_id, task_id),
                ).fetchone()
                memories = conn.execute(
                    "SELECT m.source_type,m.source_id,m.source_version,m.reme_path "
                    "FROM growth_memory_current m JOIN growth_timeline e "
                    "ON e.account_id=m.account_id AND e.source_type=m.source_type "
                    "AND e.source_id=m.source_id WHERE e.account_id=%s AND e.task_id=%s "
                    "AND m.deleted_at IS NULL ORDER BY m.source_type,m.source_id",
                    (account_id, task_id),
                ).fetchall()
                return json.loads(json.dumps({
                    "task": task, "timeline": timeline, "progress_events": progress,
                    "next_question": {"question": task["question"], **review}
                    if review and review["status"] == "active" else None,
                    "memory_refs": memories,
                }, default=str))

        return JSONResponse(await asyncio.to_thread(read))

    async def attempts(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID, body: AttemptCreate
        ) -> tuple[int, dict[str, Any]]:
            return _save_attempt(conn, account, trace, task_id, body)

        return await write_endpoint(request, AttemptCreate, handle)

    async def task_draft_update(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID,
            trace: uuid.UUID, body: TaskDraftUpdate,
        ) -> tuple[int, dict[str, Any]]:
            return _save_task_draft(conn, account, trace, task_id, body)

        return await write_endpoint(request, TaskDraftUpdate, handle)

    async def task_draft_read(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)
        task_id = request.path_params["task_id"]

        def read() -> dict[str, Any]:
            with connect(settings.database_url) as conn:
                row = conn.execute(
                    "SELECT task_id, revision, content_text, submitted_attempt_id, "
                    "updated_at FROM growth_task_drafts "
                    "WHERE account_id=%s AND task_id=%s",
                    (account_id, task_id),
                ).fetchone()
                return json.loads(json.dumps({"draft": row}, default=str))

        return JSONResponse(await asyncio.to_thread(read))

    async def feedback(request: Request) -> JSONResponse:
        attempt_id = request.path_params["attempt_id"]

        def handle(
            conn: psycopg.Connection[Any],
            account: uuid.UUID,
            trace: uuid.UUID,
            body: AttemptFeedback,
        ) -> tuple[int, dict[str, Any]]:
            return _save_feedback(conn, account, trace, attempt_id, body)

        return await write_endpoint(request, AttemptFeedback, handle)

    async def notes(request: Request) -> JSONResponse:
        return await write_endpoint(request, NoteCreate, _save_note)

    async def note_revisions(request: Request) -> JSONResponse:
        note_id = request.path_params["note_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: NoteRevision,
        ) -> tuple[int, dict[str, Any]]:
            return _note_revision(conn, account, trace, note_id, body)

        return await write_endpoint(request, NoteRevision, handle)

    async def note_resolve(request: Request) -> JSONResponse:
        note_id = request.path_params["note_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: NoteResolve,
        ) -> tuple[int, dict[str, Any]]:
            return _resolve_note(conn, account, trace, note_id, body)

        return await write_endpoint(request, NoteResolve, handle)

    async def note_delete(request: Request) -> JSONResponse:
        note_id = request.path_params["note_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: NoteDelete,
        ) -> tuple[int, dict[str, Any]]:
            return _delete_note(conn, account, trace, note_id, body)

        return await write_endpoint(request, NoteDelete, handle)

    async def knowledge_bundles(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)

        def read() -> list[dict[str, Any]]:
            with connect(settings.database_url) as conn:
                device = conn.execute(
                    "SELECT revoked_at FROM growth_devices WHERE account_id=%s AND device_id=%s",
                    (account_id, device_id),
                ).fetchone()
                if device is None or device["revoked_at"] is not None:
                    raise ApiError("DEVICE_REVOKED", "设备未注册或已撤销", 403)
                return conn.execute(
                    "SELECT card_id,revision,project_ids,bundle,payload_sha256 "
                    "FROM growth_knowledge_bundles WHERE account_id=%s ORDER BY card_id",
                    (account_id,),
                ).fetchall()

        return JSONResponse(json.loads(json.dumps(
            {"bundles": await asyncio.to_thread(read)}, default=str,
        )))

    async def knowledge_bundle_upsert(request: Request) -> JSONResponse:
        card_id = request.path_params["card_id"]

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: KnowledgeBundleUpsert,
        ) -> tuple[int, dict[str, Any]]:
            return _save_knowledge_bundle(conn, account, trace, card_id, body)

        return await write_endpoint(request, KnowledgeBundleUpsert, handle)

    async def sync_bootstrap(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        response = await asyncio.to_thread(
            _sync_read, settings, account_id, device_id
        )
        return JSONResponse(response)

    async def sync_changes(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        try:
            after = int(request.query_params.get("after", "0"))
            until_text = request.query_params.get("until")
            until = int(until_text) if until_text is not None else None
            limit = int(request.query_params.get("limit", "100"))
        except ValueError as exc:
            raise ApiError("INVALID_CURSOR", "同步游标必须为整数", 422) from exc
        if not 1 <= limit <= 500:
            raise ApiError("INVALID_PAGE_SIZE", "分页大小必须在 1 到 500 之间", 422)
        response = await asyncio.to_thread(
            _sync_read, settings, account_id, device_id,
            after=after, until=until, limit=limit,
        )
        return JSONResponse(response)

    async def sync_operations(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        try:
            raw = await request.json()
        except ValueError as exc:
            raise ApiError("INVALID_JSON", "请求正文不是 JSON", 400) from exc
        try:
            batch = SyncOperationsBatch.model_validate(raw)
        except ValidationError as exc:
            raise ApiError("INVALID_REQUEST", "批量同步字段不符合接口契约", 422) from exc

        def write_batch() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            for item in batch.operations:
                try:
                    body, apply = _sync_batch_operation(item)
                    status, response = _write_operation(
                        settings, account_id=account_id, device_id=device_id,
                        trace_id=request.state.trace_id,
                        operation_id=body.operation_id, endpoint=item.path,
                        payload=body.model_dump(mode="json"), apply=apply,
                    )
                    operation_id = str(body.operation_id)
                except ApiError as exc:
                    status = exc.status
                    response = {"code": exc.code, "message": exc.message,
                                "trace_id": str(request.state.trace_id),
                                "retryable": exc.retryable}
                    operation_id = str(item.payload.get("operation_id", ""))
                results.append({"operation_id": operation_id,
                                "status": status, "response": response})
                if status >= 400:
                    break
            return {"results": results,
                    "stopped": len(results) < len(batch.operations)}

        return JSONResponse(await asyncio.to_thread(write_batch))

    async def growth_context(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        if settings.reme_url is None:
            raise ApiError("REME_UNAVAILABLE", "长期记忆服务尚未配置", 503, retryable=True)
        query = request.query_params.get("query", "").strip()
        if not query or len(query) > 500:
            raise ApiError("INVALID_QUERY", "检索词长度必须在 1 到 500 字符之间", 422)
        try:
            limit = int(request.query_params.get("limit", "10"))
        except ValueError as exc:
            raise ApiError("INVALID_PAGE_SIZE", "检索数量必须为整数", 422) from exc
        if not 1 <= limit <= 20:
            raise ApiError("INVALID_PAGE_SIZE", "检索数量必须在 1 到 20 之间", 422)
        # Check device before contacting a shared ReMe workspace.
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)
        try:
            async with httpx.AsyncClient(base_url=settings.reme_url, timeout=20) as client:
                paths = await ReMeAdapter(client).search_paths(query, limit=limit)
        except (httpx.HTTPError, RuntimeError) as exc:
            raise ApiError("REME_UNAVAILABLE", "长期记忆服务暂时不可用", 503,
                           retryable=True) from exc
        notes = await asyncio.to_thread(
            current_notes_for_paths, settings.database_url, account_id, paths
        )
        attempts = await asyncio.to_thread(
            current_attempts_for_paths, settings.database_url, account_id, paths
        )
        linked_attempts = await asyncio.to_thread(
            linked_objective_answers_for_notes,
            settings.database_url, account_id, notes,
        )
        attempts = linked_attempts + attempts

        def read_topics() -> list[dict[str, Any]]:
            with connect(settings.database_url) as conn:
                rows = conn.execute(
                    "SELECT t.topic_id, t.title, t.status, t.parent_topic_id, "
                    "t.revision FROM growth_topics t LEFT JOIN growth_topic_aliases a "
                    "ON a.account_id=t.account_id AND a.alias_topic_id=t.topic_id "
                    "WHERE t.account_id=%s AND t.deleted_at IS NULL "
                    "AND a.alias_topic_id IS NULL ORDER BY t.title, t.topic_id",
                    (account_id,),
                ).fetchall()
                return json.loads(json.dumps(rows, default=str))

        topics = await asyncio.to_thread(read_topics)
        return JSONResponse({"topics": topics, "notes": notes, "attempts": attempts,
                             "memory_status": "current"})

    def learning_clock(request: Request) -> datetime:
        raw = request.headers.get("X-Learning-Clock")
        if raw is None:
            return datetime.now(UTC)
        if not settings.acceptance_mode:
            raise ApiError("CLOCK_OVERRIDE_FORBIDDEN", "学习时钟仅供验收运行注入", 403)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError("INVALID_LEARNING_CLOCK", "学习时钟格式无效", 422) from exc
        if parsed.tzinfo is None:
            raise ApiError("INVALID_LEARNING_CLOCK", "学习时钟必须包含时区", 422)
        return parsed.astimezone(UTC)

    async def reviews_due(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        device_id = _device_id(request)
        clock = learning_clock(request)
        await asyncio.to_thread(_sync_read, settings, account_id, device_id,
                                after=0, until=0, limit=1)

        def read_due() -> list[dict[str, Any]]:
            with connect(settings.database_url) as conn:
                rows = conn.execute(
                    "SELECT s.task_id, s.task_id AS card_id, s.due_at, s.revision, "
                    "s.last_hint_level, t.question, t.question AS front_question, "
                    "t.learning_goal, t.back_answer, t.back_explanation, "
                    "t.topic_id, t.progress, snapshot.project_id "
                    "FROM growth_review_schedules AS s "
                    "JOIN growth_tasks AS t ON t.account_id=s.account_id "
                    "AND t.task_id=s.task_id "
                    "JOIN growth_opportunities AS opportunity "
                    "ON opportunity.account_id=t.account_id "
                    "AND opportunity.opportunity_id=t.opportunity_id "
                    "JOIN growth_analysis_runs AS analysis "
                    "ON analysis.account_id=opportunity.account_id "
                    "AND analysis.analysis_id=opportunity.analysis_id "
                    "JOIN growth_snapshots AS snapshot "
                    "ON snapshot.account_id=analysis.account_id "
                    "AND snapshot.snapshot_id=analysis.snapshot_id "
                    "WHERE s.account_id=%s "
                    "AND s.status='active' AND t.deleted_at IS NULL "
                    "AND s.due_at<=%s ORDER BY s.due_at, s.task_id LIMIT 100",
                    (account_id, clock),
                ).fetchall()
                return json.loads(json.dumps(rows, default=str))

        return JSONResponse({"due": await asyncio.to_thread(read_due)})

    async def task_reviews(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]
        clock = learning_clock(request)

        def handle(
            conn: psycopg.Connection[Any], account: uuid.UUID, trace: uuid.UUID,
            body: ReviewSubmit,
        ) -> tuple[int, dict[str, Any]]:
            return _submit_review(conn, account, trace, task_id, body, clock)

        return await write_endpoint(request, ReviewSubmit, handle)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def current_account(request: Request) -> JSONResponse:
        account_id = await _account_id(request, settings)
        return JSONResponse({"account_id": str(account_id)})

    app = Starlette(
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/v1/account", current_account, methods=["GET"]),
            Route("/v1/devices", devices, methods=["POST"]),
            Route("/v1/devices/{device_id:uuid}/revoke", device_revoke,
                  methods=["POST"]),
            Route("/v1/sync/ack", device_ack, methods=["POST"]),
            Route("/v1/projects", projects, methods=["POST"]),
            Route("/v1/topics", topics_create, methods=["POST"]),
            Route("/v1/topics", topics_list, methods=["GET"]),
            Route("/v1/topics/{topic_id:uuid}", topic_read, methods=["GET"]),
            Route("/v1/topics/{topic_id:uuid}", topic_update, methods=["PATCH"]),
            Route("/v1/topics/{topic_id:uuid}/merge", topic_merge, methods=["POST"]),
            Route("/v1/topics/{topic_id:uuid}/split", topic_split, methods=["POST"]),
            Route("/v1/chats", chats_start, methods=["POST"]),
            Route("/v1/chats", chats_list, methods=["GET"]),
            Route("/v1/chats/{session_id:uuid}", chat_read, methods=["GET"]),
            Route("/v1/chats/{session_id:uuid}/messages", chat_user_message,
                  methods=["POST"]),
            Route("/v1/chats/{session_id:uuid}/assistant-replay", chat_assistant_replay,
                  methods=["POST"]),
            Route("/v1/chats/{session_id:uuid}/assistant", chat_assistant_live,
                  methods=["POST"]),
            Route("/v1/chats/{session_id:uuid}/suggestions/{suggestion_id:uuid}/decision",
                  chat_suggestion_decision, methods=["POST"]),
            Route("/v1/development-events", development_events, methods=["POST"]),
            Route("/v1/development-events/inbox", development_inbox, methods=["GET"]),
            Route("/v1/development-events/{event_id:uuid}/claim",
                  development_event_claim, methods=["POST"]),
            Route("/v1/exports", exports_create, methods=["POST"]),
            Route("/v1/exports/{export_id:uuid}", export_read, methods=["GET"]),
            Route("/v1/features", features, methods=["POST"]),
            Route("/v1/snapshots", snapshots, methods=["POST"]),
            Route("/v1/source-excerpts", source_excerpts, methods=["POST"]),
            Route("/v1/analysis-results", analysis_results, methods=["POST"]),
            Route("/v1/modules", module_map, methods=["POST"]),
            Route("/v1/modules/{module_id:uuid}", module_edit, methods=["PATCH"]),
            Route("/v1/projects/{project_id:uuid}/modules", modules_for_project,
                  methods=["GET"]),
            Route("/v1/modules/{module_id:uuid}/opportunities", module_opportunities,
                  methods=["GET"]),
            Route(
                "/v1/topic-proposals/{proposal_id:uuid}/decision",
                topic_decision,
                methods=["POST"],
            ),
            Route("/v1/tasks", tasks, methods=["POST"]),
            Route("/v1/tasks/{task_id:uuid}/progress", task_progress, methods=["PATCH"]),
            Route("/v1/tasks/{task_id:uuid}/journey", task_journey, methods=["GET"]),
            Route("/v1/tasks/{task_id:uuid}/attempts", attempts, methods=["POST"]),
            Route("/v1/tasks/{task_id:uuid}/draft", task_draft_update, methods=["PUT"]),
            Route("/v1/tasks/{task_id:uuid}/draft", task_draft_read, methods=["GET"]),
            Route("/v1/attempts/{attempt_id:uuid}/feedback", feedback, methods=["POST"]),
            Route("/v1/notes", notes, methods=["POST"]),
            Route("/v1/notes/{note_id:uuid}/revisions", note_revisions, methods=["PUT"]),
            Route("/v1/notes/{note_id:uuid}/resolve", note_resolve, methods=["POST"]),
            Route("/v1/notes/{note_id:uuid}", note_delete, methods=["DELETE"]),
            Route("/v1/sync/bootstrap", sync_bootstrap, methods=["GET"]),
            Route("/v1/sync/changes", sync_changes, methods=["GET"]),
            Route("/v1/sync/operations", sync_operations, methods=["POST"]),
            Route("/v1/knowledge/bundles", knowledge_bundles, methods=["GET"]),
            Route("/v1/knowledge/bundles/{card_id:uuid}", knowledge_bundle_upsert,
                  methods=["PUT"]),
            Route("/v1/growth/context", growth_context, methods=["GET"]),
            Route("/v1/reviews/due", reviews_due, methods=["GET"]),
            Route("/v1/tasks/{task_id:uuid}/reviews", task_reviews, methods=["POST"]),
        ],
        exception_handlers={ApiError: api_error},
    )

    async def trace_request(request: Request, call_next: Callable[..., Any]) -> Any:
        raw = request.headers.get("X-Trace-Id")
        try:
            request.state.trace_id = uuid.UUID(raw) if raw else uuid.uuid4()
        except ValueError:
            request.state.trace_id = uuid.uuid4()
        response = await call_next(request)
        response.headers["X-Trace-Id"] = str(request.state.trace_id)
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=trace_request)
    return app
