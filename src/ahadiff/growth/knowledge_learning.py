"""Evidence-bound teaching, objective answers and self-directed card review."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .local import GrowthLocalRepository


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Option(_Strict):
    id: str = Field(pattern=r"^[ABCD]$")
    text: str = Field(min_length=1, max_length=600)


class ObjectiveQuestion(_Strict):
    stem: str = Field(min_length=8, max_length=1000)
    scenario: str = Field(min_length=1, max_length=5000)
    options: list[Option] = Field(min_length=4, max_length=4)
    correct_option_id: str = Field(pattern=r"^[ABCD]$")
    explanations: dict[str, str]
    reasoning: str = Field(min_length=20, max_length=4000)
    boundary: str = Field(min_length=1, max_length=1500)
    source_refs: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def complete_answer_key(self) -> ObjectiveQuestion:
        if {option.id for option in self.options} != {"A", "B", "C", "D"}:
            raise ValueError("四选一必须恰有 A、B、C、D")
        if set(self.explanations) != {"A", "B", "C", "D"}:
            raise ValueError("必须逐项解析四个选项")
        if any(not text.strip() for text in self.explanations.values()):
            raise ValueError("选项解析不能为空")
        return self


class LearningMaterial(_Strict):
    front_question: str = Field(min_length=8, max_length=600)
    back_summary: str = Field(min_length=20, max_length=3000)
    back_mechanism: str = Field(min_length=20, max_length=5000)
    back_boundary: str = Field(min_length=10, max_length=3000)
    misconceptions: list[str] = Field(min_length=1, max_length=8)
    source_refs: list[str] = Field(min_length=1, max_length=8)
    understanding: ObjectiveQuestion
    prediction: ObjectiveQuestion


def _card(ledger: GrowthLocalRepository, card_id: str) -> Any:
    row = ledger.connection.execute(
        "SELECT cards.*,tasks.question,tasks.progress,tasks.topic_id,"
        "concepts.title AS title FROM knowledge_cards AS cards "
        "JOIN growth_tasks AS tasks ON tasks.task_id=cards.card_id "
        "LEFT JOIN knowledge_concepts AS concepts ON concepts.concept_id=cards.concept_id "
        "WHERE cards.card_id=?", (card_id,),
    ).fetchone()
    if row is None:
        raise ValueError("正式卡不存在")
    return row


def _sources(ledger: GrowthLocalRepository, card_id: str) -> list[dict[str, Any]]:
    refs = ledger.connection.execute(
        "SELECT DISTINCT source_refs.source_ref_id,source_refs.relative_path,"
        "source_refs.content,features.project_id FROM opportunities "
        "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
        "JOIN features USING(feature_id) JOIN source_refs USING(snapshot_id) "
        "WHERE opportunities.canonical_card_id=?", (card_id,),
    ).fetchall()
    commits = [dict(row) for row in ledger.connection.execute(
        "SELECT links.binding_id,links.commit_sha,history.title AS commit_title,"
        "history.authored_at,"
        "bindings.project_id FROM card_source_commits AS links "
        "JOIN bindings USING(binding_id) "
        "LEFT JOIN git_history_commits AS history ON history.binding_id=links.binding_id "
        "AND history.commit_sha=links.commit_sha WHERE links.card_id=? "
        "ORDER BY links.created_at,links.commit_sha", (card_id,),
    )]
    default_sha = commits[0]["commit_sha"] if len(commits) == 1 else None
    result = [{"source_ref_id": row["source_ref_id"],
               "relative_path": row["relative_path"],
               "project_id": row["project_id"],
               "commit_sha": default_sha,
               "excerpt": bytes(row["content"]).decode("utf-8", errors="replace")[:3000]}
              for row in refs]
    result.extend({"source_ref_id": None, "relative_path": None,
                   "project_id": item["project_id"], "binding_id": item["binding_id"],
                   "commit_sha": item["commit_sha"], "commit_title": item["commit_title"]}
                  for item in commits)
    return result


def _progress(ledger: GrowthLocalRepository, card_id: str, version: int) -> dict[str, Any]:
    row = ledger.connection.execute(
        "SELECT * FROM card_learning_progress WHERE card_id=?", (card_id,),
    ).fetchone()
    if row is None:
        return {"card_id": card_id, "card_version": version, "stage": "front",
                "option_drafts": {}, "note_text": "", "back_seen_at": None,
                "completed_at": None, "answers": {}}
    result = dict(row)
    result["option_drafts"] = json.loads(result.pop("option_drafts_json"))
    answers = {}
    for answer in ledger.connection.execute(
        "SELECT questions.kind,answers.* FROM card_objective_answers AS answers "
        "JOIN card_objective_questions AS questions USING(question_id) "
        "WHERE answers.card_id=? AND questions.card_version=?",
        (card_id, version),
    ):
        value = dict(answer)
        value["correct"] = bool(value["correct"])
        value["assisted"] = bool(value["assisted"])
        value["explanation_confirmed"] = bool(ledger.connection.execute(
            "SELECT 1 FROM card_explanation_reads WHERE card_id=? AND question_id=? "
            "AND question_version=?", (card_id, answer["question_id"],
                                     answer["question_version"]),
        ).fetchone())
        answers[answer["kind"]] = value
    result["answers"] = answers
    return result


def card_detail(ledger: GrowthLocalRepository, card_id: str) -> dict[str, Any]:
    row = _card(ledger, card_id)
    version = int(row["card_version"])
    material = ledger.connection.execute(
        "SELECT * FROM card_learning_materials WHERE card_id=? AND card_version=?",
        (card_id, version),
    ).fetchone()
    mark = ledger.connection.execute(
        "SELECT mark,reason FROM card_followup_marks WHERE card_id=?", (card_id,),
    ).fetchone()
    questions = []
    for question in ledger.connection.execute(
        "SELECT * FROM card_objective_questions WHERE card_id=? AND card_version=? "
        "ORDER BY CASE kind WHEN 'understanding' THEN 0 ELSE 1 END",
        (card_id, version),
    ):
        item = {key: question[key] for key in (
            "question_id", "card_version", "kind", "version", "stem", "scenario",
            "reasoning", "boundary",
        )}
        item["options"] = json.loads(question["options_json"])
        item["source_refs"] = json.loads(question["source_refs_json"])
        answered = ledger.connection.execute(
            "SELECT 1 FROM card_objective_answers WHERE card_id=? AND question_id=? "
            "AND question_version=?", (card_id, question["question_id"], question["version"]),
        ).fetchone()
        if answered:
            item["correct_option_id"] = question["correct_option_id"]
            item["explanations"] = json.loads(question["explanations_json"])
        else:
            item.pop("reasoning")
            item.pop("boundary")
        questions.append(item)
    conversations = []
    for item in ledger.connection.execute(
        "SELECT * FROM card_chat_exchanges WHERE card_id=? ORDER BY created_at,request_id",
        (card_id,),
    ):
        context = json.loads(item["context_json"])
        if context.get("incomplete"):
            continue
        conversations.append({
            "request_id": item["request_id"], "user_text": item["user_text"],
            "reply_text": item["reply_text"], "context": context,
            "created_at": item["created_at"],
        })
    card = {key: row[key] for key in (
        "card_id", "concept_id", "title", "learning_goal", "back_answer",
        "back_explanation", "card_version", "question", "progress", "topic_id",
    )}
    card["front_question"] = material["front_question"] if material else row["question"]
    card["followup_mark"] = mark["mark"] if mark else "none"
    card["followup_reason"] = mark["reason"] if mark else ""
    card["material"] = ({
        "front_question": material["front_question"],
        "back_summary": material["back_summary"],
        "back_mechanism": material["back_mechanism"],
        "back_boundary": material["back_boundary"],
        "misconceptions": json.loads(material["misconceptions_json"]),
        "source_refs": json.loads(material["source_refs_json"]),
    } if material else None)
    card["revision_pending"] = version > 1 and material is None
    revision_history = [dict(item) for item in ledger.connection.execute(
        "SELECT previous_version,new_version,reason,created_at "
        "FROM card_material_revisions WHERE card_id=? ORDER BY new_version",
        (card_id,),
    )]
    learning_history = [json.loads(item["progress_json"]) for item in
                        ledger.connection.execute(
        "SELECT progress_json FROM card_learning_progress_history "
        "WHERE card_id=? ORDER BY card_version", (card_id,),
    )]
    return {"card": card, "learning": _progress(ledger, card_id, version),
            "questions": questions, "sources": _sources(ledger, card_id),
            "wiki_draft": None, "conversation": conversations,
            "revision_history": revision_history,
            "learning_history": learning_history}


def _ensure_progress(ledger: GrowthLocalRepository, card_id: str, version: int) -> None:
    ledger.connection.execute(
        "INSERT OR IGNORE INTO card_learning_progress "
        "(card_id,card_version,stage,option_drafts_json,note_text,updated_at) "
        "VALUES (?,?,'front','{}','',?)", (card_id, version, _now()),
    )


def revise_material(
    ledger: GrowthLocalRepository, card_id: str, *, request_id: str,
    expected_version: int, reason: str,
) -> dict[str, Any]:
    """Start an explicit new lesson version without erasing earlier objective results."""
    reason = reason.strip()
    if not 8 <= len(reason) <= 1000:
        raise ValueError("请说明本次知识或题目修订的具体原因")
    previous = ledger.connection.execute(
        "SELECT card_id,previous_version,new_version,reason "
        "FROM card_material_revisions WHERE request_id=?", (request_id,),
    ).fetchone()
    if previous is not None:
        if (previous["card_id"] != card_id or
                int(previous["previous_version"]) != expected_version or
                previous["reason"] != reason):
            raise ValueError("同一修订请求不能更换内容")
        return card_detail(ledger, card_id)
    card = _card(ledger, card_id)
    if int(card["card_version"]) != expected_version:
        raise ValueError("卡片版本已变化，请重新加载")
    if not ledger.connection.execute(
        "SELECT 1 FROM card_learning_materials WHERE card_id=? AND card_version=?",
        (card_id, expected_version),
    ).fetchone():
        raise ValueError("当前版本尚无教学材料")
    old_progress = _progress(ledger, card_id, expected_version)
    new_version = expected_version + 1
    now = _now()
    with ledger.connection:
        ledger.connection.execute(
            "INSERT OR IGNORE INTO card_learning_progress_history VALUES (?,?,?,?)",
            (card_id, expected_version, _json(old_progress), now),
        )
        ledger.connection.execute(
            "INSERT INTO card_material_revisions VALUES (?,?,?,?,?,?)",
            (request_id, card_id, expected_version, new_version, reason, now),
        )
        ledger.connection.execute(
            "UPDATE knowledge_cards SET card_version=?,updated_at=? WHERE card_id=?",
            (new_version, now, card_id),
        )
        ledger.connection.execute(
            "INSERT INTO card_learning_progress "
            "(card_id,card_version,stage,option_drafts_json,note_text,updated_at) "
            "VALUES (?,?,'front','{}',?,?) "
            "ON CONFLICT(card_id) DO UPDATE SET card_version=excluded.card_version,"
            "stage='front',option_drafts_json='{}',note_text=excluded.note_text,"
            "back_seen_at=NULL,completed_at=NULL,updated_at=excluded.updated_at",
            (card_id, new_version, old_progress["note_text"], now),
        )
        ledger.connection.execute(
            "UPDATE growth_tasks SET progress='ready',updated_at=? WHERE task_id=?",
            (now, card_id),
        )
    return card_detail(ledger, card_id)


def save_material(
    ledger: GrowthLocalRepository, card_id: str, material: LearningMaterial,
    *, provider_name: str, payload_hash: str,
) -> dict[str, Any]:
    card = _card(ledger, card_id)
    version = int(card["card_version"])
    valid_refs = {item["source_ref_id"] for item in _sources(ledger, card_id)
                  if item.get("source_ref_id")}
    used_refs = set(material.source_refs)
    used_refs.update(material.understanding.source_refs)
    used_refs.update(material.prediction.source_refs)
    if not used_refs <= valid_refs:
        raise ValueError("教学材料引用了卡片之外的源码")
    previous = ledger.connection.execute(
        "SELECT payload_hash FROM card_learning_materials WHERE card_id=? AND card_version=?",
        (card_id, version),
    ).fetchone()
    if previous:
        if previous["payload_hash"] != payload_hash:
            raise ValueError("现有题包不能被静默覆盖，请先创建新版")
        return card_detail(ledger, card_id)
    with ledger.connection:
        ledger.connection.execute(
            "INSERT INTO card_learning_materials VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (card_id, version, material.front_question, material.back_summary,
             material.back_mechanism, material.back_boundary,
             _json(material.misconceptions), _json(material.source_refs),
             provider_name, payload_hash, _now()),
        )
        for kind, question in (("understanding", material.understanding),
                               ("prediction", material.prediction)):
            ledger.connection.execute(
                "INSERT INTO card_objective_questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), card_id, version, kind, 1, question.stem,
                 question.scenario, _json([option.model_dump() for option in question.options]),
                 question.correct_option_id, _json(question.explanations),
                 question.reasoning, question.boundary, _json(question.source_refs), _now()),
            )
        ledger.connection.execute(
            "UPDATE knowledge_cards SET back_answer=?,back_explanation=?,updated_at=? "
            "WHERE card_id=?",
            (material.back_summary, material.back_mechanism, _now(), card_id),
        )
        _ensure_progress(ledger, card_id, version)
    return card_detail(ledger, card_id)


def save_learning(
    ledger: GrowthLocalRepository, card_id: str, *, stage: str | None = None,
    option_drafts: dict[str, str] | None = None, note_text: str | None = None,
) -> dict[str, Any]:
    card = _card(ledger, card_id)
    version = int(card["card_version"])
    if stage == "completed":
        raise ValueError("完成学习必须经过完整判定与解析确认")
    if note_text is not None and len(note_text) > 20000:
        raise ValueError("个人笔记过长")
    if stage and stage not in {
        "front", "back", "understanding", "understanding_explanation",
        "prediction", "prediction_explanation",
    }:
        raise ValueError("学习阶段无效")
    if stage and stage != "front" and not ledger.connection.execute(
        "SELECT 1 FROM card_learning_materials WHERE card_id=? AND card_version=?",
        (card_id, version),
    ).fetchone():
        raise ValueError("知识讲解尚未生成")
    with ledger.connection:
        _ensure_progress(ledger, card_id, version)
        progress = _progress(ledger, card_id, version)
        answers = progress["answers"]
        if stage in {"understanding", "understanding_explanation", "prediction",
                     "prediction_explanation"} and not progress["back_seen_at"]:
            raise ValueError("请先翻面学习")
        if stage == "understanding_explanation" and "understanding" not in answers:
            raise ValueError("请先提交理解题")
        if stage in {"prediction", "prediction_explanation"} and not answers.get(
            "understanding", {}
        ).get("explanation_confirmed"):
            raise ValueError("请先阅读并确认理解题解析")
        if stage == "prediction_explanation" and "prediction" not in answers:
            raise ValueError("请先提交预测题")
        drafts = progress["option_drafts"]
        if option_drafts is not None:
            valid = {row["question_id"]: json.loads(row["options_json"])
                     for row in ledger.connection.execute(
                "SELECT question_id,options_json FROM card_objective_questions "
                "WHERE card_id=? AND card_version=?", (card_id, version),
            )}
            for question_id, option_id in option_drafts.items():
                if question_id not in valid or option_id not in {
                    option["id"] for option in valid[question_id]
                }:
                    raise ValueError("选项草稿与当前题包不匹配")
            drafts = option_drafts
        ledger.connection.execute(
            "UPDATE card_learning_progress SET stage=?,option_drafts_json=?,note_text=?,"
            "back_seen_at=CASE WHEN ?='back' AND back_seen_at IS NULL THEN ? "
            "ELSE back_seen_at END,updated_at=? WHERE card_id=?",
            (stage or progress["stage"], _json(drafts),
             note_text if note_text is not None else progress["note_text"],
             stage, _now(), _now(), card_id),
        )
        if stage and stage != "front":
            ledger.connection.execute(
                "UPDATE growth_tasks SET progress='in_progress',updated_at=? "
                "WHERE task_id=? AND progress='ready'", (_now(), card_id),
            )
    return card_detail(ledger, card_id)


def answer_question(
    ledger: GrowthLocalRepository, card_id: str, *, request_id: str,
    question_id: str, version: int, option_id: str,
) -> dict[str, Any]:
    card = _card(ledger, card_id)
    question = ledger.connection.execute(
        "SELECT * FROM card_objective_questions WHERE question_id=? AND card_id=? "
        "AND card_version=? AND version=?",
        (question_id, card_id, card["card_version"], version),
    ).fetchone()
    if question is None:
        raise ValueError("题目版本已变化，请重新加载")
    if option_id not in {item["id"] for item in json.loads(question["options_json"])}:
        raise ValueError("选项 ID 无效")
    progress = _progress(ledger, card_id, int(card["card_version"]))
    if not progress["back_seen_at"]:
        raise ValueError("请先翻面学习")
    if question["kind"] == "prediction" and not progress["answers"].get(
        "understanding", {}
    ).get("explanation_confirmed"):
        raise ValueError("请先完成理解题解析")
    with ledger.connection:
        _ensure_progress(ledger, card_id, int(card["card_version"]))
        previous = ledger.connection.execute(
            "SELECT * FROM card_objective_answers WHERE request_id=?", (request_id,),
        ).fetchone()
        if previous and (previous["card_id"] != card_id or
                         previous["question_id"] != question_id or
                         previous["option_id"] != option_id):
            raise ValueError("同一作答请求不能更换题目或选项")
        if previous is None:
            previous = ledger.connection.execute(
                "SELECT * FROM card_objective_answers WHERE card_id=? AND question_id=? "
                "AND question_version=?", (card_id, question_id, version),
            ).fetchone()
        if previous is None:
            assisted = any(
                json.loads(exchange["context_json"]).get("card_version")
                == int(card["card_version"])
                for exchange in ledger.connection.execute(
                    "SELECT context_json FROM card_chat_exchanges WHERE card_id=?",
                    (card_id,),
                )
            )
            ledger.connection.execute(
                "INSERT INTO card_objective_answers VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), card_id, question_id, version, option_id,
                 int(option_id == question["correct_option_id"]), int(assisted),
                 request_id, _now()),
            )
            ledger.connection.execute(
                "UPDATE card_learning_progress SET stage=?,updated_at=? WHERE card_id=?",
                (question["kind"] + "_explanation", _now(), card_id),
            )
        elif previous["option_id"] != option_id:
            raise ValueError("首次客观判定不可覆盖")
    result = card_detail(ledger, card_id)
    result["answer"] = {
        "question_id": question_id, "version": version, "option_id": option_id,
        "correct_option_id": question["correct_option_id"],
        "correct": option_id == question["correct_option_id"],
        "assisted": bool(result["learning"]["answers"][question["kind"]]["assisted"]),
        "explanation_confirmed": False,
    }
    return result


def confirm_explanation(
    ledger: GrowthLocalRepository, card_id: str, question_id: str, *, request_id: str,
) -> dict[str, Any]:
    card = _card(ledger, card_id)
    question = ledger.connection.execute(
        "SELECT kind,version FROM card_objective_questions WHERE question_id=? "
        "AND card_id=? AND card_version=?",
        (question_id, card_id, card["card_version"]),
    ).fetchone()
    if question is None:
        raise ValueError("题目不存在或版本已变")
    answer = ledger.connection.execute(
        "SELECT 1 FROM card_objective_answers WHERE card_id=? AND question_id=? "
        "AND question_version=?", (card_id, question_id, question["version"]),
    ).fetchone()
    if answer is None:
        raise ValueError("题目尚未提交")
    with ledger.connection:
        previous = ledger.connection.execute(
            "SELECT card_id,question_id FROM card_explanation_reads WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if previous and (previous["card_id"] != card_id or
                         previous["question_id"] != question_id):
            raise ValueError("同一解析确认请求不能更换题目")
        ledger.connection.execute(
            "INSERT OR IGNORE INTO card_explanation_reads VALUES (?,?,?,?,?)",
            (card_id, question_id, question["version"], request_id, _now()),
        )
        ledger.connection.execute(
            "UPDATE card_learning_progress SET stage=?,updated_at=? WHERE card_id=? "
            "AND completed_at IS NULL",
            ("prediction" if question["kind"] == "understanding"
             else "prediction_explanation", _now(), card_id),
        )
    return card_detail(ledger, card_id)


def complete_learning(ledger: GrowthLocalRepository, card_id: str) -> dict[str, Any]:
    card = _card(ledger, card_id)
    progress = _progress(ledger, card_id, int(card["card_version"]))
    if not progress["back_seen_at"] or not all(
        progress["answers"].get(kind, {}).get("explanation_confirmed")
        for kind in ("understanding", "prediction")
    ):
        raise ValueError("请先完成讲解、两题作答和两份解析")
    with ledger.connection:
        ledger.connection.execute(
            "UPDATE card_learning_progress SET stage='completed',"
            "completed_at=COALESCE(completed_at,?),updated_at=? WHERE card_id=?",
            (_now(), _now(), card_id),
        )
        ledger.connection.execute(
            "UPDATE growth_tasks SET progress='completed',updated_at=? WHERE task_id=?",
            (_now(), card_id),
        )
    return card_detail(ledger, card_id)


def set_followup_mark(
    ledger: GrowthLocalRepository, card_id: str, mark: str, reason: str = "",
) -> dict[str, Any]:
    _card(ledger, card_id)
    if mark not in {"none", "confused", "revisit"} or len(reason) > 500:
        raise ValueError("回看标记无效")
    with ledger.connection:
        if mark == "none":
            ledger.connection.execute(
                "DELETE FROM card_followup_marks WHERE card_id=?", (card_id,),
            )
        else:
            ledger.connection.execute(
                "INSERT INTO card_followup_marks VALUES (?,?,?,?) "
                "ON CONFLICT(card_id) DO UPDATE SET mark=excluded.mark,"
                "reason=excluded.reason,updated_at=excluded.updated_at",
                (card_id, mark, reason, _now()),
            )
    return card_detail(ledger, card_id)
