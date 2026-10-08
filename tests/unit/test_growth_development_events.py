"""A cached cloud event cannot run against an unverified local repository."""

from __future__ import annotations

import subprocess
import uuid
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from ahadiff.growth.development_events import (
    capture_development_event,
    event_analysis_request,
    event_inbox,
    link_event_analysis,
)
from ahadiff.growth.local import GrowthLocalRepository
from ahadiff.growth.sync import BOOTSTRAP_TYPES, GrowthSyncStore
from ahadiff.serve import ServeState, create_app

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def test_targeted_event_requires_matching_head_and_reuses_capture(tmp_path: Path) -> None:
    repo = tmp_path / "document-demo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "情境验证")
    _git(repo, "config", "user.email", "scenario@example.invalid")
    source = repo / "document.ts"
    source.write_text("export const canEdit = false;\n", encoding="utf-8")
    _git(repo, "add", "document.ts")
    _git(repo, "commit", "-m", "建立权限模拟基线")
    _git(repo, "switch", "-c", "fix/document-owner-check")
    source.write_text("export const canEdit = true;\n", encoding="utf-8")
    _git(repo, "add", "document.ts")
    _git(repo, "commit", "-m", "加入所有者检查")
    head = _git(repo, "rev-parse", "HEAD")
    account_id, device_id, other_device = (uuid.uuid4() for _ in range(3))
    event_id, stale_id, foreign_target_id = (uuid.uuid4() for _ in range(3))
    with GrowthLocalRepository(tmp_path / "growth.sqlite") as ledger:
        assert ledger.connection.execute("PRAGMA user_version").fetchone()[0] == 15
        project_id = ledger.create_project("文档权限", trace_id=str(uuid.uuid4()))
        binding_id = ledger.bind_repo(project_id, repo, trace_id=str(uuid.uuid4()))
        feature_id = ledger.start_feature(
            project_id,
            binding_id,
            "所有者检查",
            "main",
            trace_id=str(uuid.uuid4()),
        )
        store = GrowthSyncStore(ledger, account_id, device_id, cloud_origin="http://127.0.0.1:9001")
        ledger.grant_cloud_sync(project_id, str(account_id), "http://127.0.0.1:9001")
        ledger.connection.commit()
        entities: dict[str, list[dict[str, object]]] = {name: [] for name in BOOTSTRAP_TYPES}
        for current_id, target, expected in (
            (event_id, device_id, head),
            (stale_id, device_id, "0" * 40),
            (foreign_target_id, other_device, head),
        ):
            entities["development_events"].append(
                {
                    "event_id": str(current_id),
                    "project_id": project_id,
                    "feature_id": feature_id,
                    "target_device_id": str(target),
                    "expected_head_sha": expected,
                    "status": "targeted",
                }
            )
        store.apply_bootstrap({"high_watermark": 0, "entities": entities})
        visible = event_inbox(ledger)
        assert {item["event_id"] for item in visible} == {str(event_id), str(stale_id)}
        with pytest.raises(ValueError, match="Git HEAD"):
            capture_development_event(
                ledger,
                str(account_id),
                str(stale_id),
                binding_id,
                selected_untracked=set(),
                trace_id=str(uuid.uuid4()),
            )
        with pytest.raises(ValueError, match="当前设备"):
            capture_development_event(
                ledger,
                str(account_id),
                str(foreign_target_id),
                binding_id,
                selected_untracked=set(),
                trace_id=str(uuid.uuid4()),
            )
        first = capture_development_event(
            ledger,
            str(account_id),
            str(event_id),
            binding_id,
            selected_untracked=set(),
            trace_id=str(uuid.uuid4()),
        )
        second = capture_development_event(
            ledger,
            str(account_id),
            str(event_id),
            binding_id,
            selected_untracked=set(),
            trace_id=str(uuid.uuid4()),
        )
        assert first["reused"] is False and second["reused"] is True
        assert first["snapshot_id"] == second["snapshot_id"]
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM local_development_event_captures"
            ).fetchone()[0]
            == 1
        )
        analysis_id, created = ledger.queue_analysis(
            first["snapshot_id"], binding_id, str(event_id),
            trace_id=str(uuid.uuid4()),
        )
        assert created
        link_event_analysis(
            ledger, str(account_id), str(event_id), analysis_id,
            trace_id=str(uuid.uuid4()),
        )
        link_event_analysis(
            ledger, str(account_id), str(event_id), analysis_id,
            trace_id=str(uuid.uuid4()),
        )
        assert event_analysis_request(
            ledger, str(account_id), str(event_id), first["snapshot_id"], binding_id,
        ) == analysis_id
        another_id, _ = ledger.queue_analysis(
            first["snapshot_id"], binding_id, str(uuid.uuid4()),
            trace_id=str(uuid.uuid4()), reanalysis=True,
        )
        with pytest.raises(ValueError, match="另一分析"):
            link_event_analysis(
                ledger, str(account_id), str(event_id), another_id,
                trace_id=str(uuid.uuid4()),
            )
    app = create_app(ServeState(state_dir=tmp_path, token="test-token", locale="en"))
    with TestClient(app, base_url="http://localhost:8765") as client:
        headers = {"Origin": "http://localhost:8765", "X-AhaDiff-Token": "test-token"}
        inbox_response = client.get("/api/growth/local/sync/events", headers=headers)
        assert inbox_response.status_code == 200
        assert len(inbox_response.json()["events"]) == 2
        repeated = client.post(
            f"/api/growth/local/sync/events/{event_id}/capture",
            json={"account_id": str(account_id), "binding_id": binding_id},
            headers=headers,
        )
        assert repeated.status_code == 200
        assert repeated.json()["snapshot_id"] == first["snapshot_id"]
        assert repeated.json()["analysis_id"] == analysis_id


def test_incremental_claim_marks_cached_event_targeted(tmp_path: Path) -> None:
    with GrowthLocalRepository(tmp_path / "growth.sqlite") as ledger:
        account_id, device_id, event_id = (uuid.uuid4() for _ in range(3))
        store = GrowthSyncStore(ledger, account_id, device_id)
        entities: dict[str, list[dict[str, object]]] = {name: [] for name in BOOTSTRAP_TYPES}
        entities["development_events"] = [
            {
                "event_id": str(event_id),
                "project_id": str(uuid.uuid4()),
                "feature_id": str(uuid.uuid4()),
                "target_device_id": None,
                "expected_head_sha": "a" * 40,
                "status": "pending_confirmation",
            }
        ]
        store.apply_bootstrap({"high_watermark": 0, "entities": entities})
        store.apply_changes(
            {
                "next_after": 1,
                "changes": [
                    {
                        "change_seq": 1,
                        "entity_type": "development_event",
                        "entity_id": str(event_id),
                        "event_type": "development_event_target_confirmed",
                        "revision": 2,
                        "deleted_at": None,
                        "payload": {"target_device_id": str(device_id)},
                    }
                ],
            }
        )
        assert event_inbox(ledger)[0]["status"] == "targeted"
