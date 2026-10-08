"""Local knowledge-first growth endpoints."""

from __future__ import annotations

import asyncio
import json
import queue
import sqlite3
import threading
import uuid
from typing import Any, Literal

from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse, StreamingResponse

from ahadiff.contracts import ErrorCode
from ahadiff.core.errors import AhaDiffError
from ahadiff.growth import knowledge_first
from ahadiff.growth import knowledge_learning
from ahadiff.growth.knowledge_card_chat import chat as card_chat
from ahadiff.growth import knowledge_wiki
from ahadiff.growth.knowledge_material_model import (
    generate_material, prepare_material_preview,
)
from ahadiff.growth.knowledge_sync import resolve_knowledge_conflict
from ahadiff.growth.local import GrowthLocalRepository

from ._errors import error_response
from .auth import require_write_token, serve_state


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _AggregateInput(_Input):
    opportunity_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)


class _DecisionInput(_Input):
    request_id: uuid.UUID
    action: Literal["confirm_new", "merge", "defer", "ignore"]
    card_id: uuid.UUID | None = None
    topic_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    additional_candidate_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class _SplitInput(_Input):
    request_id: uuid.UUID
    opportunity_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=8, max_length=1000)


class _DiscussionInput(_Input):
    session_id: uuid.UUID


class _MaterialPreviewInput(_Input):
    binding_id: uuid.UUID
    provider_name: str = Field(min_length=1, max_length=120)


class _MaterialGenerateInput(_MaterialPreviewInput):
    approved_payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: Literal[True]


class _RevisionInput(_Input):
    request_id: uuid.UUID
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=8, max_length=1000)


class _LearningInput(_Input):
    stage: str | None = None
    option_drafts: dict[str, str] | None = None
    note_text: str | None = None


class _AnswerInput(_Input):
    request_id: uuid.UUID
    question_id: uuid.UUID
    version: int = Field(ge=1)
    option_id: Literal["A", "B", "C", "D"]


class _ReadInput(_Input):
    request_id: uuid.UUID


class _MarkInput(_Input):
    mark: Literal["none", "confused", "revisit"]
    reason: str = Field(default="", max_length=500)


class _NoteInput(_Input):
    text: str = Field(max_length=20000)


class _ChatInput(_Input):
    request_id: uuid.UUID
    provider_name: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)
    stage: str
    question_id: uuid.UUID | None = None
    evidence_refs: list[uuid.UUID] | None = Field(default=None, max_length=8)


class _VaultInput(_Input):
    path: str = Field(min_length=1, max_length=4096)
    target_folder: str | None = Field(default=None, min_length=1, max_length=256)


class _DraftInput(_Input):
    request_id: uuid.UUID


class _DraftEditInput(_Input):
    markdown: str = Field(min_length=1, max_length=200000)


class _SyncConflictResolveInput(_Input):
    account_id: uuid.UUID
    decision: Literal["accept_remote", "keep_local"]


def _ledger(request: Any) -> GrowthLocalRepository:
    return GrowthLocalRepository(serve_state(request).state_dir / "growth.sqlite")


async def _parse(request: Any, model: type[_Input]) -> _Input | JSONResponse:
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=422)


async def growth_knowledge_workspace(request: Any) -> JSONResponse:
    require_write_token(request)

    def read() -> dict[str, Any]:
        with _ledger(request) as ledger:
            return knowledge_first.workspace(ledger)

    return JSONResponse(await to_thread.run_sync(read))


async def growth_knowledge_sync_conflicts(request: Any) -> JSONResponse:
    require_write_token(request)

    def read() -> dict[str, Any]:
        with _ledger(request) as ledger:
            rows = ledger.connection.execute(
                "SELECT state.account_id,state.card_id,state.revision,"
                "cards.card_version,concepts.title "
                "FROM knowledge_sync_state AS state "
                "JOIN knowledge_cards AS cards USING(card_id) "
                "LEFT JOIN knowledge_concepts AS concepts USING(concept_id) "
                "WHERE state.conflict_json IS NOT NULL "
                "ORDER BY state.updated_at DESC"
            ).fetchall()
            return {"conflicts": [dict(row) for row in rows]}

    return JSONResponse(await to_thread.run_sync(read))


async def growth_knowledge_sync_conflict_resolve(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SyncConflictResolveInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _SyncConflictResolveInput)
    try:
        def resolve() -> dict[str, Any]:
            with _ledger(request) as ledger:
                account = ledger.connection.execute(
                    "SELECT cloud_origin FROM sync_accounts WHERE account_id=?",
                    (str(body.account_id),),
                ).fetchone()
                if account is None or not account["cloud_origin"]:
                    raise ValueError("同步账号未配置")
                return resolve_knowledge_conflict(
                    ledger, str(body.account_id),
                    str(request.path_params["card_id"]), body.decision,
                    str(account["cloud_origin"]),
                )

        return JSONResponse(await to_thread.run_sync(resolve))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_commit(request: Any) -> JSONResponse:
    require_write_token(request)
    try:
        binding_id = str(uuid.UUID(request.query_params["binding_id"]))
        commit_sha = str(request.path_params["commit_sha"])

        def read() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_first.commit_detail(ledger, binding_id, commit_sha)

        return JSONResponse(await to_thread.run_sync(read))
    except (KeyError, ValueError) as exc:
        return _problem(exc)


async def growth_knowledge_aggregate(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _AggregateInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _AggregateInput)
    ids = [str(item) for item in body.opportunity_ids] if body.opportunity_ids else None

    def read() -> dict[str, Any]:
        with _ledger(request) as ledger:
            return knowledge_first.workspace(ledger) | {
                "candidates": knowledge_first.candidates(ledger, ids),
            }

    return JSONResponse(await to_thread.run_sync(read))


async def growth_knowledge_decision(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _DecisionInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _DecisionInput)
    try:
        def decide() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_first.decide_candidate(
                    ledger, str(request.path_params["candidate_id"]),
                    request_id=str(body.request_id), action=body.action,
                    card_id=str(body.card_id) if body.card_id else None,
                    topic_id=str(body.topic_id) if body.topic_id else None,
                    title=body.title,
                    additional_candidate_ids=[str(item) for item in body.additional_candidate_ids],
                )

        return JSONResponse(await to_thread.run_sync(decide))
    except (ValueError, sqlite3.IntegrityError):
        return error_response(ErrorCode.INPUT_VALIDATION,
                              "growth_knowledge_decision_failed", status=409)


async def growth_knowledge_split(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SplitInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _SplitInput)
    try:
        def split() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_first.split_candidate(
                    ledger, str(request.path_params["candidate_id"]),
                    request_id=str(body.request_id),
                    opportunity_ids=[str(item) for item in body.opportunity_ids],
                    title=body.title, reason=body.reason,
                )

        return JSONResponse(await to_thread.run_sync(split))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_discussion(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _DiscussionInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _DiscussionInput)
    try:
        def link() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_first.link_discussion(
                    ledger, str(request.path_params["candidate_id"]),
                    str(body.session_id),
                )

        return JSONResponse(await to_thread.run_sync(link))
    except Exception as exc:
        return _problem(exc)


def _card_id(request: Any) -> str:
    return str(request.path_params["card_id"])


def _problem(exc: Exception) -> JSONResponse:
    if isinstance(exc, (ValueError, sqlite3.IntegrityError, ValidationError)):
        return JSONResponse({"error": str(exc)}, status_code=409)
    raise exc


async def growth_knowledge_card(request: Any) -> JSONResponse:
    require_write_token(request)
    try:
        def read() -> dict[str, Any]:
            with _ledger(request) as ledger:
                detail = knowledge_learning.card_detail(ledger, _card_id(request))
                detail["wiki_draft"] = knowledge_wiki.latest_draft(ledger, _card_id(request))
                return detail

        return JSONResponse(await to_thread.run_sync(read))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_material_preview(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _MaterialPreviewInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _MaterialPreviewInput)
    try:
        def preview() -> dict[str, Any]:
            with _ledger(request) as ledger:
                plan = prepare_material_preview(
                    ledger, _card_id(request), str(body.binding_id), body.provider_name,
                )
                return {"approval_hash": plan.approval_hash,
                        "payload_text": plan.payload_text,
                        "provider_host": plan.provider_host,
                        "model_name": plan.model_name}

        return JSONResponse(await to_thread.run_sync(preview))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_material_generate(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _MaterialGenerateInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _MaterialGenerateInput)
    try:
        def generate() -> dict[str, Any]:
            with _ledger(request) as ledger:
                plan = prepare_material_preview(
                    ledger, _card_id(request), str(body.binding_id), body.provider_name,
                )
                if plan.approval_hash != body.approved_payload_hash:
                    raise ValueError("审批内容已变化，请重新预览")
                material, _ = generate_material(plan)
                return knowledge_learning.save_material(
                    ledger, _card_id(request), material,
                    provider_name=body.provider_name, payload_hash=plan.approval_hash,
                )

        return JSONResponse(await to_thread.run_sync(generate))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_revise(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _RevisionInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _RevisionInput)
    try:
        def revise() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_learning.revise_material(
                    ledger, _card_id(request), request_id=str(body.request_id),
                    expected_version=body.expected_version, reason=body.reason,
                )

        return JSONResponse(await to_thread.run_sync(revise))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_learning(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _LearningInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _LearningInput)
    try:
        def save() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_learning.save_learning(
                    ledger, _card_id(request), stage=body.stage,
                    option_drafts=body.option_drafts, note_text=body.note_text,
                )

        return JSONResponse(await to_thread.run_sync(save))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_flip(request: Any) -> JSONResponse:
    require_write_token(request)
    try:
        def flip() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_learning.save_learning(ledger, _card_id(request), stage="back")

        return JSONResponse(await to_thread.run_sync(flip))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_answer(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _AnswerInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _AnswerInput)
    try:
        def answer() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_learning.answer_question(
                    ledger, _card_id(request), request_id=str(body.request_id),
                    question_id=str(body.question_id), version=body.version,
                    option_id=body.option_id,
                )

        return JSONResponse(await to_thread.run_sync(answer))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_explanation_confirm(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ReadInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _ReadInput)
    try:
        def confirm() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_learning.confirm_explanation(
                    ledger, _card_id(request), str(request.path_params["question_id"]),
                    request_id=str(body.request_id),
                )

        return JSONResponse(await to_thread.run_sync(confirm))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_complete(request: Any) -> JSONResponse:
    require_write_token(request)
    try:
        def complete() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_learning.complete_learning(ledger, _card_id(request))

        return JSONResponse(await to_thread.run_sync(complete))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_mark(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _MarkInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _MarkInput)
    try:
        def mark() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_learning.set_followup_mark(
                    ledger, _card_id(request), body.mark, body.reason,
                )

        return JSONResponse(await to_thread.run_sync(mark))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_note(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _NoteInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _NoteInput)
    try:
        def save() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_learning.save_learning(
                    ledger, _card_id(request), note_text=body.text,
                )

        return JSONResponse(await to_thread.run_sync(save))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_chat(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ChatInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _ChatInput)
    try:
        def reply() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return card_chat(
                    ledger, _card_id(request), request_id=str(body.request_id),
                    provider_name=body.provider_name, message=body.message,
                    stage=body.stage,
                    question_id=str(body.question_id) if body.question_id else None,
                    evidence_refs=[str(item) for item in body.evidence_refs]
                    if body.evidence_refs else None,
                )

        return JSONResponse(await to_thread.run_sync(reply))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_chat_stream(request: Any) -> StreamingResponse | JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ChatInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _ChatInput)
    card_id = _card_id(request)
    events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=64)
    stopped = threading.Event()

    class StreamStopped(Exception):
        pass

    def put(kind: str, value: Any) -> None:
        while not stopped.is_set():
            try:
                events.put((kind, value), timeout=0.2)
                return
            except queue.Full:
                continue
        raise StreamStopped()

    def run() -> None:
        try:
            with _ledger(request) as ledger:
                result = card_chat(
                    ledger, card_id, request_id=str(body.request_id),
                    provider_name=body.provider_name, message=body.message,
                    stage=body.stage,
                    question_id=str(body.question_id) if body.question_id else None,
                    evidence_refs=[str(item) for item in body.evidence_refs]
                    if body.evidence_refs else None,
                    on_text_delta=lambda delta: put("delta", delta),
                )
            put("done", result)
        except StreamStopped:
            return
        except Exception as exc:
            try:
                message = (str(exc) if isinstance(exc, (AhaDiffError, ValueError))
                           else "卡内 AI 暂时不可用，请稍后重试")
                put("error", message)
            except StreamStopped:
                return

    async def stream():
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            while True:
                try:
                    kind, value = events.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.05)
                    continue
                yield f"event: {kind}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"
                if kind in {"done", "error"}:
                    break
        finally:
            stopped.set()

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def growth_knowledge_vault(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _VaultInput) if request.method == "PUT" else None
    if isinstance(body, JSONResponse):
        return body
    try:
        def read_or_set() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return (knowledge_wiki.set_vault(ledger, body.path, body.target_folder)
                        if isinstance(body, _VaultInput) else knowledge_wiki.vault(ledger))

        return JSONResponse(await to_thread.run_sync(read_or_set))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_wiki_articles(request: Any) -> JSONResponse:
    require_write_token(request)
    try:
        def read() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return {"articles": knowledge_wiki.articles(ledger)}

        return JSONResponse(await to_thread.run_sync(read))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_wiki_draft(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _DraftInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _DraftInput)
    try:
        def create() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_wiki.create_draft(
                    ledger, _card_id(request), request_id=str(body.request_id),
                )

        return JSONResponse(await to_thread.run_sync(create))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_wiki_edit(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _DraftEditInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _DraftEditInput)
    try:
        def edit() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_wiki.update_draft(
                    ledger, str(request.path_params["draft_id"]), body.markdown,
                )

        return JSONResponse(await to_thread.run_sync(edit))
    except Exception as exc:
        return _problem(exc)


async def growth_knowledge_wiki_publish(request: Any) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _DraftInput)
    if isinstance(body, JSONResponse):
        return body
    assert isinstance(body, _DraftInput)
    try:
        def publish() -> dict[str, Any]:
            with _ledger(request) as ledger:
                return knowledge_wiki.publish_draft(
                    ledger, str(request.path_params["draft_id"]),
                    request_id=str(body.request_id),
                )

        return JSONResponse(await to_thread.run_sync(publish))
    except Exception as exc:
        return _problem(exc)
