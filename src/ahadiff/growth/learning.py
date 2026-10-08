"""Local learning decisions and append-only answer history for growth tasks."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ahadiff.quiz.schemas import ExerciseKind, QuizKind

if TYPE_CHECKING:
    from .local import GrowthLocalRepository


LEARNING_SCHEMA = """
CREATE TABLE opportunity_batches (
  analysis_id TEXT PRIMARY KEY REFERENCES analysis_runs(analysis_id),
  output_hash TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('live', 'replay')),
  provider_name TEXT, model_name TEXT, provider_request_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE opportunities (
  opportunity_id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES opportunity_batches(analysis_id),
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 2),
  title TEXT NOT NULL, reason TEXT NOT NULL, learning_goal TEXT NOT NULL,
  source_refs_json TEXT NOT NULL, estimated_minutes INTEGER NOT NULL,
  uncertainties_json TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(analysis_id, ordinal)
);
CREATE TABLE topic_proposals (
  proposal_id TEXT PRIMARY KEY,
  opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
  ordinal INTEGER NOT NULL, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending', 'confirmed', 'rejected')),
  topic_id TEXT, created_at TEXT NOT NULL, decided_at TEXT,
  UNIQUE(opportunity_id, ordinal)
);
CREATE TABLE growth_topics (
  topic_id TEXT PRIMARY KEY, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
  created_at TEXT NOT NULL
);
CREATE TABLE growth_tasks (
  task_id TEXT PRIMARY KEY,
  opportunity_id TEXT NOT NULL UNIQUE REFERENCES opportunities(opportunity_id),
  topic_id TEXT NOT NULL REFERENCES growth_topics(topic_id),
  question TEXT NOT NULL, followups_json TEXT NOT NULL,
  estimated_minutes INTEGER NOT NULL CHECK(estimated_minutes BETWEEN 3 AND 5),
  source_refs_json TEXT NOT NULL,
  progress TEXT NOT NULL DEFAULT 'ready'
    CHECK(progress IN ('ready', 'in_progress', 'paused', 'completed', 'dismissed')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE learning_attempts (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES growth_tasks(task_id),
  parent_attempt_id TEXT REFERENCES learning_attempts(attempt_id),
  answer_text TEXT NOT NULL, answer_hash TEXT NOT NULL,
  hint_level INTEGER NOT NULL CHECK(hint_level BETWEEN 0 AND 3),
  actor_origin TEXT NOT NULL CHECK(actor_origin IN ('user', 'simulated_user')),
  status TEXT NOT NULL CHECK(status IN
    ('accepted', 'feedback_pending', 'feedback_ready', 'feedback_failed')),
  feedback_json TEXT, feedback_origin TEXT, feedback_error TEXT,
  created_at TEXT NOT NULL, feedback_at TEXT
);
CREATE TABLE engineering_notes (
  note_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES growth_tasks(task_id),
  topic_id TEXT NOT NULL REFERENCES growth_topics(topic_id),
  revision INTEGER NOT NULL DEFAULT 1,
  author TEXT NOT NULL CHECK(author = 'user'),
  content_text TEXT NOT NULL, content_hash TEXT NOT NULL,
  source_refs_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE growth_timeline (
  evidence_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES growth_tasks(task_id),
  source_type TEXT NOT NULL, source_id TEXT NOT NULL,
  evidence_label TEXT NOT NULL,
  actor_origin TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(source_type, source_id)
);
"""


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardDraft(StrictModel):
    """Short guided practice adapted from AhaDiff's quiz exercise vocabulary."""

    quiz_kind: QuizKind
    exercise_kind: ExerciseKind
    context: str = Field(min_length=1, max_length=700)
    question: str = Field(min_length=1, max_length=600)
    expected_answer: str = Field(min_length=1, max_length=1200)
    hints: list[str] = Field(min_length=1, max_length=3)
    followups: list[str] = Field(default_factory=list, max_length=2)

    @field_validator("quiz_kind", mode="before")
    @classmethod
    def normalize_exercise_kind_alias(cls, value: Any) -> Any:
        # A provider may put the transfer exercise subtype in quiz_kind.
        if value in ("prediction", "completion", "error_reason"):
            return "transfer"
        return value


class OpportunityDraft(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    learning_goal: str = Field(min_length=1, max_length=500)
    source_refs: list[uuid.UUID] = Field(min_length=1, max_length=8)
    estimated_minutes: int = Field(ge=3, le=5)
    topic_suggestions: list[str] = Field(max_length=3)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    card: CardDraft | None = None


class OpportunityBatch(StrictModel):
    opportunities: list[OpportunityDraft] = Field(max_length=3)


class FeedbackDraft(StrictModel):
    summary: str = Field(min_length=1, max_length=1200)
    user_claims: list[str] = Field(default_factory=list, max_length=8)
    code_facts: list[str] = Field(default_factory=list, max_length=8)
    general_principles: list[str] = Field(default_factory=list, max_length=8)
    inferences: list[str] = Field(default_factory=list, max_length=8)
    corrections: list[str] = Field(default_factory=list, max_length=8)
    source_refs: list[uuid.UUID] = Field(max_length=8)


class GrowthLearningService:
    """Apply model output and user actions to one existing local growth ledger."""

    def __init__(self, ledger: GrowthLocalRepository) -> None:
        self.ledger = ledger
        self.db = ledger.connection

    def _source_ids(self, snapshot_id: str) -> set[str]:
        return {
            str(row[0])
            for row in self.db.execute(
                "SELECT source_ref_id FROM source_refs WHERE snapshot_id=?",
                (snapshot_id,),
            )
        }

    def save_opportunities(
        self,
        analysis_id: str,
        result: OpportunityBatch | dict[str, Any],
        *,
        origin: Literal["live", "replay"],
        trace_id: str,
        provider_name: str | None = None,
        model_name: str | None = None,
        provider_request_id: str | None = None,
    ) -> list[str]:
        batch = OpportunityBatch.model_validate(result)
        analysis = self.db.execute(
            "SELECT snapshot_id, mode, status FROM analysis_runs WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        if analysis is None or analysis["status"] != "succeeded":
            raise ValueError("分析尚未成功")
        if origin == "live" and (analysis["mode"] != "live" or not provider_name or not model_name):
            raise ValueError("live 机会需要 live 分析与真实 provider/model 记录")
        allowed_refs = self._source_ids(str(analysis["snapshot_id"]))
        for opportunity in batch.opportunities:
            if not {str(ref) for ref in opportunity.source_refs} <= allowed_refs:
                raise ValueError("机会引用了不存在或不属于本快照的源码")
        payload = batch.model_dump(mode="json")
        output_hash = _hash(_json(payload))
        existing = self.db.execute(
            "SELECT output_hash, origin FROM opportunity_batches WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        if existing:
            if existing["output_hash"] != output_hash or existing["origin"] != origin:
                raise ValueError("同一次分析不能静默替换机会")
            return [
                str(row[0])
                for row in self.db.execute(
                    "SELECT opportunity_id FROM opportunities WHERE analysis_id=? ORDER BY ordinal",
                    (analysis_id,),
                )
            ]
        created = _now()
        project_id = self.ledger.project_id_for_snapshot(str(analysis["snapshot_id"]))
        opportunity_ids: list[str] = []
        with nullcontext() if self.db.in_transaction else self.db:
            self.db.execute(
                "INSERT INTO opportunity_batches VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis_id,
                    output_hash,
                    origin,
                    provider_name,
                    model_name,
                    provider_request_id,
                    created,
                ),
            )
            for ordinal, opportunity in enumerate(batch.opportunities):
                opportunity_id = _id()
                opportunity_ids.append(opportunity_id)
                recommendation_key = _hash(
                    project_id + ":" + " ".join(opportunity.learning_goal.casefold().split())
                )
                self.db.execute(
                    "INSERT INTO opportunities (opportunity_id,analysis_id,ordinal,title,reason,"
                    "learning_goal,source_refs_json,estimated_minutes,uncertainties_json,created_at,"
                    "card_json,recommendation_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        opportunity_id,
                        analysis_id,
                        ordinal,
                        opportunity.title,
                        opportunity.reason,
                        opportunity.learning_goal,
                        _json([str(ref) for ref in opportunity.source_refs]),
                        opportunity.estimated_minutes,
                        _json(opportunity.uncertainties),
                        created,
                        _json(opportunity.card.model_dump(mode="json")) if opportunity.card else None,
                        recommendation_key,
                    ),
                )
                for proposal_ordinal, title in enumerate(opportunity.topic_suggestions):
                    if not title.strip():
                        raise ValueError("主题建议不能为空")
                    proposal_id = _id()
                    self.db.execute(
                        "INSERT INTO topic_proposals "
                        "(proposal_id, opportunity_id, ordinal, title, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (proposal_id, opportunity_id, proposal_ordinal, title.strip(), created),
                    )
                    self.ledger.record_event(
                        trace_id,
                        "topic_proposal",
                        proposal_id,
                        "topic_proposed",
                        {"opportunity_id": opportunity_id},
                    )
                self.ledger.record_event(
                    trace_id,
                    "opportunity",
                    opportunity_id,
                    "opportunity_saved",
                    {
                        "analysis_id": analysis_id,
                        "source_refs": [str(ref) for ref in opportunity.source_refs],
                    },
                )
            self.ledger.queue_enabled_publications(
                self.ledger.project_id_for_snapshot(str(analysis["snapshot_id"]))
            )
        return opportunity_ids

    def decide_topic(
        self, proposal_id: str, decision: Literal["confirm", "reject"], *, trace_id: str,
        existing_topic_id: str | None = None, existing_topic_title: str | None = None,
        title_override: str | None = None,
    ) -> str | None:
        if existing_topic_id is not None and (
            decision != "confirm" or not existing_topic_title
        ):
            raise ValueError("关联已有主题需要有效的确认决定和主题标题")
        row = self.db.execute(
            "SELECT status, title, topic_id FROM topic_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ValueError("主题建议不存在")
        chosen_title = (title_override or str(row["title"])).strip()
        if not chosen_title or len(chosen_title) > 200:
            raise ValueError("主题标题无效")
        target_status = "confirmed" if decision == "confirm" else "rejected"
        if row["status"] != "pending":
            if row["status"] != target_status or (
                existing_topic_id is not None and row["topic_id"] != existing_topic_id
            ):
                raise ValueError("主题建议已有相反决定")
            return str(row["topic_id"]) if row["topic_id"] else None
        local_match = self.db.execute(
            "SELECT topic_id FROM growth_topics WHERE title=? AND status='active' "
            "ORDER BY created_at LIMIT 1", (chosen_title,),
        ).fetchone() if decision == "confirm" and not existing_topic_id else None
        topic_id = (existing_topic_id or (str(local_match["topic_id"]) if local_match else _id())) \
            if decision == "confirm" else None
        with nullcontext() if self.db.in_transaction else self.db:
            if topic_id:
                if existing_topic_id:
                    self.db.execute(
                        "INSERT INTO growth_topics VALUES (?, ?, 'active', ?) "
                        "ON CONFLICT(topic_id) DO UPDATE SET title=excluded.title, "
                        "status='active'",
                        (topic_id, existing_topic_title, _now()),
                    )
                elif not local_match:
                    self.db.execute(
                        "INSERT INTO growth_topics VALUES (?, ?, 'active', ?)",
                        (topic_id, chosen_title, _now()),
                    )
            self.db.execute(
                "UPDATE topic_proposals SET title=?, status=?, topic_id=?, decided_at=? "
                "WHERE proposal_id=? AND status='pending'",
                (chosen_title, target_status, topic_id, _now(), proposal_id),
            )
            self.ledger.record_event(
                trace_id,
                "topic_proposal",
                proposal_id,
                f"topic_{target_status}",
                {"topic_id": topic_id, "reuse_existing": bool(existing_topic_id or local_match)},
            )
        return topic_id

    def create_task(
        self,
        opportunity_id: str,
        topic_id: str,
        question: str,
        *,
        trace_id: str,
        followups: list[str] | None = None,
        module_id: str | None = None,
        module_index_revision: str | None = None,
    ) -> str:
        question = question.strip()
        followups = followups or []
        if (
            not question
            or len(question) > 600
            or len(followups) > 2
            or any(not item.strip() or len(item) > 400 for item in followups)
        ):
            raise ValueError("卡片需要一个主要提问及最多两个有效追问")
        if (module_id is None) != (module_index_revision is None):
            raise ValueError("模块 ID 与索引修订必须同时提供")
        if module_index_revision is not None and (
            len(module_index_revision) != 64
            or any(char not in "0123456789abcdef" for char in module_index_revision)
        ):
            raise ValueError("模块索引修订不是有效的 SHA-256")
        opportunity = self.db.execute(
            "SELECT source_refs_json, estimated_minutes, learning_goal, reason, card_json "
            "FROM opportunities WHERE opportunity_id=?",
            (opportunity_id,),
        ).fetchone()
        if opportunity is None:
            raise ValueError("机会不存在")
        confirmed = self.db.execute(
            "SELECT 1 FROM topic_proposals WHERE opportunity_id=? "
            "AND topic_id=? AND status='confirmed'",
            (opportunity_id, topic_id),
        ).fetchone()
        if confirmed is None:
            raise ValueError("卡片主题尚未由用户确认")
        existing = self.db.execute(
            "SELECT task_id, topic_id, question, followups_json, module_id, "
            "module_index_revision "
            "FROM growth_tasks WHERE opportunity_id=?",
            (opportunity_id,),
        ).fetchone()
        if existing:
            if (existing["topic_id"], existing["question"], existing["followups_json"],
                    existing["module_id"], existing["module_index_revision"]) != (
                topic_id,
                question,
                _json(followups),
                module_id,
                module_index_revision,
            ):
                raise ValueError("同一机会的卡片不能静默替换")
            return str(existing["task_id"])
        task_id = _id()
        card_draft = json.loads(opportunity["card_json"]) if opportunity["card_json"] else {}
        created = _now()
        captured = self.db.execute(
            "SELECT snapshots.capture_scope, snapshot_bindings.binding_id "
            "FROM opportunities JOIN analysis_runs USING(analysis_id) "
            "JOIN snapshots USING(snapshot_id) JOIN snapshot_bindings USING(snapshot_id) "
            "WHERE opportunities.opportunity_id=? "
            "ORDER BY snapshot_bindings.captured_at DESC LIMIT 1", (opportunity_id,),
        ).fetchone()
        capture_scope = json.loads(captured["capture_scope"]) if captured else {}
        commit_sha = capture_scope.get("sha") if capture_scope.get("kind") == "commit" else None
        with nullcontext() if self.db.in_transaction else self.db:
            self.db.execute(
                "INSERT INTO growth_tasks (task_id,opportunity_id,topic_id,question,"
                "followups_json,estimated_minutes,source_refs_json,progress,created_at,"
                "updated_at,module_id,module_index_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)",
                (
                    task_id,
                    opportunity_id,
                    topic_id,
                    question,
                    _json(followups),
                    opportunity["estimated_minutes"],
                    opportunity["source_refs_json"],
                    created,
                    created,
                    module_id,
                    module_index_revision,
                ),
            )
            self.db.execute(
                "INSERT INTO knowledge_cards "
                "(card_id,learning_goal,back_answer,back_explanation,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, opportunity["learning_goal"],
                 card_draft.get("expected_answer", ""),
                 "\n\n".join(filter(None, (opportunity["reason"],
                                            card_draft.get("context", "")))),
                 created, created),
            )
            concept_id = _id()
            self.db.execute(
                "INSERT INTO knowledge_concepts VALUES (?,?,?,?,?)",
                (concept_id, str(opportunity["learning_goal"]),
                 "card:" + task_id, created, created),
            )
            self.db.execute(
                "UPDATE knowledge_cards SET concept_id=? WHERE card_id=?",
                (concept_id, task_id),
            )
            if commit_sha and captured:
                self.db.execute(
                    "INSERT INTO card_source_commits VALUES (?,?,?,?,?)",
                    (task_id, commit_sha, captured["binding_id"], "captured", created),
                )
                self.db.execute(
                    "UPDATE knowledge_cards SET source_link_status='linked' WHERE card_id=?",
                    (task_id,),
                )
            self.db.execute(
                "UPDATE opportunities SET recommendation_status='confirmed', "
                "canonical_card_id=? WHERE opportunity_id=?",
                (task_id, opportunity_id),
            )
            self.ledger.record_event(
                trace_id,
                "task",
                task_id,
                "task_created",
                {"opportunity_id": opportunity_id, "topic_id": topic_id,
                 "module_id": module_id, "module_index_revision": module_index_revision},
            )
        return task_id

    def submit_answer(
        self,
        task_id: str,
        answer_text: str,
        *,
        trace_id: str,
        actor_origin: Literal["user", "simulated_user"],
        parent_attempt_id: str | None = None,
        hint_level: int = 0,
    ) -> str:
        if not answer_text.strip() or not 0 <= hint_level <= 3:
            raise ValueError("回答不能为空，提示程度必须为 0–3")
        issued_hint_level = int(self.db.execute(
            "SELECT COALESCE(MAX(level), 0) FROM growth_task_hints WHERE task_id=?",
            (task_id,),
        ).fetchone()[0])
        if hint_level < issued_hint_level:
            raise ValueError("回答的提示程度不能低于已查看的提示")
        task = self.db.execute(
            "SELECT task_id, progress FROM growth_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise ValueError("卡片不存在")
        if task["progress"] in {"completed", "dismissed"}:
            raise ValueError("已结束的卡片需先重新打开")
        if parent_attempt_id:
            parent = self.db.execute(
                "SELECT task_id FROM learning_attempts WHERE attempt_id=?", (parent_attempt_id,)
            ).fetchone()
            if parent is None or parent["task_id"] != task_id:
                raise ValueError("修正回答必须关联同一卡片的旧回答")
        attempt_id = _id()
        created = _now()
        with self.db:
            self.db.execute(
                "INSERT INTO learning_attempts "
                "(attempt_id, task_id, parent_attempt_id, answer_text, answer_hash, "
                "hint_level, actor_origin, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?)",
                (
                    attempt_id,
                    task_id,
                    parent_attempt_id,
                    answer_text,
                    _hash(answer_text),
                    hint_level,
                    actor_origin,
                    created,
                ),
            )
            self.db.execute(
                "UPDATE growth_tasks SET progress='in_progress', updated_at=? WHERE task_id=?",
                (created, task_id),
            )
            self.db.execute(
                "INSERT INTO growth_timeline VALUES (?, ?, 'attempt', ?, 'practicing', ?, ?)",
                (_id(), task_id, attempt_id, actor_origin, created),
            )
            self.ledger.record_event(
                trace_id,
                "attempt",
                attempt_id,
                "answer_saved",
                {
                    "task_id": task_id,
                    "parent_attempt_id": parent_attempt_id,
                    "answer_hash": _hash(answer_text),
                },
            )
        return attempt_id

    def request_hint(self, task_id: str, request_id: str, *, trace_id: str) -> dict[str, Any]:
        """Return one durable local thinking cue without sending code to a provider."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT * FROM growth_task_hints WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is not None:
                if existing["task_id"] != task_id:
                    raise ValueError("提示请求 ID 已用于另一张卡片")
                return dict(existing)
            task = self.db.execute(
                "SELECT growth_tasks.progress, growth_tasks.source_refs_json, "
                "opportunities.card_json FROM growth_tasks JOIN opportunities USING(opportunity_id) "
                "WHERE growth_tasks.task_id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise ValueError("卡片不存在")
            if task["progress"] in {"completed", "dismissed"}:
                raise ValueError("已结束的卡片不能再请求提示")
            level = int(self.db.execute(
                "SELECT COALESCE(MAX(level), 0) + 1 FROM growth_task_hints WHERE task_id=?",
                (task_id,),
            ).fetchone()[0])
            if level > 3:
                raise ValueError("本张卡片的三条提示已全部查看")
            source_ids = json.loads(task["source_refs_json"])
            source = self.db.execute(
                "SELECT relative_path FROM source_refs WHERE source_ref_id=?",
                (source_ids[0],),
            ).fetchone() if source_ids else None
            path = str(source["relative_path"]) if source else "相关源码"
            fallback_hints = (
                f"先查看 {path}，指出与题目相关的输入、输出和关键调用。",
                f"对照 {path} 的修改前后，找出改变行为的条件或调用，并说明它何时执行。",
                "把回答分成源码直接证明的事实与需要运行或依赖外部实现才能确认的推断。",
            )
            card = json.loads(task["card_json"]) if task["card_json"] else None
            hints = tuple(card["hints"]) if card else fallback_hints
            if level > len(hints):
                raise ValueError("本张卡片的提示已全部查看")
            hint = {
                "hint_id": _id(), "task_id": task_id, "request_id": request_id,
                "level": level, "hint_text": hints[level - 1], "created_at": _now(),
            }
            self.db.execute(
                "INSERT INTO growth_task_hints "
                "(hint_id, task_id, request_id, level, hint_text, created_at) "
                "VALUES (:hint_id, :task_id, :request_id, :level, :hint_text, :created_at)",
                hint,
            )
            self.ledger.record_event(
                trace_id, "task", task_id, "hint_viewed", {"level": level}
            )
        return hint

    def set_task_progress(
        self,
        task_id: str,
        target: Literal["in_progress", "paused", "completed", "dismissed"],
        *,
        trace_id: str,
    ) -> None:
        current_row = self.db.execute(
            "SELECT progress FROM growth_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if current_row is None:
            raise ValueError("卡片不存在")
        current = str(current_row["progress"])
        if current == target:
            return
        allowed = {
            "ready": {"in_progress", "dismissed"},
            "in_progress": {"paused", "completed", "dismissed"},
            "paused": {"in_progress", "dismissed"},
            "completed": {"in_progress"},
            "dismissed": {"in_progress"},
        }
        if target not in allowed[current]:
            raise ValueError(f"不允许从 {current} 转换到 {target}")
        with self.db:
            self.db.execute(
                "UPDATE growth_tasks SET progress=?, updated_at=? WHERE task_id=?",
                (target, _now(), task_id),
            )
            self.ledger.record_event(
                trace_id, "task", task_id, "task_progress_changed", {"from": current, "to": target}
            )

    def mark_feedback_pending(self, attempt_id: str, *, trace_id: str) -> None:
        with self.db:
            cursor = self.db.execute(
                "UPDATE learning_attempts SET status='feedback_pending', feedback_error=NULL "
                "WHERE attempt_id=? AND status IN ('accepted', 'feedback_failed')",
                (attempt_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("回答不存在或反馈状态不可重新提交")
            self.ledger.record_event(trace_id, "attempt", attempt_id, "feedback_pending", {})

    def save_feedback(
        self,
        attempt_id: str,
        feedback: FeedbackDraft | dict[str, Any],
        *,
        origin: Literal["live", "replay"],
        trace_id: str,
    ) -> None:
        body = FeedbackDraft.model_validate(feedback)
        row = self.db.execute(
            "SELECT learning_attempts.status, growth_tasks.source_refs_json, "
            "opportunity_batches.origin AS opportunity_origin "
            "FROM learning_attempts JOIN growth_tasks "
            "ON growth_tasks.task_id=learning_attempts.task_id "
            "JOIN opportunities ON opportunities.opportunity_id=growth_tasks.opportunity_id "
            "JOIN opportunity_batches ON opportunity_batches.analysis_id=opportunities.analysis_id "
            "WHERE learning_attempts.attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise ValueError("回答不存在")
        if origin == "live" and row["opportunity_origin"] != "live":
            raise ValueError("回放机会不能标记为 live 反馈")
        if not {str(ref) for ref in body.source_refs} <= set(json.loads(row["source_refs_json"])):
            raise ValueError("反馈引用了未验证的源码")
        payload = _json(body.model_dump(mode="json"))
        if row["status"] == "feedback_ready":
            stored = self.db.execute(
                "SELECT feedback_json, feedback_origin FROM learning_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            assert stored is not None
            if stored["feedback_json"] != payload or stored["feedback_origin"] != origin:
                raise ValueError("反馈不可静默覆盖")
            return
        with nullcontext() if self.db.in_transaction else self.db:
            self.db.execute(
                "UPDATE learning_attempts SET status='feedback_ready', feedback_json=?, "
                "feedback_origin=?, feedback_error=NULL, feedback_at=? WHERE attempt_id=?",
                (payload, origin, _now(), attempt_id),
            )
            self.ledger.record_event(
                trace_id, "attempt", attempt_id, "feedback_ready", {"origin": origin}
            )

    def feedback_failed(self, attempt_id: str, reason: str, *, trace_id: str) -> None:
        with self.db:
            cursor = self.db.execute(
                "UPDATE learning_attempts SET status='feedback_failed', feedback_error=?, "
                "feedback_at=? WHERE attempt_id=? AND status!='feedback_ready'",
                (reason[:500], _now(), attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("回答不存在或反馈已经成功")
            self.ledger.record_event(
                trace_id, "attempt", attempt_id, "feedback_failed", {"reason": reason[:500]}
            )

    def save_user_note(self, task_id: str, content_text: str, *, trace_id: str) -> str:
        if not content_text.strip():
            raise ValueError("用户笔记正文不能为空")
        task = self.db.execute(
            "SELECT topic_id, source_refs_json FROM growth_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise ValueError("卡片不存在")
        note_id, created = _id(), _now()
        with self.db:
            self.db.execute(
                "INSERT INTO engineering_notes VALUES (?, ?, ?, 1, 'user', ?, ?, ?, ?, ?)",
                (
                    note_id,
                    task_id,
                    task["topic_id"],
                    content_text,
                    _hash(content_text),
                    task["source_refs_json"],
                    created,
                    created,
                ),
            )
            self.db.execute(
                "INSERT INTO growth_timeline VALUES (?, ?, 'note', ?, 'encountered', 'user', ?)",
                (_id(), task_id, note_id, created),
            )
            self.ledger.record_event(
                trace_id,
                "note",
                note_id,
                "note_saved",
                {"task_id": task_id, "content_hash": _hash(content_text)},
            )
        return note_id

    def export_task_chain(self, task_id: str) -> dict[str, Any]:
        task = self.db.execute("SELECT * FROM growth_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("卡片不存在")
        opportunity = self.db.execute(
            "SELECT * FROM opportunities WHERE opportunity_id=?", (task["opportunity_id"],)
        ).fetchone()
        assert opportunity is not None
        proposals = self.db.execute(
            "SELECT * FROM topic_proposals WHERE opportunity_id=? ORDER BY ordinal",
            (task["opportunity_id"],),
        ).fetchall()
        attempts = self.db.execute(
            "SELECT * FROM learning_attempts WHERE task_id=? ORDER BY created_at, rowid", (task_id,)
        ).fetchall()
        notes = self.db.execute(
            "SELECT * FROM engineering_notes WHERE task_id=? ORDER BY created_at, rowid", (task_id,)
        ).fetchall()
        timeline = self.db.execute(
            "SELECT * FROM growth_timeline WHERE task_id=? ORDER BY created_at, rowid", (task_id,)
        ).fetchall()
        hints = self.db.execute(
            "SELECT * FROM growth_task_hints WHERE task_id=? ORDER BY level", (task_id,)
        ).fetchall()
        code_row = self.db.execute(
            "SELECT selected_patch FROM analysis_model_requests WHERE analysis_id=?",
            (opportunity["analysis_id"],),
        ).fetchone()
        if code_row is None:
            code_row = self.db.execute(
                "SELECT snapshots.patch_text AS selected_patch FROM analysis_runs "
                "JOIN snapshots USING(snapshot_id) WHERE analysis_id=?",
                (opportunity["analysis_id"],),
            ).fetchone()
        source_patch = str(code_row["selected_patch"]) if code_row else ""
        knowledge_card = self.db.execute(
            "SELECT * FROM knowledge_cards WHERE card_id=?", (task_id,),
        ).fetchone()
        source_commits = [dict(row) for row in self.db.execute(
            "SELECT commit_sha,binding_id,link_basis FROM card_source_commits "
            "WHERE card_id=? ORDER BY created_at,commit_sha", (task_id,),
        )]
        return {
            "task": dict(task),
            "knowledge_card": dict(knowledge_card) | {
                "front_question": task["question"], "topic_id": task["topic_id"],
                "progress": task["progress"], "source_commits": source_commits,
            } if knowledge_card else None,
            "opportunity": dict(opportunity),
            "card": json.loads(opportunity["card_json"]) if opportunity["card_json"] else None,
            "sources": [dict(row) for row in self.db.execute(
                "SELECT source_ref_id, relative_path FROM source_refs WHERE source_ref_id IN ("
                + ",".join("?" for _ in json.loads(task["source_refs_json"])) + ")",
                json.loads(task["source_refs_json"]),
            )],
            "source_patch": source_patch[:12000]
            + ("\n〔源码变化已截断〕" if len(source_patch) > 12000 else ""),
            "proposals": [dict(row) for row in proposals],
            "attempts": [dict(row) for row in attempts],
            "notes": [dict(row) for row in notes],
            "timeline": [dict(row) for row in timeline],
            "hints": [dict(row) for row in hints],
        }
