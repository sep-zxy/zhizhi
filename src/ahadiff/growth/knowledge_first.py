"""Concept-centred candidate review on top of the existing growth ledger."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .git_snapshot import git
from .local import GrowthLocalRepository
from .recommendations import apply_decisions
from .knowledge_wiki import articles, vault


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _key(value: str) -> str:
    """A matching hint, never the durable identity of a concept."""
    folded = re.sub(r"[\s\W_]+", "", value.casefold(), flags=re.UNICODE)
    # Only match a mechanism when the title names its distinctive evidence.
    # A broad word such as “线程池” or “缓存” alone must not merge distinct lessons.
    if "latestrequest" in folded or ("requestid" in folded and "竞态" in folded):
        return "request-race:latest-request"
    if "pricecache" in folded and ("ttl" in folded or "过期" in folded):
        return "cache-expiry:price-cache"
    if "ownerid" in folded and any(word in folded for word in ("授权", "归属", "越权")):
        return "resource-authorization:owner-id"
    return folded[:180]


def _source_shas(row: Any) -> list[str]:
    scope = json.loads(row["capture_scope"])
    if scope.get("kind") == "commit" and scope.get("sha"):
        return [str(scope["sha"])]
    try:
        raw = git(Path(row["canonical_local_path"]), "rev-list", "--reverse",
                  f"{row['resolved_base_sha']}..{row['head_sha']}")
        return [sha for sha in raw.decode().splitlines() if len(sha) == 40]
    except (OSError, ValueError):
        return []


def _opportunities(ledger: GrowthLocalRepository, ids: list[str] | None = None) -> list[Any]:
    clause = ""
    params: list[str] = []
    if ids is not None:
        if not ids:
            return []
        clause = " AND opportunities.opportunity_id IN (" + ",".join("?" for _ in ids) + ")"
        params = ids
    return ledger.connection.execute(
        "SELECT opportunities.*, proposals.proposal_id, proposals.title AS topic_title, "
        "features.project_id, snapshots.snapshot_id, snapshots.capture_scope, "
        "snapshots.resolved_base_sha, snapshots.head_sha, "
        "bindings.binding_id, bindings.canonical_local_path "
        "FROM opportunities JOIN analysis_runs USING(analysis_id) "
        "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
        "LEFT JOIN topic_proposals AS proposals ON proposals.opportunity_id=opportunities.opportunity_id "
        "AND proposals.ordinal=0 "
        "LEFT JOIN snapshot_bindings ON snapshot_bindings.snapshot_id=snapshots.snapshot_id "
        "LEFT JOIN bindings ON bindings.binding_id=snapshot_bindings.binding_id "
        "WHERE opportunities.recommendation_status IN ('pending','deferred')" + clause +
        " ORDER BY opportunities.created_at,opportunities.opportunity_id",
        params,
    ).fetchall()


def _matching_card(ledger: GrowthLocalRepository, key: str) -> str | None:
    rows = ledger.connection.execute(
        "SELECT DISTINCT cards.card_id FROM knowledge_concepts AS concepts "
        "JOIN knowledge_cards AS cards USING(concept_id) "
        "LEFT JOIN knowledge_concept_aliases AS aliases USING(concept_id) "
        "WHERE concepts.canonical_key=? OR aliases.alias_key=?",
        (key, key),
    ).fetchall()
    return str(rows[0][0]) if len(rows) == 1 else None


def candidates(ledger: GrowthLocalRepository, ids: list[str] | None = None) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    overrides = {str(row["opportunity_id"]): row for row in ledger.connection.execute(
        "SELECT * FROM knowledge_candidate_overrides"
    )}
    for row in _opportunities(ledger, ids):
        override = overrides.get(str(row["opportunity_id"]))
        title = str(override["title"] if override else row["title"])
        key = str(override["concept_key"]) if override else _key(title)
        if not key:
            key = "opportunity:" + str(row["opportunity_id"])
        group = groups.setdefault(key, {
            "candidate_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "growth-candidate:" + key)),
            "concept_key": key,
            "title": title,
            "reason": "",
            "status": "pending",
            "opportunity_ids": [],
            "source_commit_shas": [],
            "sources": [],
            "project_ids": [],
            "suggested_card_id": _matching_card(ledger, key),
            "uncertainties": [],
            "discussions": [],
        })
        group["opportunity_ids"].append(str(row["opportunity_id"]))
        group["reason"] = str(override["reason"] if override else row["reason"]) if not group["reason"] else group["reason"]
        group["status"] = "deferred" if row["recommendation_status"] == "deferred" else "pending"
        if row["project_id"] not in group["project_ids"]:
            group["project_ids"].append(str(row["project_id"]))
        shas = _source_shas(row) if row["canonical_local_path"] else []
        for sha in shas:
            if sha not in group["source_commit_shas"]:
                group["source_commit_shas"].append(sha)
                group["sources"].append({
                    "binding_id": row["binding_id"], "commit_sha": sha,
                    "project_id": row["project_id"],
                })
        for uncertainty in json.loads(row["uncertainties_json"]):
            if uncertainty not in group["uncertainties"]:
                group["uncertainties"].append(uncertainty)
        for discussion in ledger.connection.execute(
            "SELECT sessions.session_id,sessions.suggestion_json,sessions.topic_id "
            "FROM knowledge_candidate_discussions AS links "
            "JOIN local_explorations AS sessions USING(session_id) "
            "WHERE links.opportunity_id=?", (row["opportunity_id"],),
        ):
            if any(item["session_id"] == discussion["session_id"]
                   for item in group["discussions"]):
                continue
            suggestion = json.loads(discussion["suggestion_json"] or "null")
            group["discussions"].append({
                "session_id": discussion["session_id"],
                "suggestion": suggestion,
                "topic_id": discussion["topic_id"],
            })
    result = list(groups.values())
    for group in result:
        count = len(group["opportunity_ids"])
        group["evidence_count"] = len(group["sources"])
        if count > 1:
            group["reason"] = f"{count} 个分析结果围绕同一主题；{group['reason']}"
    return sorted(result, key=lambda item: (-item["evidence_count"], item["title"]))


def split_candidate(
    ledger: GrowthLocalRepository, candidate_id: str, *, request_id: str,
    opportunity_ids: list[str], title: str, reason: str,
) -> dict[str, Any]:
    """Move selected analyses into a separately reviewable concept candidate."""
    title, reason = title.strip(), reason.strip()
    if not title or len(title) > 200 or len(reason) < 8 or len(reason) > 1000:
        raise ValueError("请填写拆分后的主题及具体理由")
    ids = list(dict.fromkeys(opportunity_ids))
    payload = {"candidate_id": candidate_id, "opportunity_ids": ids,
               "title": title, "reason": reason}
    digest = hashlib.sha256(_json(payload).encode()).hexdigest()
    previous = ledger.connection.execute(
        "SELECT request_hash,result_json FROM knowledge_candidate_decisions WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if previous:
        if previous["request_hash"] != digest:
            raise ValueError("同一请求不能更换拆分内容")
        return json.loads(previous["result_json"])
    candidate = next((item for item in candidates(ledger)
                      if item["candidate_id"] == candidate_id), None)
    if candidate is None or not ids or len(ids) >= len(candidate["opportunity_ids"]) or not set(ids) <= set(candidate["opportunity_ids"]):
        raise ValueError("请选择候选中的部分分析结果拆分")
    key = "manual:" + str(uuid.uuid4())
    with ledger.connection:
        for opportunity_id in ids:
            ledger.connection.execute(
                "INSERT INTO knowledge_candidate_overrides VALUES (?,?,?,?,?,?)",
                (opportunity_id, key, title, reason, request_id, _now()),
            )
        result = {"candidates": candidates(ledger), "split_concept_key": key}
        ledger.connection.execute(
            "INSERT INTO knowledge_candidate_decisions VALUES (?,?,?,?)",
            (request_id, digest, _json(result), _now()),
        )
    return result


def link_discussion(
    ledger: GrowthLocalRepository, candidate_id: str, session_id: str,
) -> dict[str, Any]:
    candidate = next((item for item in candidates(ledger)
                      if item["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise ValueError("知识候选不存在或已确认")
    session = ledger.connection.execute(
        "SELECT project_id,suggestion_json FROM local_explorations WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if session is None or not session["suggestion_json"]:
        raise ValueError("探讨尚未产生可审阅的知识建议")
    if session["project_id"] not in candidate["project_ids"]:
        raise ValueError("探讨项目与候选代码证据不匹配")
    with ledger.connection:
        for opportunity_id in candidate["opportunity_ids"]:
            ledger.connection.execute(
                "INSERT OR IGNORE INTO knowledge_candidate_discussions VALUES (?,?,?)",
                (session_id, opportunity_id, _now()),
            )
    return next(item for item in candidates(ledger)
                if item["candidate_id"] == candidate_id)


def cards(ledger: GrowthLocalRepository) -> list[dict[str, Any]]:
    rows = ledger.connection.execute(
        "SELECT cards.card_id,cards.concept_id,concepts.title,cards.learning_goal,"
        "cards.back_answer,cards.back_explanation,cards.card_version,"
        "tasks.question AS front_question,tasks.progress,"
        "COALESCE(learning.stage,'front') AS learning_stage,"
        "COALESCE(marks.mark,'none') AS followup_mark,"
        "COALESCE((SELECT draft.status FROM knowledge_wiki_drafts AS draft "
        "WHERE draft.card_id=cards.card_id ORDER BY draft.created_at DESC LIMIT 1),"
        "(SELECT 'published' FROM knowledge_wiki_articles AS article "
        "WHERE article.concept_id=cards.concept_id)) AS wiki_draft_status,"
        "COALESCE((SELECT draft.updated_at FROM knowledge_wiki_drafts AS draft "
        "WHERE draft.card_id=cards.card_id ORDER BY draft.created_at DESC LIMIT 1),"
        "(SELECT article.published_at FROM knowledge_wiki_articles AS article "
        "WHERE article.concept_id=cards.concept_id)) AS wiki_updated_at,"
        "(SELECT COUNT(*) FROM card_source_commits AS links WHERE links.card_id=cards.card_id) "
        "AS evidence_count FROM knowledge_cards AS cards "
        "JOIN growth_tasks AS tasks ON tasks.task_id=cards.card_id "
        "LEFT JOIN knowledge_concepts AS concepts ON concepts.concept_id=cards.concept_id "
        "LEFT JOIN card_learning_progress AS learning ON learning.card_id=cards.card_id "
        "LEFT JOIN card_followup_marks AS marks ON marks.card_id=cards.card_id "
        "ORDER BY cards.updated_at DESC"
    ).fetchall()
    result = []
    for row in rows:
        card = dict(row)
        sources = [dict(item) for item in ledger.connection.execute(
            "SELECT links.binding_id,bindings.project_id,links.commit_sha "
            "FROM card_source_commits AS links JOIN bindings USING(binding_id) "
            "WHERE links.card_id=? ORDER BY links.created_at,links.commit_sha",
            (row["card_id"],),
        )]
        card["source_commits"] = sources
        card["source_commit_shas"] = [item["commit_sha"] for item in sources]
        card["project_ids"] = list(dict.fromkeys(
            item["project_id"] for item in sources
        ))
        result.append(card)
    return result


def workspace(ledger: GrowthLocalRepository) -> dict[str, Any]:
    settings = vault(ledger)
    return {"candidates": candidates(ledger), "cards": cards(ledger),
            "wiki_articles": articles(ledger), "vault_path": settings["path"]}


def commit_detail(
    ledger: GrowthLocalRepository, binding_id: str, commit_sha: str,
) -> dict[str, Any]:
    """Return one scanned, reachable commit and its local patch for evidence drilldown."""
    row = ledger.connection.execute(
        "SELECT history.*,bindings.canonical_local_path,bindings.project_id "
        "FROM git_history_commits AS history JOIN bindings USING(binding_id) "
        "WHERE history.binding_id=? AND history.commit_sha=? AND history.reachable=1",
        (binding_id, commit_sha),
    ).fetchone()
    if row is None:
        raise ValueError("提交不在此仓库的已扫描历史中")
    patch = git(Path(row["canonical_local_path"]), "show", "--format=",
                "--no-ext-diff", "--unified=5", commit_sha).decode(
                    "utf-8", errors="replace")
    maximum = 120000
    cards = [dict(item) for item in ledger.connection.execute(
        "SELECT links.card_id,concepts.title,links.link_basis "
        "FROM card_source_commits AS links "
        "JOIN knowledge_cards AS cards USING(card_id) "
        "LEFT JOIN knowledge_concepts AS concepts USING(concept_id) "
        "WHERE links.binding_id=? AND links.commit_sha=? ORDER BY concepts.title",
        (binding_id, commit_sha),
    )]
    matches = [
        {"candidate_id": item["candidate_id"], "title": item["title"],
         "status": item["status"]}
        for item in candidates(ledger)
        if any(source["binding_id"] == binding_id and
               source["commit_sha"] == commit_sha for source in item["sources"])
    ]
    return {
        "binding_id": binding_id, "project_id": row["project_id"],
        "commit_sha": commit_sha, "title": row["title"],
        "author_name": row["author_name"], "authored_at": row["authored_at"],
        "category": row["category"], "paths": json.loads(row["paths_json"]),
        "patch": patch[:maximum], "truncated": len(patch) > maximum,
        "cards": cards, "candidates": matches,
    }


def decide_candidate(
    ledger: GrowthLocalRepository, candidate_id: str, *, request_id: str,
    action: str, card_id: str | None = None, title: str | None = None,
    additional_candidate_ids: list[str] | None = None,
    topic_id: str | None = None,
) -> dict[str, Any]:
    if action not in {"confirm_new", "merge", "defer", "ignore"}:
        raise ValueError("候选决定无效")
    payload = {"candidate_id": candidate_id, "action": action,
               "card_id": card_id, "title": title,
               "additional_candidate_ids": additional_candidate_ids or [],
               "topic_id": topic_id}
    digest = hashlib.sha256(_json(payload).encode()).hexdigest()
    previous = ledger.connection.execute(
        "SELECT request_hash,result_json FROM knowledge_candidate_decisions WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if previous:
        if previous["request_hash"] != digest:
            raise ValueError("同一请求不能更换候选决定")
        return json.loads(previous["result_json"])
    candidate = next((item for item in candidates(ledger)
                      if item["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise ValueError("候选不存在或已经确认")
    selected = [candidate]
    all_candidates = {item["candidate_id"]: item for item in candidates(ledger)}
    for other_id in additional_candidate_ids or []:
        if other_id == candidate_id or other_id not in all_candidates or any(
            item["candidate_id"] == other_id for item in selected
        ):
            raise ValueError("附加候选不存在或重复")
        selected.append(all_candidates[other_id])
    if len(selected) > 1 and action not in {"confirm_new", "merge"}:
        raise ValueError("多个候选只能一起确认成卡或归并")
    linked_topic_ids = {
        str(discussion["topic_id"])
        for item in selected for discussion in item["discussions"]
        if discussion.get("topic_id")
    }
    if topic_id and action != "confirm_new":
        raise ValueError("只有新建正式卡可以选择探讨主题")
    if topic_id and topic_id not in linked_topic_ids:
        raise ValueError("所选主题未与候选探讨关联")
    if action == "confirm_new" and not topic_id:
        if len(linked_topic_ids) > 1:
            raise ValueError("候选关联了多个探讨主题，请先选择一个")
        topic_id = next(iter(linked_topic_ids), None)
    ids = list(dict.fromkeys(
        opportunity_id for item in selected for opportunity_id in item["opportunity_ids"]
    ))
    if action in {"defer", "ignore"}:
        result = apply_decisions(
            ledger, request_id, confirm_groups=[],
            defer_ids=ids if action == "defer" else [],
            ignore_ids=ids if action == "ignore" else [],
        )
        outcome = {"candidate": candidate | {"status": "deferred" if action == "defer" else "ignored"},
                   "card": None, "decision": result}
    else:
        suggested = {item["suggested_card_id"] for item in selected
                     if item["suggested_card_id"]}
        target = card_id or (next(iter(suggested)) if len(suggested) == 1 else None)
        if len(suggested) > 1 and not card_id:
            raise ValueError("候选指向不同正式卡，请先指定归并目标")
        if action == "merge" and not target:
            raise ValueError("请选择归并的正式卡")
        if action == "confirm_new" and card_id:
            raise ValueError("新建卡不能指定已有卡")
        first = ledger.connection.execute(
            "SELECT proposal_id FROM topic_proposals WHERE opportunity_id=? "
            "ORDER BY ordinal LIMIT 1", (ids[0],),
        ).fetchone()
        if first is None:
            raise ValueError("候选缺少主题建议，无法成卡")
        decision = apply_decisions(
            ledger, request_id,
            confirm_groups=[{"opportunity_ids": ids, "proposal_id": first[0],
                             "existing_card_id": target if action == "merge" else None,
                             "existing_topic_id": topic_id,
                             "title": (title or candidate["title"]).strip()}],
            defer_ids=[], ignore_ids=[],
        )
        resulting_card_id = str(decision["cards"][0]["card_id"])
        with ledger.connection:
            existing = ledger.connection.execute(
                "SELECT concept_id FROM knowledge_cards WHERE card_id=?",
                (resulting_card_id,),
            ).fetchone()
            if existing is None:
                raise ValueError("正式卡创建失败")
            concept_id = existing["concept_id"]
            if not concept_id:
                concept_id = str(uuid.uuid4())
                chosen_title = (title or candidate["title"]).strip()
                concept_key = candidate["concept_key"]
                if ledger.connection.execute(
                    "SELECT 1 FROM knowledge_concepts WHERE canonical_key=?",
                    (concept_key,),
                ).fetchone():
                    concept_key += ":" + concept_id
                ledger.connection.execute(
                    "INSERT INTO knowledge_concepts VALUES (?,?,?,?,?)",
                    (concept_id, chosen_title, concept_key, _now(), _now()),
                )
                ledger.connection.execute(
                    "UPDATE knowledge_cards SET concept_id=? WHERE card_id=?",
                    (concept_id, resulting_card_id),
                )
            if not decision["cards"][0]["reused"]:
                ledger.connection.execute(
                    "UPDATE knowledge_concepts SET title=?,updated_at=? WHERE concept_id=?",
                    ((title or candidate["title"]).strip(), _now(), concept_id),
                )
            ledger.connection.execute(
                "INSERT OR IGNORE INTO knowledge_concept_aliases VALUES (?,?,?)",
                (concept_id, candidate["concept_key"], candidate["title"]),
            )
            for other in selected[1:]:
                ledger.connection.execute(
                    "INSERT OR IGNORE INTO knowledge_concept_aliases VALUES (?,?,?)",
                    (concept_id, other["concept_key"], other["title"]),
                )
        card = next(item for item in cards(ledger) if item["card_id"] == resulting_card_id)
        outcome = {"candidate": candidate | {"status": "confirmed"}, "card": card,
                   "decision": decision}
    with ledger.connection:
        ledger.connection.execute(
            "INSERT INTO knowledge_candidate_decisions VALUES (?,?,?,?)",
            (request_id, digest, _json(outcome), _now()),
        )
    return outcome
