"""Authenticated desktop bridge for due growth reviews and user answers."""

from __future__ import annotations

import os
import json
import uuid  # noqa: TC003 - Pydantic resolves UUID annotations at runtime.
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import Field
from starlette.responses import JSONResponse

from ahadiff.contracts import ErrorCode
from ahadiff.growth.sync import GrowthSyncStore

from ._errors import error_response
from .auth import require_write_token, serve_state
from .routes_growth import _Input, _ledger, _parse
from .routes_growth_chat import _ChatAccess, _client, _remote_result


class _ReviewSubmit(_ChatAccess):
    operation_id: uuid.UUID
    review_id: uuid.UUID
    base_revision: int = Field(ge=1)
    answer_text: str = Field(min_length=1, max_length=20000)
    answer: Literal["easy", "good", "hard", "wrong"]
    hint_level: int = Field(ge=0, le=3)


class _LocalReviewSubmit(_Input):
    account_id: uuid.UUID
    device_id: uuid.UUID | None = None
    operation_id: uuid.UUID
    review_id: uuid.UUID
    base_revision: int = Field(ge=1)
    answer_text: str = Field(min_length=1, max_length=20000)
    answer: Literal["easy", "good", "hard", "wrong"]
    hint_level: int = Field(ge=0, le=3)


def _clock_headers(request: Any, headers: dict[str, str]) -> dict[str, str]:
    clock = request.headers.get("X-Learning-Clock")
    if clock and os.getenv("GROWTH_ACCEPTANCE_MODE") == "1":
        return {**headers, "X-Learning-Clock": clock}
    return headers


def _learning_clock(request: Any) -> datetime:
    raw = request.headers.get("X-Learning-Clock") if os.getenv("GROWTH_ACCEPTANCE_MODE") == "1" else None
    if raw:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("学习时钟必须包含时区")
        return value.astimezone(UTC)
    return datetime.now(UTC)


def _cached_due(state: Any, account_id: uuid.UUID, clock: datetime) -> dict[str, Any]:
    with _ledger(state) as ledger:
        account = ledger.connection.execute(
            "SELECT device_id,bootstrapped FROM sync_accounts WHERE account_id=?",
            (str(account_id),),
        ).fetchone()
        if account is None or not account["bootstrapped"]:
            raise ValueError("复习队列尚未同步到本机")
        store = GrowthSyncStore(ledger, account_id, uuid.UUID(account["device_id"]))
        due: list[dict[str, Any]] = []
        for row in ledger.connection.execute(
            "SELECT entity_id,revision,payload_json FROM sync_entities "
            "WHERE account_id=? AND entity_type='review_schedule' AND deleted_at IS NULL",
            (str(account_id),),
        ):
            schedule = json.loads(row["payload_json"])
            if schedule.get("status") != "active" or not schedule.get("due_at"):
                continue
            due_at = datetime.fromisoformat(str(schedule["due_at"]).replace("Z", "+00:00"))
            if due_at > clock:
                continue
            task_id = str(row["entity_id"])
            task = store.cached_entity("task", task_id)
            if not task or task.get("deleted_at"):
                continue
            pending = ledger.connection.execute(
                "SELECT 1 FROM sync_outbox WHERE account_id=? "
                "AND target_type='review_schedule' AND target_id=? "
                "AND state IN ('pending','conflict') LIMIT 1",
                (str(account_id), task_id),
            ).fetchone()
            if pending:
                continue
            due.append({
                "task_id": task_id, "card_id": task_id,
                "project_id": store._task_project_id(task_id),
                "question": task.get("question", ""),
                "front_question": task.get("question", ""),
                "learning_goal": task.get("learning_goal", ""),
                "back_answer": task.get("back_answer", ""),
                "back_explanation": task.get("back_explanation", ""),
                "topic_id": task.get("topic_id"), "progress": task.get("progress"),
                "due_at": schedule["due_at"], "revision": row["revision"],
                "last_hint_level": schedule.get("last_hint_level"),
            })
        due.sort(key=lambda item: (item["due_at"], item["card_id"]))
        return {"due": due[:100], "offline": True}


def _queue_local_review(
    state: Any, account_id: uuid.UUID, task_id: uuid.UUID,
    body: _ReviewSubmit | _LocalReviewSubmit,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        account = ledger.connection.execute(
            "SELECT device_id,bootstrapped FROM sync_accounts WHERE account_id=?",
            (str(account_id),),
        ).fetchone()
        if account is None or not account["bootstrapped"]:
            raise ValueError("复习答案无法离线保存：本机尚未同步账号")
        if isinstance(body, _LocalReviewSubmit) and body.device_id is not None and (
            str(body.device_id) != account["device_id"]
        ):
            raise ValueError("复习答案的设备身份与本机不同")
        store = GrowthSyncStore(ledger, account_id, uuid.UUID(account["device_id"]))
        operation_id = store.queue_review(
            task_id, operation_id=body.operation_id, review_id=body.review_id,
            base_revision=body.base_revision, answer_text=body.answer_text,
            answer=body.answer, hint_level=body.hint_level,
        )
        return {"card_id": str(task_id), "task_id": str(task_id),
                "review_id": str(body.review_id), "operation_id": operation_id,
                "sync_state": "pending"}


async def growth_local_review_due(request: Any) -> JSONResponse:
    require_write_token(request)
    try:
        account_id = uuid.UUID(request.query_params.get("account_id", ""))
        return JSONResponse(_cached_due(serve_state(request), account_id, _learning_clock(request)))
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_review_cache_unavailable", status=422)


async def growth_local_review_submit(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _LocalReviewSubmit)
    if isinstance(body, JSONResponse):
        return body
    try:
        return JSONResponse(_queue_local_review(
            serve_state(request), body.account_id,
            uuid.UUID(request.path_params["card_id"]), body,
        ), status_code=202)
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_review_queue_failed", status=409)


async def growth_review_due(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ChatAccess)
    if isinstance(body, JSONResponse):
        return body
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.get(
                "/v1/reviews/due", headers=_clock_headers(request, headers),
            )
            if response.status_code >= 500:
                return JSONResponse(_cached_due(
                    serve_state(request), body.account_id, _learning_clock(request)
                ))
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        try:
            return JSONResponse(_cached_due(
                serve_state(request), body.account_id, _learning_clock(request)
            ))
        except ValueError:
            return error_response(ErrorCode.PROVIDER_TRANSPORT,
                                  "growth_review_cloud_failed", status=502)


async def growth_review_submit(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ReviewSubmit)
    if isinstance(body, JSONResponse):
        return body
    task_id = request.path_params["task_id"]
    payload = body.model_dump(mode="json", include={
        "operation_id", "review_id", "base_revision", "answer_text",
        "answer", "hint_level",
    })
    payload["actor_origin"] = "user"
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.post(
                f"/v1/tasks/{task_id}/reviews",
                headers=_clock_headers(request, headers), json=payload,
            )
            if response.status_code >= 500:
                return JSONResponse(_queue_local_review(
                    serve_state(request), body.account_id, uuid.UUID(task_id), body
                ), status_code=202)
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        try:
            return JSONResponse(_queue_local_review(
                serve_state(request), body.account_id, uuid.UUID(task_id), body
            ), status_code=202)
        except ValueError:
            return error_response(ErrorCode.PROVIDER_TRANSPORT,
                                  "growth_review_cloud_failed", status=502)
