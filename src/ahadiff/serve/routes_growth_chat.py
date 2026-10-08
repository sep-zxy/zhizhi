"""Authenticated cloud chat and durable local model generation."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import httpx
from anyio import to_thread
from pydantic import Field, SecretStr
from starlette.responses import JSONResponse

from ahadiff.contracts import ErrorCode
from ahadiff.growth.chat_model import (
    ChatModelPreview,
    generate_chat_reply,
    prepare_chat_preview,
)
from ahadiff.growth.sync import GrowthSyncStore, connect_and_sync
from ahadiff.growth.model_feedback import (
    FeedbackPreview, _PROMPT, generate_feedback,
)
from ahadiff.growth.model_opportunities import _config_hash, _digest, _provider
from ahadiff.safety.redact import apply_redactions, scan_text_for_secrets

from ._errors import error_response
from .auth import require_write_token, serve_state
from .routes_growth import _cloud_origin, _Input, _ledger, _parse


class _ChatAccess(_Input):
    cloud_url: str = Field(min_length=8, max_length=2048)
    access_token: SecretStr
    account_id: uuid.UUID


class _ChatStart(_ChatAccess):
    session_id: uuid.UUID
    project_id: uuid.UUID | None = None


class _ChatMessage(_ChatAccess):
    message_id: uuid.UUID
    content_text: str = Field(min_length=1, max_length=20000)


class _ChatPreview(_ChatAccess):
    provider_name: str = Field(min_length=1, max_length=100)


class _ChatGenerate(_ChatPreview):
    request_id: uuid.UUID
    message_id: uuid.UUID
    approved_payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool


class _ChatDecision(_ChatAccess):
    decision: Literal["confirm", "reject"]


class _SyncedFeedbackPreview(_ChatAccess):
    provider_name: str = Field(min_length=1, max_length=100)


class _SyncedFeedbackSubmit(_SyncedFeedbackPreview):
    approval_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool


def _chat_decision_payload(
    account_id: uuid.UUID, suggestion_id: uuid.UUID, decision: Literal["confirm", "reject"],
) -> dict[str, str | None]:
    return {
        "operation_id": str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"growth-chat-decision:{account_id}:{suggestion_id}:{decision}",
        )),
        "decision": decision,
        "topic_id": str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"growth-chat-topic:{account_id}:{suggestion_id}",
        )) if decision == "confirm" else None,
    }


def _account_device(state: Any, body: _ChatAccess, origin: str) -> str:
    with _ledger(state) as ledger:
        row = ledger.connection.execute(
            "SELECT device_id, cloud_origin FROM sync_accounts "
            "WHERE account_id=? AND bootstrapped=1", (str(body.account_id),),
        ).fetchone()
    if row is None or row["cloud_origin"] != origin:
        raise ValueError("云账号或来源与本机已同步账号不一致")
    return str(row["device_id"])


@asynccontextmanager
async def _client(body: _ChatAccess, state: Any
                  ) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    origin = _cloud_origin(body.cloud_url)
    device_id = _account_device(state, body, origin)
    headers = {
        "Authorization": f"Bearer {body.access_token.get_secret_value()}",
        "X-Device-Id": device_id,
    }
    async with httpx.AsyncClient(
        base_url=origin, timeout=httpx.Timeout(120.0, connect=5.0),
        follow_redirects=False,
    ) as client:
        identity = await client.get("/v1/account", headers=headers)
        identity.raise_for_status()
        if identity.json().get("account_id") != str(body.account_id):
            raise ValueError("访问令牌与本机同步账号不一致")
        yield client, headers


def _remote_result(response: httpx.Response) -> JSONResponse:
    return JSONResponse(response.json(), status_code=response.status_code)


async def _forward(request: Any, model: type[_ChatAccess], method: str,
                   path: str, payload: dict[str, Any] | None = None) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, model)
    if isinstance(body, JSONResponse):
        return body
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.request(method, path, headers=headers, json=payload)
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT, "growth_chat_cloud_failed", status=502)


async def growth_synced_task_journey(request: Any) -> JSONResponse:
    return await _forward(
        request, _ChatAccess, "GET",
        f"/v1/tasks/{request.path_params['task_id']}/journey",
    )


async def growth_chat_list(request: Any) -> JSONResponse:
    return await _forward(request, _ChatAccess, "GET", "/v1/chats")


async def growth_chat_start(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ChatStart)
    if isinstance(body, JSONResponse):
        return body
    payload = {"operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"growth-chat-start:{body.account_id}:{body.session_id}")),
        "session_id": str(body.session_id),
        "project_id": str(body.project_id) if body.project_id else None}
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.post("/v1/chats", headers=headers, json=payload)
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT, "growth_chat_cloud_failed", status=502)


async def growth_chat_read(request: Any) -> JSONResponse:
    return await _forward(request, _ChatAccess, "GET",
                          f"/v1/chats/{request.path_params['session_id']}")


async def growth_chat_message(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ChatMessage)
    if isinstance(body, JSONResponse):
        return body
    session_id = request.path_params["session_id"]
    payload = {"operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"growth-chat-user:{body.account_id}:{body.message_id}")),
        "message_id": str(body.message_id), "content_text": body.content_text}
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.post(
                f"/v1/chats/{session_id}/messages", headers=headers, json=payload,
            )
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT, "growth_chat_cloud_failed", status=502)


async def _preview(request: Any, body: _ChatPreview) -> ChatModelPreview:
    state = serve_state(request)
    async with _client(body, state) as (client, headers):
        response = await client.get(
            f"/v1/chats/{request.path_params['session_id']}", headers=headers,
        )
        response.raise_for_status()
        chat = response.json()
        messages = chat.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("对话还没有用户消息")
        query = str(messages[-1].get("content_text", "")).strip()[:500]
        memory: dict[str, Any] = {"memory_status": "unavailable", "notes": [],
                                  "attempts": []}
        if query:
            context_response = await client.get(
                "/v1/growth/context", headers=headers,
                params={"query": query, "limit": 20},
            )
            if context_response.status_code == 200:
                memory = context_response.json()
            elif context_response.status_code != 503:
                context_response.raise_for_status()
    project_id = chat["session"].get("project_id")
    if project_id:
        with _ledger(state) as ledger:
            row = ledger.connection.execute(
                "SELECT model_allowed, cloud_allowed, cloud_account_id "
                "FROM project_policies WHERE project_id=?", (project_id,),
            ).fetchone()
        if row is None or not row["model_allowed"] or not row["cloud_allowed"] \
                or row["cloud_account_id"] != str(body.account_id):
            raise ValueError("项目未允许模型处理或云同步")
    return prepare_chat_preview(state.state_dir.parent, body.provider_name, chat, memory)


async def growth_chat_preview(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ChatPreview)
    if isinstance(body, JSONResponse):
        return body
    try:
        preview = await _preview(request, body)
    except (ValueError, httpx.HTTPError, KeyError, TypeError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_chat_preview_failed", status=409)
    return JSONResponse({
        "payload_text": preview.payload_text,
        "approval_hash": preview.approval_hash,
        "provider_host": preview.provider_host,
        "model_name": preview.model_name,
    })


def _existing_request(
    state: Any, body: _ChatGenerate, session_id: str,
) -> tuple[str, dict[str, Any] | None] | None:
    with _ledger(state) as ledger:
        db = ledger.connection
        row = db.execute(
            "SELECT * FROM local_chat_model_requests WHERE account_id=? AND request_id=?",
            (str(body.account_id), str(body.request_id)),
        ).fetchone()
        if row is None:
            return None
        if (row["session_id"] != session_id or row["message_id"] != str(body.message_id)
                or row["provider_name"] != body.provider_name
                or row["approval_hash"] != body.approved_payload_hash):
            raise ValueError("模型请求 ID 已用于不同内容")
        return str(row["status"]), json.loads(row["response_json"]) \
            if row["response_json"] else None


def _reserve(state: Any, body: _ChatGenerate, session_id: str) -> tuple[str, dict[str, Any] | None]:
    existing = _existing_request(state, body, session_id)
    if existing is not None:
        return existing
    with _ledger(state) as ledger:
        db = ledger.connection
        now = datetime.now(UTC).isoformat()
        with db:
            db.execute(
                "INSERT INTO local_chat_model_requests VALUES (?, ?, ?, ?, ?, ?, "
                "'running', NULL, NULL, ?, ?)",
                (str(body.account_id), session_id, str(body.request_id),
                 str(body.message_id), body.provider_name, body.approved_payload_hash,
                 now, now),
            )
        return "new", None


def _finish(state: Any, body: _ChatGenerate, session_id: str,
            payload: dict[str, Any] | None, project_id: str | None,
            error: str | None = None) -> None:
    with _ledger(state) as ledger:
        db = ledger.connection
        with db:
            db.execute(
                "UPDATE local_chat_model_requests SET status=?, response_json=?, "
                "error=?, updated_at=? WHERE account_id=? AND request_id=?",
                ("ready" if payload else "failed",
                 json.dumps(payload, ensure_ascii=False) if payload else None,
                 error, datetime.now(UTC).isoformat(), str(body.account_id),
                 str(body.request_id)),
            )
            if payload:
                account = db.execute(
                    "SELECT device_id, cloud_origin FROM sync_accounts WHERE account_id=?",
                    (str(body.account_id),),
                ).fetchone()
                assert account is not None
                store = GrowthSyncStore(
                    ledger, body.account_id, uuid.UUID(account["device_id"]),
                    cloud_origin=account["cloud_origin"],
                )
                store.queue_operation(
                    str(body.request_id), "POST", f"/v1/chats/{session_id}/assistant",
                    payload, None, None, "chat_message", str(body.message_id),
                )
                if project_id is not None:
                    db.execute(
                        "INSERT INTO sync_publication_links(account_id, operation_id, "
                        "project_id) VALUES (?, ?, ?)",
                        (str(body.account_id), str(body.request_id), project_id),
                    )


async def growth_chat_generate(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ChatGenerate)
    if isinstance(body, JSONResponse):
        return body
    if not body.approved:
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_chat_approval_required", status=422,
        )
    state = serve_state(request)
    session_id = str(request.path_params["session_id"])
    lock = state.write_lock
    assert lock is not None
    try:
        async with lock:
            existing = _existing_request(state, body, session_id)
        preview = None
        if existing is None:
            preview = await _preview(request, body)
            if preview.approval_hash != body.approved_payload_hash:
                raise ValueError("对话或模型配置已变化，请重新预览并批准")
        else:
            # A completed reply becomes the last chat message. Rebuilding the
            # preview then would reject a valid retry before it can replay.
            async with _client(body, state):
                pass
        async with lock:
            status, payload = _reserve(state, body, session_id)
        if status in {"running", "failed"}:
            raise ValueError("此请求的模型调用结果不确定或已失败，请检查后使用新请求 ID")
        if status == "new":
            assert preview is not None
            try:
                started = perf_counter()
                reply, response = await to_thread.run_sync(
                    generate_chat_reply, state.state_dir.parent, preview,
                )
                elapsed_ms = max(0, round((perf_counter() - started) * 1000))
                payload = {
                    "operation_id": str(body.request_id),
                    "message_id": str(body.message_id),
                    "content_text": reply.reply,
                    "suggestion": ({
                        "suggestion_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                            f"growth-chat-suggestion:{body.account_id}:{body.message_id}")),
                        "title": reply.suggestion.title,
                        "reason": reply.suggestion.reason,
                    } if reply.suggestion else None),
                    "provider_name": body.provider_name,
                    "model_name": response.model_id,
                    "provider_request_id": response.request_id,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "elapsed_ms": elapsed_ms,
                    "request_payload_hash": hashlib.sha256(
                        preview.payload_text.encode("utf-8")).hexdigest(),
                    "response_payload_hash": hashlib.sha256(
                        response.content.encode("utf-8")).hexdigest(),
                }
                async with lock:
                    _finish(state, body, session_id, payload, preview.project_id)
            except Exception as exc:
                async with lock:
                    _finish(state, body, session_id, None, preview.project_id,
                            type(exc).__name__)
                raise
        assert payload is not None
        try:
            origin = _cloud_origin(body.cloud_url)
            async with lock, httpx.AsyncClient(
                base_url=origin, timeout=20.0, follow_redirects=False,
            ) as client:
                with _ledger(state) as ledger:
                    sync = await connect_and_sync(
                        ledger, client, body.access_token.get_secret_value(),
                    )
                    row = ledger.connection.execute(
                        "SELECT state FROM sync_outbox WHERE account_id=? AND operation_id=?",
                        (str(body.account_id), str(body.request_id)),
                    ).fetchone()
            return JSONResponse({
                "message": payload, "sync_state": row["state"] if row else None,
                "account_id": sync["account_id"],
            })
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return JSONResponse({"message": payload, "sync_state": "pending"},
                                status_code=202)
    except (ValueError, httpx.HTTPError, KeyError, TypeError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_chat_generate_failed", status=409)
    except Exception:
        return error_response(ErrorCode.PROVIDER_TRANSPORT, "growth_chat_model_failed", status=502)


async def growth_chat_decision(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ChatDecision)
    if isinstance(body, JSONResponse):
        return body
    session_id = request.path_params["session_id"]
    suggestion_id = uuid.UUID(str(request.path_params["suggestion_id"]))
    payload = _chat_decision_payload(body.account_id, suggestion_id, body.decision)
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.post(
                f"/v1/chats/{session_id}/suggestions/{suggestion_id}/decision",
                headers=headers, json=payload,
            )
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT, "growth_chat_cloud_failed", status=502)


def _synced_feedback_preview(
    state: Any, body: _SyncedFeedbackPreview, attempt_id: str,
) -> FeedbackPreview:
    origin = _cloud_origin(body.cloud_url)
    device_id = _account_device(state, body, origin)
    with _ledger(state) as ledger:
        workspace = GrowthSyncStore(
            ledger, body.account_id, uuid.UUID(device_id),
        ).task_workspace()
    matching = [
        (task, attempt)
        for task in workspace for attempt in task["attempts"]
        if attempt["attempt_id"] == attempt_id
    ]
    if len(matching) != 1:
        raise ValueError("已同步回答不存在")
    task, attempt = matching[0]
    if task["deleted_at"] or attempt.get("status") == "feedback_ready":
        raise ValueError("回答已删除或已有反馈")
    excerpts = [
        source for source in task["approved_sources"]
        if source["approved_excerpt"] is not None
    ]
    if not excerpts:
        raise ValueError("这张卡片没有单独批准同步的源码片段")
    root = state.state_dir.parent
    config, _, _ = _provider(root, body.provider_name, require_api_key=False)
    code = [{
        "source_ref_id": source["source_ref_id"],
        "relative_path": source["relative_path"],
        "blob_hash": source["blob_hash"],
        "start_line": source["approved_excerpt"]["start_line"],
        "end_line": source["approved_excerpt"]["end_line"],
        "content_text": source["approved_excerpt"]["content_text"],
    } for source in excerpts]
    context = {
        "approved_code_context": code,
        "task_question": task["task"]["question"],
        "user_answer": attempt["answer_text"],
        "allowed_source_refs": [source["source_ref_id"] for source in excerpts],
    }
    raw = _PROMPT + json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    payload = apply_redactions(
        raw, scan_text_for_secrets(raw, source_name="growth_synced_feedback"),
    )
    if len(payload) > 60000 or any(
        item.blocked_remote and not item.allowlisted
        for item in scan_text_for_secrets(
            payload, source_name="growth_synced_feedback_redacted",
        )
    ):
        raise ValueError("反馈请求包含过长或敏感内容")
    config_hash = _config_hash(config)
    destination = urlsplit(config.base_url)
    return FeedbackPreview(
        attempt_id=attempt_id, binding_id="synced", provider_name=body.provider_name,
        provider_config_hash=config_hash,
        provider_host=f"{destination.scheme}://{destination.netloc}",
        model_name=config.model_name, selected_patch=json.dumps(
            code, ensure_ascii=False, sort_keys=True,
        ), payload_text=payload,
        approval_hash=_digest(payload + "\n" + config_hash),
        workspace_root=root,
    )


async def growth_synced_feedback_preview(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SyncedFeedbackPreview)
    if isinstance(body, JSONResponse):
        return body
    try:
        preview = await to_thread.run_sync(
            _synced_feedback_preview, serve_state(request), body,
            str(request.path_params["attempt_id"]),
        )
        return JSONResponse({
            "payload_text": preview.payload_text,
            "approval_hash": preview.approval_hash,
            "provider_host": preview.provider_host,
            "model_name": preview.model_name,
        })
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION,
                              "growth_feedback_preview_failed", status=409)


async def growth_synced_feedback_submit(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SyncedFeedbackSubmit)
    if isinstance(body, JSONResponse):
        return body
    if not body.approved:
        return error_response(ErrorCode.INPUT_VALIDATION,
                              "growth_model_approval_required", status=422)
    state = serve_state(request)
    attempt_id = str(request.path_params["attempt_id"])
    try:
        preview = await to_thread.run_sync(
            _synced_feedback_preview, state, body, attempt_id,
        )
        if preview.approval_hash != body.approval_hash:
            raise ValueError("反馈预览已变化")
        feedback, response = await to_thread.run_sync(generate_feedback, preview)
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"growth-synced-feedback:{body.account_id}:{attempt_id}:"
            f"{preview.approval_hash}",
        )
        async with _client(body, state) as (client, headers):
            saved = await client.post(
                f"/v1/attempts/{attempt_id}/feedback", headers=headers,
                json={
                    "operation_id": str(operation_id),
                    "feedback_origin": "live",
                    "feedback": feedback.model_dump(mode="json"),
                },
            )
            if saved.status_code != 200:
                return _remote_result(saved)
            lock = state.write_lock
            assert lock is not None
            async with lock:
                with _ledger(state) as ledger:
                    await connect_and_sync(
                        ledger, client, body.access_token.get_secret_value(),
                    )
        return JSONResponse({
            "attempt_id": attempt_id, "status": "feedback_ready",
            "feedback": feedback.model_dump(mode="json"),
            "provider_request_id": response.request_id,
        })
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.INPUT_VALIDATION,
                              "growth_feedback_not_ready", status=409)
    except Exception:
        return error_response(ErrorCode.PROVIDER_TRANSPORT,
                              "growth_feedback_model_failed", status=502)
