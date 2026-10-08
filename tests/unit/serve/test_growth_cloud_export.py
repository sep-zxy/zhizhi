"""A desktop export must use the synced cloud identity and return both formats."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from starlette.testclient import TestClient

from ahadiff.growth.local import GrowthLocalRepository
from ahadiff.growth.sync import GrowthSyncStore
from ahadiff.serve import ServeState, create_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_export_requires_synced_origin_and_matching_token_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id, device_id = uuid.uuid4(), uuid.uuid4()
    state = ServeState(state_dir=tmp_path / ".ahadiff", token="local-token")
    with GrowthLocalRepository(state.state_dir / "growth.sqlite") as ledger:
        GrowthSyncStore(
            ledger, account_id, device_id, cloud_origin="http://127.0.0.1:9001",
        )
        ledger.connection.execute(
            "UPDATE sync_accounts SET bootstrapped=1 WHERE account_id=?",
            (str(account_id),),
        )
        ledger.connection.commit()

    seen: list[tuple[str, str]] = []
    operation_ids: list[str] = []
    exported = {"topics": [{"title": "登录机制"}], "notes": [{"content_text": "原始笔记"}]}
    identity = {"account_id": str(account_id)}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer cloud-token"
        if request.url.path == "/v1/account":
            return httpx.Response(200, json=identity)
        assert request.headers["x-device-id"] == str(device_id)
        if request.url.path == "/v1/exports" and request.method == "POST":
            posted = json.loads(request.read())
            assert posted["export_id"] == body["export_id"]
            operation_ids.append(posted["operation_id"])
            return httpx.Response(201, json={"export_id": posted["export_id"]})
        if request.url.params.get("format") == "json":
            return httpx.Response(200, json=exported)
        return httpx.Response(200, text="# 成长历史\n\n原始笔记\n")

    original_client = httpx.AsyncClient

    def mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original_client(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    client = TestClient(create_app(state), base_url="http://localhost:8765")
    headers = {"Origin": "http://localhost:8765", "X-AhaDiff-Token": "local-token"}
    body = {
        "cloud_url": "http://127.0.0.1:9001", "access_token": "cloud-token",
        "account_id": str(account_id), "export_id": str(uuid.uuid4()),
    }
    wrong_origin = client.post(
        "/api/growth/local/cloud/exports", headers=headers,
        json=body | {"cloud_url": "http://127.0.0.1:9002"},
    )
    assert wrong_origin.status_code == 409 and seen == []
    identity["account_id"] = str(uuid.uuid4())
    wrong_account = client.post("/api/growth/local/cloud/exports", headers=headers, json=body)
    assert wrong_account.status_code == 409
    assert seen == [("GET", "/v1/account")]
    identity["account_id"] = str(account_id)
    response = client.post("/api/growth/local/cloud/exports", headers=headers, json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["export_id"] == body["export_id"]
    assert "原始笔记" in payload["json_text"]
    assert "原始笔记" in payload["markdown_text"]
    assert seen[-4:] == [
        ("GET", "/v1/account"), ("POST", "/v1/exports"),
        ("GET", f"/v1/exports/{payload['export_id']}"),
        ("GET", f"/v1/exports/{payload['export_id']}"),
    ]
    assert client.post(
        "/api/growth/local/cloud/exports", headers=headers, json=body,
    ).status_code == 200
    assert operation_ids == [operation_ids[0], operation_ids[0]]
