"""Desktop operations for user-owned cloud topic maintenance."""

from __future__ import annotations

import uuid  # noqa: TC003 - Pydantic resolves UUID annotations at runtime.
from typing import Any, Literal

import httpx
from pydantic import Field
from starlette.responses import JSONResponse

from ahadiff.contracts import ErrorCode

from ._errors import error_response
from .auth import require_write_token, serve_state
from .routes_growth import _Input, _parse
from .routes_growth_chat import _ChatAccess, _client, _remote_result


class _TopicOperation(_ChatAccess):
    operation_id: uuid.UUID


class _TopicCreate(_TopicOperation):
    topic_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)


class _TopicNoteCreate(_TopicOperation):
    note_id: uuid.UUID
    task_id: uuid.UUID | None = None
    content_text: str = Field(min_length=1, max_length=100000)


class _TopicUpdate(_TopicOperation):
    base_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    status: Literal["active", "archived"]


class _TopicMerge(_TopicOperation):
    target_topic_id: uuid.UUID
    source_base_revision: int = Field(ge=1)
    target_base_revision: int = Field(ge=1)


class _TopicSplitChild(_Input):
    topic_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    note_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    task_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class _TopicSplit(_TopicOperation):
    base_revision: int = Field(ge=1)
    children: list[_TopicSplitChild] = Field(min_length=1, max_length=8)


async def _call(request: Any, model: type[_ChatAccess], method: str,
                path: str, fields: tuple[str, ...] = ()) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, model)
    if isinstance(body, JSONResponse):
        return body
    payload = body.model_dump(mode="json", include=set(fields)) if fields else None
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.request(method, path, headers=headers, json=payload)
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT,
                              "growth_topic_cloud_failed", status=502)


async def growth_topic_list(request: Any) -> JSONResponse:
    return await _call(request, _ChatAccess, "GET", "/v1/topics")


async def growth_topic_create(request: Any) -> JSONResponse:
    return await _call(request, _TopicCreate, "POST", "/v1/topics",
                       ("operation_id", "topic_id", "title"))


async def growth_topic_read(request: Any) -> JSONResponse:
    return await _call(request, _ChatAccess, "GET",
                       f"/v1/topics/{request.path_params['topic_id']}")


async def growth_topic_note_create(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _TopicNoteCreate)
    if isinstance(body, JSONResponse):
        return body
    topic_id = request.path_params["topic_id"]
    payload = {"operation_id": str(body.operation_id),
               "note_id": str(body.note_id), "topic_id": str(topic_id),
               "task_id": str(body.task_id) if body.task_id else None,
               "content_text": body.content_text}
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.post("/v1/notes", headers=headers, json=payload)
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT,
                              "growth_topic_cloud_failed", status=502)


async def growth_topic_update(request: Any) -> JSONResponse:
    return await _call(
        request, _TopicUpdate, "PATCH",
        f"/v1/topics/{request.path_params['topic_id']}",
        ("operation_id", "base_revision", "title", "status"),
    )


async def growth_topic_merge(request: Any) -> JSONResponse:
    return await _call(
        request, _TopicMerge, "POST",
        f"/v1/topics/{request.path_params['topic_id']}/merge",
        ("operation_id", "target_topic_id", "source_base_revision",
         "target_base_revision"),
    )


async def growth_topic_split(request: Any) -> JSONResponse:
    return await _call(
        request, _TopicSplit, "POST",
        f"/v1/topics/{request.path_params['topic_id']}/split",
        ("operation_id", "base_revision", "children"),
    )
