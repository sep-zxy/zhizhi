"""Explicit source approval survives sync to a device without a repository."""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from typing import TYPE_CHECKING

from starlette.testclient import TestClient

from ahadiff.growth.local import GrowthLocalRepository
from ahadiff.growth.sync import BOOTSTRAP_TYPES, GrowthSyncStore
from ahadiff.serve.app import create_app
from ahadiff.serve.state import ServeState

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_source_excerpt_requires_separate_approval_and_reads_without_binding(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "excerpt@example.invalid")
    _git(repo, "config", "user.name", "源码片段验收")
    (repo / "example.ts").write_text("export const answer = 1;\n", encoding="utf-8")
    _git(repo, "add", "example.ts")
    _git(repo, "commit", "-m", "建立基线")
    (repo / "example.ts").write_text("export const answer = 2;\n", encoding="utf-8")

    state_dir = tmp_path / "first" / ".ahadiff"
    state_dir.mkdir(parents=True)
    account_id, device_id = uuid.uuid4(), uuid.uuid4()
    with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
        project_id = ledger.create_project("片段验收", trace_id=str(uuid.uuid4()))
        binding_id = ledger.bind_repo(project_id, repo, trace_id=str(uuid.uuid4()))
        feature_id = ledger.start_feature(
            project_id, binding_id, "修改答案", "HEAD", trace_id=str(uuid.uuid4()),
        )
        snapshot_id = ledger.capture_snapshot(
            feature_id, binding_id, trace_id=str(uuid.uuid4()),
        )
        source = ledger.connection.execute(
            "SELECT source_ref_id, relative_path, blob_hash FROM source_refs "
            "WHERE snapshot_id=?", (snapshot_id,),
        ).fetchone()
        source_ref_id = str(source["source_ref_id"])
        store = GrowthSyncStore(
            ledger, account_id, device_id, cloud_origin="http://127.0.0.1:9001",
        )
        ledger.grant_cloud_sync(project_id, str(account_id), "http://127.0.0.1:9001")
        entities: dict[str, list[dict[str, object]]] = {
            name: [] for name in BOOTSTRAP_TYPES
        }
        opportunity_id, task_id = uuid.uuid4(), uuid.uuid4()
        entities["source_refs"] = [{
            "source_ref_id": source_ref_id, "snapshot_id": snapshot_id,
            "relative_path": str(source["relative_path"]),
            "blob_hash": str(source["blob_hash"]),
        }]
        entities["opportunities"] = [{
            "opportunity_id": str(opportunity_id), "source_refs": [source_ref_id],
        }]
        entities["tasks"] = [{
            "task_id": str(task_id), "opportunity_id": str(opportunity_id),
            "question": "answer 的值是什么？", "progress": "ready",
        }]
        store.apply_bootstrap({"high_watermark": 0, "entities": entities})
        before = store.task_workspace()[0]
        assert before["approved_sources"][0]["approved_excerpt"] is None
        assert "answer = 2" not in str(before)

    client = TestClient(
        create_app(ServeState(state_dir=state_dir, token="test-token")),
        base_url="http://localhost:8765",
    )
    headers = {"origin": "http://localhost:8765", "X-AhaDiff-Token": "test-token"}
    path = f"/api/growth/local/source-refs/{source_ref_id}/excerpt"
    preview = client.post(
        f"{path}/preview", headers=headers,
        json={"start_line": 1, "end_line": 1},
    )
    assert preview.status_code == 200, preview.text
    exact = preview.json()
    assert exact["content_text"].splitlines() == ["export const answer = 2;"]
    assert exact["relative_path"] == "example.ts"
    assert exact["content_hash"] == hashlib.sha256(
        exact["content_text"].encode(),
    ).hexdigest()
    unapproved = client.post(path, headers=headers, json={
        "account_id": str(account_id), "start_line": 1, "end_line": 1,
        "approval_hash": exact["approval_hash"],
    })
    assert unapproved.status_code == 422
    denied = client.post(path, headers=headers, json={
        "account_id": str(account_id), "start_line": 1, "end_line": 1,
        "approval_hash": "0" * 64, "approved": True,
    })
    assert denied.status_code == 409
    queued = client.post(path, headers=headers, json={
        "account_id": str(account_id), "start_line": 1, "end_line": 1,
        "approval_hash": exact["approval_hash"], "approved": True,
    })
    assert queued.status_code == 201, queued.text
    repeated = client.post(path, headers=headers, json={
        "account_id": str(account_id), "start_line": 1, "end_line": 1,
        "approval_hash": exact["approval_hash"], "approved": True,
    })
    assert repeated.status_code == 201
    assert repeated.json()["operation_id"] == queued.json()["operation_id"]
    with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
        row = ledger.connection.execute(
            "SELECT payload_json FROM sync_outbox WHERE account_id=? "
            "AND operation_id=?", (str(account_id), queued.json()["operation_id"]),
        ).fetchone()
        assert row is not None and "answer = 2" in row["payload_json"]

    second_dir = tmp_path / "second" / ".ahadiff"
    second_dir.mkdir(parents=True)
    with GrowthLocalRepository(second_dir / "growth.sqlite") as ledger:
        second = GrowthSyncStore(ledger, account_id, uuid.uuid4())
        entities["source_excerpts"] = [{
            "source_ref_id": source_ref_id, "snapshot_id": snapshot_id,
            "blob_hash": exact["blob_hash"], "excerpt_id": str(uuid.uuid4()),
            "start_line": 1, "end_line": 1,
            "content_text": exact["content_text"],
            "content_hash": exact["content_hash"],
        }]
        second.apply_bootstrap({"high_watermark": 1, "entities": entities})
        assert ledger.connection.execute("SELECT count(*) FROM bindings").fetchone()[0] == 0
        approved = second.task_workspace()[0]["approved_sources"][0]
        assert approved["approved_excerpt"]["content_text"] == exact["content_text"]
