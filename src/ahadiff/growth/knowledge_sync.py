"""Account-scoped knowledge card snapshots for explicit cross-device sync."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from .local import GrowthLocalRepository


_TABLES = (
    "projects", "bindings", "features", "snapshots", "snapshot_bindings",
    "source_refs", "analysis_runs", "opportunity_batches", "opportunities",
    "growth_topics", "topic_proposals", "knowledge_concepts",
    "knowledge_concept_aliases", "growth_tasks", "knowledge_cards",
    "card_source_commits", "card_learning_materials", "card_objective_questions",
    "card_learning_progress", "card_objective_answers", "card_explanation_reads",
    "card_followup_marks", "card_chat_exchanges",
    "card_learning_progress_history", "card_material_revisions",
    "knowledge_wiki_articles", "knowledge_wiki_drafts",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _select(
    connection: sqlite3.Connection, table: str, column: str, ids: set[str],
) -> list[dict[str, Any]]:
    if table not in _TABLES or column not in {
        "project_id", "binding_id", "feature_id", "snapshot_id", "analysis_id",
        "opportunity_id", "topic_id", "concept_id", "card_id", "task_id",
    }:
        raise ValueError("知识同步查询字段不在固定清单内")
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return [dict(row) for row in connection.execute(
        f"SELECT * FROM {table} WHERE {column} IN ({placeholders})",
        sorted(ids),
    )]


def bundle_for_card(
    ledger: GrowthLocalRepository, card_id: str,
) -> dict[str, Any] | None:
    """Export only the card graph; source bytes, vault paths and model keys stay local."""
    c = ledger.connection
    card_rows = _select(c, "knowledge_cards", "card_id", {card_id})
    if not card_rows:
        return None
    task_rows = _select(c, "growth_tasks", "task_id", {card_id})
    if len(task_rows) != 1:
        raise ValueError("知识卡缺少正式任务")
    concept_ids = {str(row["concept_id"]) for row in card_rows if row["concept_id"]}
    opportunity_ids = {str(row["opportunity_id"]) for row in c.execute(
        "SELECT opportunity_id FROM opportunities WHERE canonical_card_id=?",
        (card_id,),
    )}
    opportunity_ids.add(str(task_rows[0]["opportunity_id"]))
    opportunities = _select(c, "opportunities", "opportunity_id", opportunity_ids)
    analysis_ids = {str(row["analysis_id"]) for row in opportunities}
    analyses = _select(c, "analysis_runs", "analysis_id", analysis_ids)
    snapshot_ids = {str(row["snapshot_id"]) for row in analyses}
    snapshots = _select(c, "snapshots", "snapshot_id", snapshot_ids)
    feature_ids = {str(row["feature_id"]) for row in snapshots}
    features = _select(c, "features", "feature_id", feature_ids)
    commit_links = _select(c, "card_source_commits", "card_id", {card_id})
    snapshot_links = _select(c, "snapshot_bindings", "snapshot_id", snapshot_ids)
    binding_ids = {
        str(row["binding_id"]) for row in commit_links + snapshot_links
    }
    bindings = _select(c, "bindings", "binding_id", binding_ids)
    project_ids = {
        str(row["project_id"]) for row in features + bindings
    }
    topic_ids = {str(row["topic_id"]) for row in task_rows if row["topic_id"]}
    tables: dict[str, list[dict[str, Any]]] = {
        "projects": _select(c, "projects", "project_id", project_ids),
        "bindings": bindings,
        "features": features,
        "snapshots": snapshots,
        "snapshot_bindings": snapshot_links,
        "source_refs": _select(c, "source_refs", "snapshot_id", snapshot_ids),
        "analysis_runs": analyses,
        "opportunity_batches": _select(
            c, "opportunity_batches", "analysis_id", analysis_ids,
        ),
        "opportunities": opportunities,
        "growth_topics": _select(c, "growth_topics", "topic_id", topic_ids),
        "topic_proposals": _select(
            c, "topic_proposals", "opportunity_id", opportunity_ids,
        ),
        "knowledge_concepts": _select(
            c, "knowledge_concepts", "concept_id", concept_ids,
        ),
        "knowledge_concept_aliases": _select(
            c, "knowledge_concept_aliases", "concept_id", concept_ids,
        ),
        "growth_tasks": task_rows,
        "knowledge_cards": card_rows,
        "card_source_commits": commit_links,
        "card_learning_materials": _select(
            c, "card_learning_materials", "card_id", {card_id},
        ),
        "card_objective_questions": _select(
            c, "card_objective_questions", "card_id", {card_id},
        ),
        "card_learning_progress": _select(
            c, "card_learning_progress", "card_id", {card_id},
        ),
        "card_objective_answers": _select(
            c, "card_objective_answers", "card_id", {card_id},
        ),
        "card_explanation_reads": _select(
            c, "card_explanation_reads", "card_id", {card_id},
        ),
        "card_followup_marks": _select(
            c, "card_followup_marks", "card_id", {card_id},
        ),
        "card_chat_exchanges": _select(
            c, "card_chat_exchanges", "card_id", {card_id},
        ),
        "card_learning_progress_history": _select(
            c, "card_learning_progress_history", "card_id", {card_id},
        ),
        "card_material_revisions": _select(
            c, "card_material_revisions", "card_id", {card_id},
        ),
        "knowledge_wiki_articles": _select(
            c, "knowledge_wiki_articles", "concept_id", concept_ids,
        ),
        "knowledge_wiki_drafts": _select(
            c, "knowledge_wiki_drafts", "card_id", {card_id},
        ),
    }
    for row in tables["bindings"]:
        row["canonical_local_path"] = f"remote-unbound:{row['binding_id']}"
    for row in tables["snapshots"]:
        row["patch_text"] = ""
    for row in tables["source_refs"]:
        row["content"] = ""
    for row in tables["analysis_runs"]:
        if row.get("supersedes_analysis_id") not in analysis_ids:
            row["supersedes_analysis_id"] = None
    for rows in tables.values():
        rows.sort(key=_canonical)
    return {
        "schema_version": 1, "card_id": card_id,
        "project_ids": sorted(project_ids), "tables": tables,
    }


def _apply_bundle(
    ledger: GrowthLocalRepository, bundle: dict[str, Any],
    account_id: str, origin: str,
) -> None:
    if bundle.get("schema_version") != 1 or not isinstance(bundle.get("tables"), dict):
        raise ValueError("知识卡同步版本不支持")
    card_id = str(bundle.get("card_id", ""))
    tables = bundle["tables"]
    if set(tables) != set(_TABLES):
        raise ValueError("知识卡同步表清单不完整")
    if len(tables["knowledge_cards"]) != 1 or (
        tables["knowledge_cards"][0].get("card_id") != card_id
    ):
        raise ValueError("知识卡同步身份不一致")
    c = ledger.connection
    if c.in_transaction:
        raise ValueError("知识卡同步需要独立事务")
    c.execute("BEGIN IMMEDIATE")
    with c:
        c.execute("PRAGMA defer_foreign_keys=ON")
        for table in _TABLES:
            actual_columns = {row["name"] for row in c.execute(
                f"PRAGMA table_info({table})"
            )}
            for raw in tables[table]:
                if not isinstance(raw, dict) or set(raw) != actual_columns:
                    raise ValueError(f"知识卡同步字段不匹配：{table}")
                row = dict(raw)
                if table == "source_refs":
                    row["content"] = b""
                columns = list(row)
                placeholders = ",".join("?" for _ in columns)
                local_only = {
                    "bindings": {"canonical_local_path"},
                    "snapshots": {"patch_text"},
                    "source_refs": {"content"},
                }.get(table, set())
                updates = ",".join(
                    f"{column}=excluded.{column}" for column in columns
                    if column not in local_only
                )
                try:
                    c.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) "
                        f"VALUES ({placeholders}) ON CONFLICT DO UPDATE SET {updates}",
                        [row[column] for column in columns],
                    )
                except sqlite3.IntegrityError as exc:
                    defer = c.execute("PRAGMA defer_foreign_keys").fetchone()[0]
                    raise ValueError(
                        f"知识卡同步表 {table} 写入失败：{exc}; "
                        f"defer={defer}; in_transaction={c.in_transaction}"
                    ) from exc
        for project_id in bundle["project_ids"]:
            c.execute(
                "INSERT INTO project_policies "
                "(project_id,local_processing,model_allowed,cloud_allowed,"
                "cloud_account_id,cloud_origin,updated_at) "
                "VALUES (?,1,0,1,?,?,?) ON CONFLICT(project_id) DO NOTHING",
                (project_id, account_id, origin, _now()),
            )


def _record_conflict(
    ledger: GrowthLocalRepository, account: str, card_id: str,
    remote: dict[str, Any],
) -> None:
    previous = ledger.connection.execute(
        "SELECT revision,payload_sha256 FROM knowledge_sync_state "
        "WHERE account_id=? AND card_id=?", (account, card_id),
    ).fetchone()
    revision = int(previous["revision"]) if previous else int(remote["revision"])
    digest = (str(previous["payload_sha256"]) if previous
              else str(remote["payload_sha256"]))
    with ledger.connection:
        ledger.connection.execute(
            "INSERT INTO knowledge_sync_state "
            "(account_id,card_id,revision,payload_sha256,conflict_json,updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,card_id) "
            "DO UPDATE SET conflict_json=excluded.conflict_json,"
            "updated_at=excluded.updated_at",
            (account, card_id, revision, digest, _canonical(remote), _now()),
        )


def resolve_knowledge_conflict(
    ledger: GrowthLocalRepository, account_id: str, card_id: str, decision: str,
    origin: str,
) -> dict[str, Any]:
    """Resolve a recorded concurrent edit without silently overwriting either side."""
    if decision not in {"accept_remote", "keep_local"}:
        raise ValueError("知识卡冲突处理方式无效")
    state = ledger.connection.execute(
        "SELECT conflict_json FROM knowledge_sync_state "
        "WHERE account_id=? AND card_id=?", (account_id, card_id),
    ).fetchone()
    if state is None or not state["conflict_json"]:
        raise ValueError("知识卡没有待处理的同步冲突")
    remote = json.loads(state["conflict_json"])
    if decision == "accept_remote":
        _apply_bundle(ledger, remote["bundle"], account_id, origin)
    with ledger.connection:
        ledger.connection.execute(
            "UPDATE knowledge_sync_state SET revision=?,payload_sha256=?,"
            "conflict_json=NULL,updated_at=? WHERE account_id=? AND card_id=?",
            (int(remote["revision"]), str(remote["payload_sha256"]), _now(),
             account_id, card_id),
        )
    return {"card_id": card_id, "decision": decision,
            "revision": int(remote["revision"])}


async def sync_knowledge(
    ledger: GrowthLocalRepository, client: httpx.AsyncClient,
    token: str, account_id: uuid.UUID, device_id: uuid.UUID,
) -> dict[str, Any]:
    """Pull clean cards and CAS-push local changes; conflicting edits stay local."""
    account = str(account_id)
    headers = {"Authorization": f"Bearer {token}", "X-Device-Id": str(device_id)}
    response = await client.get("/v1/knowledge/bundles", headers=headers)
    response.raise_for_status()
    remote = {row["card_id"]: row for row in response.json()["bundles"]}
    pulled = uploaded = 0
    conflicts: list[str] = []
    origin = str(client.base_url).rstrip("/")
    for card_id, item in remote.items():
        local = bundle_for_card(ledger, card_id)
        state = ledger.connection.execute(
            "SELECT revision,payload_sha256,conflict_json FROM knowledge_sync_state "
            "WHERE account_id=? AND card_id=?", (account, card_id),
        ).fetchone()
        local_hash = _digest(local) if local else None
        remote_revision = int(item["revision"])
        remote_hash = str(item["payload_sha256"])
        if state is not None and state["conflict_json"]:
            _record_conflict(ledger, account, card_id, item)
            conflicts.append(card_id)
            continue
        dirty = local is not None and (
            state is None and local_hash != remote_hash
            or state is not None and local_hash != state["payload_sha256"]
        )
        if dirty and (state is None or remote_revision != state["revision"]):
            _record_conflict(ledger, account, card_id, item)
            conflicts.append(card_id)
            continue
        if state is None or remote_revision > state["revision"]:
            if _digest(item["bundle"]) != remote_hash:
                raise ValueError("云端知识卡摘要不匹配")
            _apply_bundle(ledger, item["bundle"], account, origin)
            with ledger.connection:
                ledger.connection.execute(
                    "INSERT INTO knowledge_sync_state "
                    "(account_id,card_id,revision,payload_sha256,updated_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(account_id,card_id) "
                    "DO UPDATE SET revision=excluded.revision,"
                    "payload_sha256=excluded.payload_sha256,"
                    "conflict_json=NULL,updated_at=excluded.updated_at",
                    (account, card_id, remote_revision, remote_hash, _now()),
                )
            pulled += 1
    for row in ledger.connection.execute(
        "SELECT card_id FROM knowledge_cards ORDER BY card_id"
    ).fetchall():
        card_id = str(row["card_id"])
        if card_id in conflicts:
            continue
        bundle = bundle_for_card(ledger, card_id)
        assert bundle is not None
        projects = bundle["project_ids"]
        if not projects:
            continue
        allowed = ledger.connection.execute(
            "SELECT COUNT(*) FROM project_policies WHERE project_id IN ("
            + ",".join("?" for _ in projects) + ") AND cloud_allowed=1 "
            "AND cloud_account_id=? AND cloud_origin=?",
            [*projects, account, origin],
        ).fetchone()[0]
        if allowed != len(projects):
            continue
        digest = _digest(bundle)
        state = ledger.connection.execute(
            "SELECT revision,payload_sha256 FROM knowledge_sync_state "
            "WHERE account_id=? AND card_id=?", (account, card_id),
        ).fetchone()
        if state is not None and state["payload_sha256"] == digest:
            continue
        revision = int(state["revision"]) if state else 0
        operation_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"growth-knowledge:{account}:{card_id}:{revision}:{digest}"
        ))
        result = await client.put(
            f"/v1/knowledge/bundles/{card_id}", headers=headers,
            json={"operation_id": operation_id, "base_revision": revision,
                  "project_ids": projects, "bundle": bundle},
        )
        if result.status_code == 409:
            latest = await client.get("/v1/knowledge/bundles", headers=headers)
            latest.raise_for_status()
            current = next(
                (entry for entry in latest.json()["bundles"]
                 if entry["card_id"] == card_id), None
            )
            if current is None:
                raise ValueError("知识卡并发写入后云端记录不可读取")
            _record_conflict(ledger, account, card_id, current)
            conflicts.append(card_id)
            continue
        result.raise_for_status()
        body = result.json()
        with ledger.connection:
            ledger.connection.execute(
                "INSERT INTO knowledge_sync_state "
                "(account_id,card_id,revision,payload_sha256,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(account_id,card_id) "
                "DO UPDATE SET revision=excluded.revision,"
                "payload_sha256=excluded.payload_sha256,"
                "conflict_json=NULL,updated_at=excluded.updated_at",
                (account, card_id, int(body["revision"]), digest, _now()),
            )
        uploaded += 1
    return {"knowledge_pulled": pulled, "knowledge_uploaded": uploaded,
            "knowledge_conflicts": conflicts}
