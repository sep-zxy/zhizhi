"""The desktop growth route captures a real Git change and survives a restart."""

from __future__ import annotations

import subprocess
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from starlette.testclient import TestClient

from ahadiff.growth.local import GrowthLocalRepository
from ahadiff.serve.app import create_app
from ahadiff.serve.state import ServeState

if TYPE_CHECKING:
    from pathlib import Path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_local_growth_route_captures_git_change_and_persists(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "growth@example.invalid")
    _git(repo, "config", "user.name", "Growth Acceptance")
    (repo / "main.py").write_text("answer = 1\n", encoding="utf-8")
    _git(repo, "add", "main.py")
    _git(repo, "commit", "-m", "初始代码")

    state_dir = tmp_path / "workspace" / ".ahadiff"
    headers = {"origin": "http://localhost:8765", "X-AhaDiff-Token": "test-token"}
    client = TestClient(
        create_app(ServeState(state_dir=state_dir, token="test-token")),
        base_url="http://localhost:8765",
    )

    assert client.get("/api/growth/local").status_code == 401
    project = client.post(
        "/api/growth/local/projects", headers=headers, json={"name": "示例项目"}
    )
    assert project.status_code == 201
    project_id = project.json()["project_id"]
    binding = client.post(
        f"/api/growth/local/projects/{project_id}/bindings",
        headers=headers, json={"path": str(repo)},
    )
    assert binding.status_code == 201
    binding_id = binding.json()["binding_id"]
    invalid_ref = client.post(
        f"/api/growth/local/projects/{project_id}/features",
        headers=headers,
        json={"binding_id": binding_id, "label": "无效引用", "base_ref": "--help"},
    )
    assert invalid_ref.status_code == 422
    feature = client.post(
        f"/api/growth/local/projects/{project_id}/features",
        headers=headers,
        json={"binding_id": binding_id, "label": "修正计算", "base_ref": "HEAD"},
    )
    assert feature.status_code == 201
    feature_id = feature.json()["feature_id"]

    (repo / "main.py").write_text("answer = 2\n", encoding="utf-8")
    (repo / "cache.py").write_text("CACHE = True\n", encoding="utf-8")
    (repo / ".ahadiff").mkdir()
    (repo / ".ahadiff" / ".env").write_text("MODEL_KEY=secret\n", encoding="utf-8")
    untracked = client.get(
        f"/api/growth/local/bindings/{binding_id}/untracked", headers=headers
    )
    assert untracked.status_code == 200
    assert untracked.json()["paths"] == ["cache.py"]
    rejected = client.post(
        f"/api/growth/local/features/{feature_id}/snapshots",
        headers=headers,
        json={"binding_id": binding_id, "selected_untracked": [".ahadiff/.env"]},
    )
    assert rejected.status_code == 422
    captured = client.post(
        f"/api/growth/local/features/{feature_id}/snapshots",
        headers=headers,
        json={"binding_id": binding_id, "selected_untracked": ["cache.py"]},
    )
    assert captured.status_code == 201
    assert captured.json()["changed_paths"] == ["cache.py", "main.py"]
    assert captured.json()["snapshot_id"]

    other_worktree = tmp_path / "other-worktree"
    _git(repo, "worktree", "add", "--detach", str(other_worktree), "HEAD")
    (other_worktree / "main.py").write_text("answer = 2\n", encoding="utf-8")
    (other_worktree / "cache.py").write_text("CACHE = True\n", encoding="utf-8")
    other_binding = client.post(
        f"/api/growth/local/projects/{project_id}/bindings",
        headers=headers, json={"path": str(other_worktree)},
    )
    assert other_binding.status_code == 201
    other_binding_id = other_binding.json()["binding_id"]
    wrong_binding = client.post(
        f"/api/growth/local/snapshots/{captured.json()['snapshot_id']}/analysis",
        headers=headers,
        json={"binding_id": other_binding_id, "request_id": str(uuid.uuid4()),
              "mode": "dry_run"},
    )
    assert wrong_binding.status_code == 409
    same_content = client.post(
        f"/api/growth/local/features/{feature_id}/snapshots",
        headers=headers,
        json={"binding_id": other_binding_id, "selected_untracked": ["cache.py"]},
    )
    assert same_content.status_code == 201
    assert same_content.json()["snapshot_id"] == captured.json()["snapshot_id"]

    restarted = TestClient(
        create_app(ServeState(state_dir=state_dir, token="test-token")),
        base_url="http://localhost:8765",
    )
    listing = restarted.get("/api/growth/local", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["projects"][0]["project_id"] == project_id
    assert {row["binding_id"] for row in listing.json()["bindings"]} == {
        binding_id, other_binding_id,
    }
    assert listing.json()["features"][0]["feature_id"] == feature_id
    assert listing.json()["snapshots"][0]["snapshot_id"] == captured.json()["snapshot_id"]
    assert set(listing.json()["snapshots"][0]["binding_ids"]) == {
        binding_id, other_binding_id,
    }
    assert "patch_text" not in listing.text

    with TestClient(
        create_app(ServeState(state_dir=state_dir, token="test-token")),
        base_url="http://localhost:8765",
    ) as analysis_client:
        request_id = str(uuid.uuid4())
        submitted = analysis_client.post(
            f"/api/growth/local/snapshots/{captured.json()['snapshot_id']}/analysis",
            headers=headers,
            json={"binding_id": binding_id, "request_id": request_id,
                  "mode": "dry_run"},
        )
        assert submitted.status_code == 202
        task_id = submitted.json()["task_id"]
        for _ in range(100):
            task = analysis_client.get(f"/api/tasks/{task_id}", headers=headers)
            assert task.status_code == 200
            if task.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert task.json()["status"] == "completed"
        analyses = analysis_client.get("/api/growth/local", headers=headers).json()
        assert len(analyses["analysis_runs"]) == 1
        row = analyses["analysis_runs"][0]
        assert row["snapshot_id"] == captured.json()["snapshot_id"]
        assert row["mode"] == "dry_run" and row["status"] == "succeeded"
        assert row["upstream_run_id"]
        assert analysis_client.post(
            f"/api/growth/local/analyses/{row['analysis_id']}/retry",
            headers=headers, json={"binding_id": binding_id},
        ).status_code == 409

        (repo / "main.py").write_text("answer = 3\n", encoding="utf-8")
        stale = analysis_client.post(
            f"/api/growth/local/snapshots/{captured.json()['snapshot_id']}/analysis",
            headers=headers,
            json={"binding_id": binding_id, "request_id": str(uuid.uuid4()),
                  "mode": "dry_run", "reanalysis": True},
        )
        assert stale.status_code == 202
        for _ in range(100):
            stale_task = analysis_client.get(
                f"/api/tasks/{stale.json()['task_id']}", headers=headers
            )
            if stale_task.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert stale_task.json()["status"] == "failed"
        failed_analysis = next(
            item for item in analysis_client.get(
                "/api/growth/local", headers=headers
            ).json()["analysis_runs"] if item["status"] == "failed"
        )
        assert failed_analysis["status"] == "failed"
        assert failed_analysis["upstream_run_id"] is None
        assert failed_analysis["needs_recapture"] is True
        assert "error" not in failed_analysis

        (repo / "main.py").write_text("answer = 2\n", encoding="utf-8")
        retried = analysis_client.post(
            f"/api/growth/local/analyses/{failed_analysis['analysis_id']}/retry",
            headers=headers, json={"binding_id": binding_id},
        )
        assert retried.status_code == 202
        for _ in range(100):
            retry_task = analysis_client.get(
                f"/api/tasks/{retried.json()['task_id']}", headers=headers
            )
            if retry_task.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert retry_task.json()["status"] == "completed"
        after_retry = analysis_client.get("/api/growth/local", headers=headers).json()
        recovered = next(
            item for item in after_retry["analysis_runs"]
            if item["analysis_id"] == failed_analysis["analysis_id"]
        )
        assert recovered["status"] == "succeeded"

    interrupted_id = str(uuid.uuid4())
    with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
        ledger.connection.execute(
                "INSERT INTO analysis_runs (analysis_id, snapshot_id, request_id, "
                "input_fingerprint, mode, status, upstream_run_id, error, created_at) "
                "VALUES (?, ?, ?, ?, 'dry_run', 'queued', NULL, NULL, ?)",
            (interrupted_id, captured.json()["snapshot_id"], str(uuid.uuid4()),
             "interrupted-fingerprint", datetime.now(UTC).isoformat()),
        )
        ledger.connection.commit()
    with TestClient(
        create_app(ServeState(state_dir=state_dir, token="test-token")),
        base_url="http://localhost:8765",
    ) as recovered_client:
        saved = recovered_client.get("/api/growth/local", headers=headers).json()
        interrupted = next(
            item for item in saved["analysis_runs"]
            if item["analysis_id"] == interrupted_id
        )
        assert interrupted["status"] == "failed"
        assert interrupted["needs_recapture"] is False
        retry = recovered_client.post(
            f"/api/growth/local/analyses/{interrupted_id}/retry",
            headers=headers, json={"binding_id": binding_id},
        )
        assert retry.status_code == 202
        for _ in range(100):
            result = recovered_client.get(
                f"/api/tasks/{retry.json()['task_id']}", headers=headers
            )
            if result.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert result.json()["status"] == "completed"

    # A pre-v6 database has snapshots but no capture-to-binding provenance.
    with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
        ledger.connection.execute("DROP TABLE local_chat_model_requests")
        ledger.connection.execute("DROP TABLE local_development_event_captures")
        ledger.connection.execute("DROP TABLE sync_publication_links")
        ledger.connection.execute("DROP TABLE project_policies")
        ledger.connection.execute("DROP TABLE growth_task_hints")
        ledger.connection.execute("DROP TABLE feedback_model_responses")
        ledger.connection.execute("DROP TABLE feedback_model_attempts")
        ledger.connection.execute("DROP TABLE feedback_model_requests")
        ledger.connection.execute("DROP TABLE analysis_model_responses")
        ledger.connection.execute("DROP TABLE analysis_model_requests")
        ledger.connection.execute("DROP TABLE snapshot_bindings")
        ledger.connection.execute("PRAGMA user_version=5")
        ledger.connection.commit()
    with GrowthLocalRepository(state_dir / "growth.sqlite") as migrated:
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 15
        assert migrated.connection.execute(
            "SELECT snapshot_id FROM snapshots WHERE snapshot_id=?",
            (captured.json()["snapshot_id"],),
        ).fetchone() is not None
        assert migrated.connection.execute(
            "SELECT COUNT(*) FROM snapshot_bindings"
        ).fetchone()[0] == 0
        recaptured_id = migrated.capture_snapshot(
            feature_id, binding_id, trace_id=str(uuid.uuid4()),
            selected_untracked={"cache.py"},
        )
        assert recaptured_id == captured.json()["snapshot_id"]
        assert migrated.connection.execute(
            "SELECT binding_id FROM snapshot_bindings WHERE snapshot_id=?",
            (recaptured_id,),
        ).fetchone()[0] == binding_id
