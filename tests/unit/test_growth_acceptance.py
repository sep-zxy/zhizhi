"""The release gate must refuse incomplete or altered story evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ahadiff.growth.acceptance import _proof_binding_ok, prepare, report, run, verify


@pytest.mark.parametrize("case", ["S01", "S02", "S03", "S04"])
def test_prepared_real_git_story_stays_blocked_without_live_evidence(
    tmp_path: Path, case: str
) -> None:
    runs = tmp_path / "runs"
    first = prepare(runs, case, f"candidate-{case.lower()}", "candidate-001")
    assert first["status"] == "NOT_RUN"
    run_dir = runs / f"candidate-{case.lower()}"
    assert (run_dir / "fixture" / "baseline.bundle").is_file()
    assert (run_dir / "fixture" / "next").is_dir()
    blocked = run(runs, case, "live", first["run_id"])
    assert blocked["status"] == "BLOCKED"
    assert blocked["checks"]["fixture_bundle"]
    assert not blocked["checks"][f"{case}-01"]
    assert "状态：**BLOCKED**。" in (run_dir / "report.md").read_text(encoding="utf-8")


def test_replay_and_forged_success_cannot_pass(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    prepare(runs, "S01", "candidate-s01", "candidate-001")
    with pytest.raises(ValueError, match="只接受 live"):
        run(runs, "S01", "replay", "candidate-s01")
    result_path = runs / "candidate-s01" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["status"] = "PASS"
    result["proof"] = {"windows_app": True, "macos_app": True}
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="既有最终结果不可改写"):
        verify(runs, "candidate-s01")


def test_corrupt_fixture_is_blocked_and_retry_keeps_prior_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    prepare(runs, "S02", "first-s02", "candidate-001")
    original = runs / "first-s02" / "fixture" / "baseline.bundle"
    original.write_bytes(original.read_bytes() + b"tampered")
    first = verify(runs, "first-s02")
    assert first["status"] == "BLOCKED"
    assert not first["checks"]["fixture_bundle"]
    prepare(runs, "S02", "retry-s02", "candidate-001", "first-s02")
    summary = report(runs, "candidate-001")
    assert summary["status"] == "BLOCKED"
    assert summary["cases"]["S02"]["run_id"] == "retry-s02"
    assert original.is_file()


def test_committed_bundle_can_be_verified_without_fixture_repository(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    prepare(runs, "S01", "candidate-s01", "candidate-001")
    restored_runs = tmp_path / "restored"
    shutil.copytree(
        runs, restored_runs,
        ignore=lambda directory, names: {"repo"}
        if Path(directory).name == "fixture" else set(),
    )
    restored = verify(restored_runs, "candidate-s01")
    assert restored["checks"]["fixture_bundle"]
    assert restored["checks"]["git_base"]
    assert restored["status"] == "BLOCKED"


def test_evidence_gate_rejects_reversed_time_and_unknown_step(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    prepare(runs, "S01", "candidate-s01", "candidate-001")
    run_dir = runs / "candidate-s01"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    start = datetime.fromisoformat(manifest["started_at"]).astimezone(UTC)
    bundle = run_dir / "fixture" / "baseline.bundle"
    row = {
        "step_id": "S01-01", "status": "PASS",
        "started_at": (start + timedelta(seconds=2)).isoformat(),
        "finished_at": (start + timedelta(seconds=1)).isoformat(),
        "device_id": str(uuid.uuid4()), "trace_id": str(uuid.uuid4()),
        "action": "运行基线", "expected": "返回 A|B", "actual": {"probe": "A|B"},
        "artifacts": [{"path": "fixture/baseline.bundle",
                       "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}],
    }
    (run_dir / "steps.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    reversed_time = verify(runs, "candidate-s01")
    assert not reversed_time["checks"]["S01-01"]
    row["finished_at"] = (start + timedelta(seconds=3)).isoformat()
    (run_dir / "steps.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    valid_prefix = verify(runs, "candidate-s01")
    assert valid_prefix["checks"]["S01-01"]
    row["step_id"] = "S01-99"
    (run_dir / "steps.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    unknown = verify(runs, "candidate-s01")
    assert not unknown["checks"]["known_steps"]


def test_evidence_gate_rejects_reordered_or_foreign_story_steps(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    prepare(runs, "S01", "candidate-s01", "candidate-001")
    run_dir = runs / "candidate-s01"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    start = datetime.fromisoformat(manifest["started_at"]).astimezone(UTC)
    bundle = run_dir / "fixture" / "baseline.bundle"
    artifact = {"path": "fixture/baseline.bundle",
                "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}
    rows = [
        {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "case_id": "S01",
            "step_id": f"S01-{number:02d}",
            "status": "PASS",
            "started_at": (start + timedelta(seconds=number * 2)).isoformat(),
            "finished_at": (start + timedelta(seconds=number * 2 + 1)).isoformat(),
            "device_id": str(uuid.uuid4()),
            "trace_id": str(uuid.uuid4()),
            "action": "验收操作",
            "expected": "业务断言",
            "actual": {"observed": number},
            "artifacts": [artifact],
        }
        for number in range(1, 12)
    ]

    def write_steps(selected: list[dict[str, object]]) -> None:
        (run_dir / "steps.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected),
            encoding="utf-8",
        )

    write_steps([rows[1], rows[0]])
    reordered = verify(runs, "candidate-s01")
    assert reordered["checks"]["S01-01"]
    assert reordered["checks"]["S01-02"]
    assert not reordered["checks"]["ordered_steps"]

    write_steps(rows[:2])
    prefix = verify(runs, "candidate-s01")
    assert prefix["checks"]["ordered_steps"]
    assert prefix["checks"]["step_identity"]

    rows[0]["run_id"] = "another-run"
    write_steps(rows[:2])
    foreign = verify(runs, "candidate-s01")
    assert not foreign["checks"]["step_identity"]

    rows[0]["run_id"] = manifest["run_id"]
    rows[-1].pop("run_id")
    write_steps(rows)
    incomplete_identity = verify(runs, "candidate-s01")
    assert incomplete_identity["checks"]["ordered_steps"]
    assert not incomplete_identity["checks"]["step_identity"]


def test_complete_story_cannot_reuse_fixture_as_all_live_proofs(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    candidate_hash = "a" * 64
    prepare(runs, "S01", "candidate-s01", "candidate-001",
            candidate_sha256=candidate_hash)
    run_dir = runs / "candidate-s01"
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    start = datetime.fromisoformat(manifest["started_at"]).astimezone(UTC)
    manifest["finished_at"] = (start + timedelta(seconds=30)).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    bundle = run_dir / "fixture" / "baseline.bundle"
    artifact = {"path": "fixture/baseline.bundle",
                "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}
    rows = [
        {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "case_id": "S01",
            "step_id": f"S01-{number:02d}",
            "status": "PASS",
            "started_at": (start + timedelta(seconds=number * 2)).isoformat(),
            "finished_at": (start + timedelta(seconds=number * 2 + 1)).isoformat(),
            "device_id": str(uuid.uuid4()),
            "trace_id": str(uuid.uuid4()),
            "action": "声称已操作",
            "expected": "声称的断言",
            "actual": {"claimed": True},
            "artifacts": [artifact],
        }
        for number in range(1, 12)
    ]
    (run_dir / "steps.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    (run_dir / "chain.json").write_text(json.dumps({
        "project_id": "project", "snapshot_id": "snapshot", "task_id": "task",
        "attempt_ids": ["attempt"], "note_id": "note",
    }), encoding="utf-8")
    result_path = run_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["proof"] = dict.fromkeys((
        "real_environment", "business_assertions", "semantic_review",
        "complete_chain", "windows_app", "macos_app", "cloud_postgres",
        "model_provider", "reme",
    ), artifact)
    result_path.write_text(json.dumps(result), encoding="utf-8")

    blocked = verify(runs, "candidate-s01")
    assert blocked["status"] == "BLOCKED"
    assert all(blocked["checks"][f"S01-{number:02d}"] for number in range(1, 12))
    assert all(blocked["checks"][name] for name in result["proof"])
    assert not blocked["checks"]["proof_binding"]


def test_live_proof_manifests_bind_raw_artifacts_to_one_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    prepare(runs, "S01", "candidate-s01", "candidate-001",
            candidate_sha256="b" * 64)
    run_dir = runs / "candidate-s01"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    proof: dict[str, dict[str, str]] = {}
    for name in (
        "real_environment", "business_assertions", "semantic_review",
        "complete_chain", "windows_app", "macos_app", "cloud_postgres",
        "model_provider", "reme",
    ):
        raw = run_dir / "artifacts" / f"{name}-raw.json"
        raw.write_text(json.dumps({"source": name}), encoding="utf-8")
        wrapper = run_dir / "artifacts" / f"{name}-proof.json"
        wrapper.write_text(json.dumps({
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "case_id": manifest["case_id"],
            "release": manifest["release"],
            "candidate_sha256": manifest["candidate_sha256"],
            "proof_type": name,
            "mode": "live",
            "status": "PASS",
            "observed": {"source": name},
            "supporting_artifacts": [{
                "path": f"artifacts/{raw.name}",
                "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            }],
        }), encoding="utf-8")
        proof[name] = {
            "path": f"artifacts/{wrapper.name}",
            "sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        }

    assert _proof_binding_ok(run_dir, proof, manifest)
    (run_dir / "artifacts" / "reme-raw.json").write_text(
        '{"source":"tampered"}', encoding="utf-8"
    )
    assert not _proof_binding_ok(run_dir, proof, manifest)
