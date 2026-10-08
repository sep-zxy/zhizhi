"""Desktop topic actions preserve account scope and the user's explicit assignments."""

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


def test_topic_sidecar_forwards_explicit_revisions_and_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id, device_id, source_id, target_id = (uuid.uuid4() for _ in range(4))
    note_id, child_a, child_b = (uuid.uuid4() for _ in range(3))
    state = ServeState(state_dir=tmp_path / ".ahadiff", token="local-token")
    with GrowthLocalRepository(state.state_dir / "growth.sqlite") as ledger:
        GrowthSyncStore(ledger, account_id, device_id,
                        cloud_origin="http://127.0.0.1:9001")
        ledger.connection.execute(
            "UPDATE sync_accounts SET bootstrapped=1 WHERE account_id=?",
            (str(account_id),),
        )
        ledger.connection.commit()

    remote: list[tuple[str, str, dict[str, Any] | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer cloud-token"
        if request.url.path == "/v1/account":
            return httpx.Response(200, json={"account_id": str(account_id)})
        assert request.headers["x-device-id"] == str(device_id)
        body = json.loads(request.read()) if request.method != "GET" else None
        remote.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"ok": True})

    original_client = httpx.AsyncClient

    def mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original_client(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    client = TestClient(create_app(state), base_url="http://localhost:8765")
    headers = {"Origin": "http://localhost:8765", "X-AhaDiff-Token": "local-token"}
    access = {"cloud_url": "http://127.0.0.1:9001", "access_token": "cloud-token",
              "account_id": str(account_id)}
    base = "/api/growth/local/cloud/topics"

    assert client.post(base, headers=headers, json=access).status_code == 200
    assert client.post(f"{base}/{source_id}", headers=headers, json=access).status_code == 200
    create = {**access, "operation_id": str(uuid.uuid4()),
              "topic_id": str(source_id), "title": "旧主题"}
    assert client.post(f"{base}/create", headers=headers, json=create).status_code == 200
    note = {**access, "operation_id": str(uuid.uuid4()),
            "note_id": str(note_id), "content_text": "我对认证的原始理解"}
    assert client.post(f"{base}/{source_id}/notes", headers=headers,
                       json=note).status_code == 200
    update = {**access, "operation_id": str(uuid.uuid4()),
              "base_revision": 3, "title": "旧主题", "status": "archived"}
    assert client.post(f"{base}/{source_id}/update", headers=headers,
                       json=update).status_code == 200
    merge = {**access, "operation_id": str(uuid.uuid4()),
             "target_topic_id": str(target_id), "source_base_revision": 2,
             "target_base_revision": 4}
    assert client.post(f"{base}/{source_id}/merge", headers=headers,
                       json=merge).status_code == 200
    split = {**access, "operation_id": str(uuid.uuid4()), "base_revision": 5,
             "children": [{"topic_id": str(child_a), "title": "认证",
                           "note_ids": [str(note_id)], "task_ids": []},
                          {"topic_id": str(child_b), "title": "授权",
                           "note_ids": [], "task_ids": []}]}
    assert client.post(f"{base}/{target_id}/split", headers=headers,
                       json=split).status_code == 200
    assert [(method, path) for method, path, _ in remote] == [
        ("GET", "/v1/topics"), ("GET", f"/v1/topics/{source_id}"),
        ("POST", "/v1/topics"), ("POST", "/v1/notes"),
        ("PATCH", f"/v1/topics/{source_id}"),
        ("POST", f"/v1/topics/{source_id}/merge"),
        ("POST", f"/v1/topics/{target_id}/split"),
    ]
    assert remote[2][2] == {"operation_id": create["operation_id"],
                            "topic_id": str(source_id), "title": "旧主题"}
    assert remote[3][2]["topic_id"] == str(source_id)
    assert remote[4][2]["base_revision"] == 3
    assert remote[5][2]["source_base_revision"] == 2
    assert remote[5][2]["target_base_revision"] == 4
    assert remote[6][2]["children"][0]["note_ids"] == [str(note_id)]
    assert all("access_token" not in body for _, _, body in remote if body)
    assert client.post(base, headers=headers, json=access | {
        "cloud_url": "http://127.0.0.1:9002",
    }).status_code == 502


def test_local_topic_proposal_reuses_synced_cloud_topic(tmp_path: Path) -> None:
    state = ServeState(state_dir=tmp_path / ".ahadiff", token="local-token")
    project, feature, snapshot, analysis, opportunity, proposal = (
        str(uuid.uuid4()) for _ in range(6)
    )
    account, device, topic = (str(uuid.uuid4()) for _ in range(3))
    created = "2026-01-01T00:00:00+00:00"
    with GrowthLocalRepository(state.state_dir / "growth.sqlite") as ledger:
        GrowthSyncStore(ledger, uuid.UUID(account), uuid.UUID(device))
        with ledger.connection:
            ledger.connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?)", (project, "测试项目", created),
            )
            ledger.connection.execute(
                "INSERT INTO project_policies(project_id, cloud_allowed, cloud_account_id, "
                "cloud_origin, updated_at) VALUES (?, 1, ?, ?, ?)",
                (project, account, "http://127.0.0.1:9001", created),
            )
            ledger.connection.execute(
                "INSERT INTO features VALUES (?, ?, ?, ?, ?, ?, ?)",
                (feature, project, "认证模块", "main", "a" * 40, "active", created),
            )
            ledger.connection.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot, feature, "a" * 40, "b" * 40, "c" * 64, "d" * 64,
                 "{}", "v1", "", created),
            )
            ledger.connection.execute(
                "INSERT INTO analysis_runs(analysis_id, snapshot_id, request_id, "
                "input_fingerprint, mode, status, upstream_run_id, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (analysis, snapshot, str(uuid.uuid4()), "e" * 64, "dry_run",
                 "completed", None, None, created),
            )
            ledger.connection.execute(
                "INSERT INTO opportunity_batches VALUES (?, ?, ?, ?, ?, ?, ?)",
                (analysis, "f" * 64, "replay", None, None, None, created),
            )
            ledger.connection.execute(
                "INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (opportunity, analysis, 0, "认证和授权", "源于代码", "区分概念",
                 "[]", 4, "[]", created),
            )
            ledger.connection.execute(
                "INSERT INTO topic_proposals VALUES (?, ?, ?, ?, 'pending', NULL, ?, NULL)",
                (proposal, opportunity, 0, "Web 认证与授权", created),
            )
            ledger.connection.execute(
                "UPDATE sync_accounts SET bootstrapped=1 WHERE account_id=?", (account,),
            )
            ledger.connection.execute(
                "INSERT INTO sync_entities VALUES (?, 'topic', ?, 1, ?, NULL)",
                (account, topic, json.dumps({"title": "Web 认证与授权",
                                             "status": "active"})),
            )
    client = TestClient(create_app(state), base_url="http://localhost:8765")
    headers = {"Origin": "http://localhost:8765", "X-AhaDiff-Token": "local-token"}
    path = f"/api/growth/local/topic-proposals/{proposal}/decision"
    body = {"decision": "confirm", "account_id": account, "existing_topic_id": topic}
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["topic_id"] == topic
    assert client.post(path, headers=headers, json=body).status_code == 200
    with GrowthLocalRepository(state.state_dir / "growth.sqlite") as ledger:
        topics = ledger.connection.execute(
            "SELECT topic_id, title FROM growth_topics",
        ).fetchall()
        assert [(row["topic_id"], row["title"]) for row in topics] == [
            (topic, "Web 认证与授权")
        ]
        event = ledger.connection.execute(
            "SELECT payload_json FROM growth_events WHERE entity_type='topic_proposal' "
            "AND entity_id=? AND event_type='topic_confirmed'", (proposal,),
        ).fetchone()
        assert json.loads(event["payload_json"])["reuse_existing"] is True
