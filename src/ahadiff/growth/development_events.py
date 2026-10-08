"""Receive a cloud development event against an explicit local Git binding."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .git_snapshot import git_text

if TYPE_CHECKING:
    from .local import GrowthLocalRepository


SHA40 = re.compile(r"[0-9a-f]{40}\Z")


def event_inbox(ledger: GrowthLocalRepository) -> list[dict[str, Any]]:
    """Show this device's targeted events and unclaimed suggestions from its cache."""
    db = ledger.connection
    rows = db.execute(
        "SELECT entities.account_id, accounts.device_id, entities.entity_id, "
        "entities.payload_json FROM sync_entities entities "
        "JOIN sync_accounts accounts USING(account_id) "
        "WHERE accounts.bootstrapped=1 AND entities.entity_type='development_event' "
        "ORDER BY entities.rowid DESC"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        event = cast("dict[str, Any]", json.loads(row["payload_json"]))
        target = event.get("target_device_id")
        if target not in {None, row["device_id"]}:
            continue
        project_id = str(event.get("project_id") or "")
        bindings = [
            dict(binding)
            for binding in db.execute(
                "SELECT binding_id, canonical_local_path FROM bindings "
                "WHERE project_id=? ORDER BY created_at",
                (project_id,),
            )
        ]
        captured = db.execute(
            "SELECT binding_id, snapshot_id, analysis_id FROM "
            "local_development_event_captures WHERE account_id=? AND event_id=?",
            (row["account_id"], row["entity_id"]),
        ).fetchone()
        result.append(
            {
                "account_id": row["account_id"],
                "device_id": row["device_id"],
                "event_id": row["entity_id"],
                "project_id": project_id,
                "feature_id": event.get("feature_id"),
                "expected_head_sha": event.get("expected_head_sha"),
                "status": event.get("status", "pending_confirmation"),
                "target_device_id": target,
                "bindings": bindings,
                "capture": dict(captured) if captured else None,
            }
        )
    return result


def _targeted_event(
    ledger: GrowthLocalRepository,
    account_id: str,
    event_id: str,
) -> tuple[dict[str, Any], str]:
    row = ledger.connection.execute(
        "SELECT accounts.device_id, entities.payload_json "
        "FROM sync_accounts accounts JOIN sync_entities entities USING(account_id) "
        "WHERE accounts.account_id=? AND accounts.bootstrapped=1 "
        "AND entities.entity_type='development_event' AND entities.entity_id=?",
        (account_id, event_id),
    ).fetchone()
    if row is None:
        raise ValueError("本机没有此账号的已同步开发事件")
    event = cast("dict[str, Any]", json.loads(row["payload_json"]))
    if event.get("status") != "targeted" or event.get("target_device_id") != row["device_id"]:
        raise ValueError("开发事件尚未指定到当前设备")
    head = event.get("expected_head_sha")
    if not isinstance(head, str) or not SHA40.fullmatch(head):
        raise ValueError("开发事件的预期 Git HEAD 无效")
    return event, str(row["device_id"])


def capture_development_event(
    ledger: GrowthLocalRepository,
    account_id: str,
    event_id: str,
    binding_id: str,
    *,
    selected_untracked: set[str],
    trace_id: str,
) -> dict[str, Any]:
    """Capture once after checking the cached target, local binding and HEAD."""
    db = ledger.connection
    event, device_id = _targeted_event(ledger, account_id, event_id)
    previous = db.execute(
        "SELECT binding_id, expected_head_sha, snapshot_id, analysis_id "
        "FROM local_development_event_captures "
        "WHERE account_id=? AND event_id=?",
        (account_id, event_id),
    ).fetchone()
    if previous is not None:
        if previous["binding_id"] != binding_id:
            raise ValueError("此开发事件已由另一仓库绑定接收")
        return {
            "event_id": event_id,
            "snapshot_id": previous["snapshot_id"],
            "analysis_id": previous["analysis_id"],
            "reused": True,
            "head_sha": previous["expected_head_sha"],
        }
    project_id = str(event.get("project_id") or "")
    feature_id = str(event.get("feature_id") or "")
    if not feature_id:
        raise ValueError("开发事件缺少已确认的本机 Feature")
    binding = db.execute(
        "SELECT canonical_local_path FROM bindings WHERE binding_id=? AND project_id=?",
        (binding_id, project_id),
    ).fetchone()
    feature = db.execute(
        "SELECT 1 FROM features WHERE feature_id=? AND project_id=?",
        (feature_id, project_id),
    ).fetchone()
    policy = db.execute(
        "SELECT cloud_allowed, cloud_account_id FROM project_policies WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if (
        binding is None
        or feature is None
        or policy is None
        or not policy["cloud_allowed"]
        or policy["cloud_account_id"] != account_id
    ):
        raise ValueError("开发事件与获准同步的本机项目/仓库/Feature 不匹配")
    root = Path(binding["canonical_local_path"])
    actual_head = git_text(root, "rev-parse", "HEAD")
    if actual_head != event["expected_head_sha"]:
        raise ValueError("本机 Git HEAD 与开发事件不一致，请先核对代码版本")
    snapshot_id = ledger.capture_snapshot(
        feature_id,
        binding_id,
        trace_id=trace_id,
        selected_untracked=selected_untracked,
    )
    with db:
        db.execute(
            "INSERT INTO local_development_event_captures "
            "(account_id, event_id, device_id, project_id, feature_id, "
            "binding_id, expected_head_sha, snapshot_id, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                event_id,
                device_id,
                project_id,
                feature_id,
                binding_id,
                actual_head,
                snapshot_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        ledger.record_event(
            trace_id,
            "development_event",
            event_id,
            "development_event_captured",
            {
                "snapshot_id": snapshot_id,
                "binding_id": binding_id,
                "expected_head_sha": actual_head,
            },
        )
    return {
        "event_id": event_id,
        "snapshot_id": snapshot_id,
        "analysis_id": None,
        "reused": False,
        "head_sha": actual_head,
    }


def event_analysis_request(
    ledger: GrowthLocalRepository,
    account_id: str,
    event_id: str,
    snapshot_id: str,
    binding_id: str,
) -> str | None:
    """Require the model analysis to use exactly the approved event capture."""
    _targeted_event(ledger, account_id, event_id)
    row = ledger.connection.execute(
        "SELECT snapshot_id, binding_id, analysis_id FROM "
        "local_development_event_captures WHERE account_id=? AND event_id=?",
        (account_id, event_id),
    ).fetchone()
    if row is None or row["snapshot_id"] != snapshot_id or row["binding_id"] != binding_id:
        raise ValueError("开发事件尚未在该本机仓库捕获")
    return cast("str | None", row["analysis_id"])


def link_event_analysis(
    ledger: GrowthLocalRepository,
    account_id: str,
    event_id: str,
    analysis_id: str,
    *,
    trace_id: str,
) -> None:
    with ledger.connection:
        cursor = ledger.connection.execute(
            "UPDATE local_development_event_captures SET analysis_id=? "
            "WHERE account_id=? AND event_id=? AND analysis_id IS NULL",
            (analysis_id, account_id, event_id),
        )
        if cursor.rowcount != 1:
            row = ledger.connection.execute(
                "SELECT analysis_id FROM local_development_event_captures "
                "WHERE account_id=? AND event_id=?",
                (account_id, event_id),
            ).fetchone()
            if row is None or row["analysis_id"] != analysis_id:
                raise ValueError("开发事件已关联另一分析")
            return
        ledger.record_event(
            trace_id,
            "development_event",
            event_id,
            "development_event_analysis_linked",
            {"analysis_id": analysis_id},
        )
