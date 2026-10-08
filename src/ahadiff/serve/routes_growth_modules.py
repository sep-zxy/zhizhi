"""Desktop relay for account-scoped cloud module mapping and locking."""

from __future__ import annotations

import subprocess
import uuid  # noqa: TC003 - Pydantic resolves UUID annotations at runtime.
from typing import Any

import httpx
from anyio import to_thread
from starlette.responses import JSONResponse

from ahadiff.contracts import ErrorCode
from ahadiff.growth.cloud.models import (  # noqa: TC001 - Pydantic resolves nested models at runtime.
    ModuleEdit,
    ModuleMap,
)

from ._errors import error_response
from .auth import require_write_token, serve_state
from .routes_growth import _code_index, _CodeIndexInput, _parse
from .routes_growth_chat import _ChatAccess, _client, _remote_result


class _ModuleMapRequest(_ChatAccess):
    binding_id: uuid.UUID
    operation: ModuleMap


class _ModuleEditRequest(_ChatAccess):
    operation: ModuleEdit


async def _call(request: Any, model: type[_ChatAccess], method: str,
                path: str, *, operation: bool = False) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, model)
    if isinstance(body, JSONResponse):
        return body
    payload = None
    if operation:
        assert isinstance(body, _ModuleMapRequest | _ModuleEditRequest)
        payload = body.operation.model_dump(mode="json", by_alias=True)
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.request(method, path, headers=headers, json=payload)
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT,
                              "growth_module_cloud_failed", status=502)


async def growth_module_map(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ModuleMapRequest)
    if isinstance(body, JSONResponse):
        return body
    try:
        symbols = [node.symbol for node in body.operation.flow.nodes]
        current = await to_thread.run_sync(
            _code_index, serve_state(request), str(body.operation.snapshot_id),
            _CodeIndexInput(binding_id=body.binding_id, symbols=symbols),
        )
        if (
            current["index_revision"] != body.operation.index_revision
            or current["effective_tree_hash"] != body.operation.effective_tree_hash
            or current["flow"] != body.operation.flow.model_dump(mode="json", by_alias=True)
        ):
            return error_response(ErrorCode.INPUT_VALIDATION,
                                  "growth_module_index_mismatch", status=409)
    except RuntimeError as exc:
        status = 409 if str(exc) == "snapshot_stale" else 503
        return error_response(ErrorCode.INPUT_VALIDATION,
                              "growth_module_index_unavailable", status=status)
    except (ValueError, subprocess.CalledProcessError):
        return error_response(ErrorCode.INPUT_VALIDATION,
                              "growth_module_index_unavailable", status=422)
    try:
        async with _client(body, serve_state(request)) as (client, headers):
            response = await client.post(
                "/v1/modules", headers=headers,
                json=body.operation.model_dump(mode="json", by_alias=True),
            )
            return _remote_result(response)
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT,
                              "growth_module_cloud_failed", status=502)


async def growth_module_edit(request: Any) -> JSONResponse:
    return await _call(request, _ModuleEditRequest, "PATCH",
                       f"/v1/modules/{request.path_params['module_id']}",
                       operation=True)


async def growth_module_list(request: Any) -> JSONResponse:
    return await _call(request, _ChatAccess, "GET",
                       f"/v1/projects/{request.path_params['project_id']}/modules")
