"""One user decision turns selected recommendation drafts into formal cards."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .git_snapshot import git
from .learning import GrowthLearningService

if TYPE_CHECKING:
    from .local import GrowthLocalRepository


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rows(ledger: GrowthLocalRepository, opportunity_ids: list[str]) -> list[Any]:
    if not opportunity_ids:
        return []
    return ledger.connection.execute(
        "SELECT opportunities.*, features.project_id, snapshots.capture_scope, "
        "snapshots.snapshot_id, snapshots.resolved_base_sha, snapshots.head_sha "
        "FROM opportunities JOIN analysis_runs USING(analysis_id) "
        "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
        "WHERE opportunity_id IN (" + ",".join("?" for _ in opportunity_ids) + ")",
        opportunity_ids,
    ).fetchall()


def _link_sources(
    ledger: GrowthLocalRepository, card_id: str, recommendations: list[Any],
) -> None:
    for row in recommendations:
        scope = json.loads(row["capture_scope"])
        binding = ledger.connection.execute(
            "SELECT snapshot_bindings.binding_id,bindings.canonical_local_path "
            "FROM snapshot_bindings JOIN bindings USING(binding_id) WHERE snapshot_id=? "
            "ORDER BY captured_at DESC LIMIT 1", (row["snapshot_id"],),
        ).fetchone()
        if binding is None:
            continue
        shas = [str(scope["sha"])] if scope.get("kind") == "commit" and scope.get("sha") else []
        if not shas:
            try:
                from pathlib import Path
                raw = git(Path(binding["canonical_local_path"]), "rev-list", "--reverse",
                          f"{row['resolved_base_sha']}..{row['head_sha']}")
                shas = [value for value in raw.decode().splitlines() if len(value) == 40]
            except (OSError, ValueError):
                shas = []
        for sha in shas:
            ledger.connection.execute(
                "INSERT OR IGNORE INTO card_source_commits VALUES (?,?,?,?,?)",
                (card_id, sha, binding["binding_id"], "confirmed",
                 datetime.now(UTC).isoformat()),
            )
        if not shas:
            continue
        ledger.connection.execute(
            "UPDATE knowledge_cards SET source_link_status='linked' WHERE card_id=?",
            (card_id,),
        )


def apply_decisions(
    ledger: GrowthLocalRepository, request_id: str, *,
    confirm_groups: list[dict[str, Any]], defer_ids: list[str], ignore_ids: list[str],
) -> dict[str, Any]:
    request = {"confirm_groups": confirm_groups, "defer_ids": defer_ids,
               "ignore_ids": ignore_ids}
    request_hash = hashlib.sha256(_json(request).encode()).hexdigest()
    selected = [item for group in confirm_groups for item in group["opportunity_ids"]]
    if len(set(selected + defer_ids + ignore_ids)) != len(selected + defer_ids + ignore_ids):
        raise ValueError("同一推荐不能重复决定")
    db = ledger.connection
    db.execute("BEGIN IMMEDIATE")
    try:
        previous = db.execute(
            "SELECT request_hash,result_json FROM recommendation_decisions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if previous:
            if previous["request_hash"] != request_hash:
                raise ValueError("同一确认请求不能更换决定")
            result = json.loads(previous["result_json"])
            db.commit()
            return result
        service = GrowthLearningService(ledger)
        cards: list[dict[str, Any]] = []
        decided: set[str] = set()
        for group in confirm_groups:
            ids = group["opportunity_ids"]
            rows = _rows(ledger, ids)
            if len(rows) != len(ids):
                raise ValueError("推荐必须存在")
            by_id = {row["opportunity_id"]: row for row in rows}
            primary = by_id[ids[0]]
            proposal = db.execute(
                "SELECT opportunity_id FROM topic_proposals WHERE proposal_id=?",
                (group["proposal_id"],),
            ).fetchone()
            if proposal is None or proposal["opportunity_id"] != primary["opportunity_id"]:
                raise ValueError("主题建议不属于首项推荐")
            keys = list({row["recommendation_key"] for row in rows})
            matching = db.execute(
                "SELECT opportunities.opportunity_id FROM opportunities "
                "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
                "JOIN features USING(feature_id) WHERE recommendation_key IN ("
                + ",".join("?" for _ in keys) + ") "
                "AND recommendation_status IN ('pending','deferred')",
                keys,
            ).fetchall()
            expanded_ids = list(dict.fromkeys([*ids, *(row[0] for row in matching)]))
            if decided.intersection(expanded_ids) or set(expanded_ids).intersection(
                defer_ids + ignore_ids
            ):
                raise ValueError("重复推荐存在相反决定")
            expanded = _rows(ledger, expanded_ids)
            if any(row["recommendation_status"] not in {"pending", "deferred"}
                   for row in expanded):
                raise ValueError("推荐已经确认或忽略")
            existing = db.execute(
                "SELECT DISTINCT canonical_card_id FROM opportunities "
                "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
                "JOIN features USING(feature_id) WHERE recommendation_key IN ("
                + ",".join("?" for _ in keys) + ") "
                "AND canonical_card_id IS NOT NULL",
                keys,
            ).fetchall()
            if len(existing) > 1:
                raise ValueError("已存在不同卡片，请先核对重复推荐")
            requested_card_id = group.get("existing_card_id")
            if requested_card_id:
                card = db.execute(
                    "SELECT card_id FROM knowledge_cards WHERE card_id=?",
                    (requested_card_id,),
                ).fetchone()
                if card is None or (existing and str(existing[0][0]) != requested_card_id):
                    raise ValueError("归并目标不存在或与既有归属冲突")
            reused = bool(existing or requested_card_id)
            if reused:
                card_id = str(requested_card_id or existing[0][0])
            else:
                existing_topic_id = group.get("existing_topic_id")
                topic_title = None
                if existing_topic_id:
                    topic = db.execute(
                        "SELECT title FROM growth_topics WHERE topic_id=? AND status='active'",
                        (existing_topic_id,),
                    ).fetchone()
                    if topic is None:
                        raise ValueError("已有主题不存在或已归档")
                    topic_title = str(topic["title"])
                topic_id = service.decide_topic(
                    group["proposal_id"], "confirm", trace_id=request_id,
                    existing_topic_id=existing_topic_id,
                    existing_topic_title=topic_title,
                    title_override=group.get("title"),
                )
                assert topic_id is not None
                draft = json.loads(primary["card_json"]) if primary["card_json"] else {}
                question = str(primary["learning_goal"])
                card_id = service.create_task(
                    primary["opportunity_id"], topic_id, question,
                    trace_id=request_id, followups=draft.get("followups"),
                )
            _link_sources(ledger, card_id, expanded)
            db.execute(
                "UPDATE opportunities SET recommendation_status='confirmed', "
                "canonical_card_id=? WHERE opportunity_id IN (" +
                ",".join("?" for _ in expanded_ids) + ")",
                (card_id, *expanded_ids),
            )
            decided.update(expanded_ids)
            cards.append({"card_id": card_id, "task_id": card_id,
                          "opportunity_ids": expanded_ids, "reused": reused})
        for target, ids in (("deferred", defer_ids), ("ignored", ignore_ids)):
            if ids:
                rows = _rows(ledger, ids)
                if len(rows) != len(ids) or any(
                    row["recommendation_status"] not in {"pending", "deferred"}
                    for row in rows
                ):
                    raise ValueError("只能暂缓或忽略待确认的推荐")
                db.execute(
                    "UPDATE opportunities SET recommendation_status=? WHERE opportunity_id IN ("
                    + ",".join("?" for _ in ids) + ")", (target, *ids),
                )
        result = {"cards": cards, "deferred_ids": defer_ids, "ignored_ids": ignore_ids,
                  "created_card_count": sum(not card["reused"] for card in cards)}
        db.execute(
            "INSERT INTO recommendation_decisions VALUES (?,?,?,?)",
            (request_id, request_hash, _json(result), datetime.now(UTC).isoformat()),
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
