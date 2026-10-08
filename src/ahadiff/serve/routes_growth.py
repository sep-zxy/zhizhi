"""Explicit local project binding and Git capture for the growth desktop UI."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast
from urllib.parse import urlsplit

import httpx
from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.responses import JSONResponse, Response

from ahadiff.contracts import ErrorCode
from ahadiff.core.orchestrator import LearnRequest, run_learn_pipeline
from ahadiff.growth.code_intel import build_index
from ahadiff.growth.chat_model import generate_chat_reply, prepare_chat_preview
from ahadiff.growth.card_export import export_knowledge_cards_apkg
from ahadiff.growth.development_events import (
    capture_development_event,
    event_analysis_request,
    event_inbox,
    link_event_analysis,
)
from ahadiff.growth.git_snapshot import capture_worktree, git, is_internal_state_path, repository_root
from ahadiff.growth.history import group_authors, list_history, scan_history
from ahadiff.growth.learning import GrowthLearningService
from ahadiff.growth.local import GrowthLocalRepository
from ahadiff.growth.recommendations import apply_decisions
from ahadiff.growth.model_feedback import GrowthFeedbackService, prepare_feedback_preview
from ahadiff.growth.model_opportunities import (
    configured_provider_names,
    prepare_model_preview,
)
from ahadiff.quiz.generator import load_quiz_questions
from ahadiff.growth.publish import publication_plan
from ahadiff.growth.sync import GrowthSyncStore, SyncOriginMismatchError, connect_and_sync
from ahadiff.safety.redact import apply_redactions, scan_text_for_secrets

from ._errors import error_response
from .auth import require_write_token, serve_state

if TYPE_CHECKING:
    from starlette.requests import Request

    from .state import ServeState


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ProjectInput(_Input):
    name: str = Field(min_length=1, max_length=120)


class _ImportInput(_Input):
    name: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=4096)
    model_allowed: bool = False


class _ProjectPolicyInput(_Input):
    local_processing: bool
    model_allowed: bool
    cloud_allowed: bool


class _BindingInput(_Input):
    path: str = Field(min_length=1, max_length=4096)


class _AuthorGroupInput(_Input):
    author_emails: list[str] = Field(min_length=1, max_length=20)
    group_name: str = Field(min_length=1, max_length=120)


class _CardCommitsInput(_Input):
    binding_id: uuid.UUID
    commit_shas: list[str] = Field(min_length=1, max_length=20)


class _RecommendationConfirmGroup(_Input):
    opportunity_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    proposal_id: uuid.UUID
    existing_topic_id: uuid.UUID | None = None
    existing_card_id: uuid.UUID | None = None


class _RecommendationDecisionInput(_Input):
    request_id: uuid.UUID
    confirm_groups: list[_RecommendationConfirmGroup] = Field(default_factory=list, max_length=20)
    defer_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    ignore_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class _FeatureInput(_Input):
    binding_id: uuid.UUID
    label: str = Field(min_length=1, max_length=160)
    base_ref: str = Field(min_length=1, max_length=200)


class _CaptureInput(_Input):
    binding_id: uuid.UUID
    selected_untracked: list[str] = Field(default_factory=list, max_length=80)
    commit_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")


class _ExploreInput(_Input):
    session_id: uuid.UUID
    binding_id: uuid.UUID
    provider_name: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)


class _ExploreGenerateInput(_ExploreInput):
    approved_payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool


class _MaterialsInput(_Input):
    binding_id: uuid.UUID
    provider_name: str = Field(min_length=1, max_length=120)


class _MaterialsGenerateInput(_MaterialsInput):
    approved_payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool


class _MaterialAnswerInput(_Input):
    answer_text: str = Field(min_length=1, max_length=10000)


class _MaterialRatingInput(_Input):
    answer: Literal["wrong", "hard", "good", "easy"]
    idempotency_key: uuid.UUID


class _CodeIndexInput(_Input):
    binding_id: uuid.UUID
    symbols: list[str] = Field(min_length=1, max_length=8)


class _AnalysisInput(_Input):
    binding_id: uuid.UUID
    request_id: uuid.UUID
    mode: Literal["dry_run", "live"]
    provider_name: str | None = Field(default=None, max_length=120)
    source_ref_ids: list[uuid.UUID] | None = Field(default=None, max_length=8)
    approved_payload_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    approved: bool = False
    reanalysis: bool = False
    account_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None


class _ModelPreviewInput(_Input):
    binding_id: uuid.UUID
    provider_name: str = Field(min_length=1, max_length=120)
    source_ref_ids: list[uuid.UUID] = Field(min_length=1, max_length=8)


class _AnalysisRetryInput(_Input):
    binding_id: uuid.UUID


class _TopicDecisionInput(_Input):
    decision: Literal["confirm", "reject"]
    existing_topic_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


class _TaskCreateInput(_Input):
    topic_id: uuid.UUID
    module_id: uuid.UUID | None = None
    module_index_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class _AnswerInput(_Input):
    answer_text: str = Field(min_length=1, max_length=10000)
    parent_attempt_id: uuid.UUID | None = None
    hint_level: int = Field(default=0, ge=0, le=3)


class _HintInput(_Input):
    request_id: uuid.UUID


class _NoteInput(_Input):
    content_text: str = Field(min_length=1, max_length=20000)


class _TaskProgressInput(_Input):
    target: Literal["in_progress", "paused", "completed", "dismissed"]


class _FeedbackPreviewInput(_Input):
    binding_id: uuid.UUID


class _FeedbackSubmitInput(_Input):
    binding_id: uuid.UUID
    request_id: uuid.UUID
    approved_payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool
    retry: bool = False


class _SyncInput(_Input):
    cloud_url: str = Field(min_length=8, max_length=2048)
    access_token: SecretStr
    publish_project_id: uuid.UUID | None = None
    approved_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    refresh_history: bool = False


class _CloudExportInput(_Input):
    cloud_url: str = Field(min_length=8, max_length=2048)
    access_token: SecretStr
    account_id: uuid.UUID
    export_id: uuid.UUID


class _SyncPreviewInput(_Input):
    project_id: uuid.UUID


class _SourceExcerptPreviewInput(_Input):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class _SourceExcerptQueueInput(_SourceExcerptPreviewInput):
    account_id: uuid.UUID
    approval_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: Literal[True]


class _SyncNoteRevisionInput(_Input):
    account_id: uuid.UUID
    base_revision: int = Field(ge=1)
    content_text: str = Field(min_length=1, max_length=100000)


class _SyncNoteResolveInput(_SyncNoteRevisionInput):
    conflict_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class _SyncNoteDeleteInput(_Input):
    account_id: uuid.UUID
    base_revision: int = Field(ge=1)


class _SyncTaskDraftInput(_Input):
    account_id: uuid.UUID
    content_text: str = Field(max_length=20000)
    after_attempt_id: uuid.UUID | None = None


class _SyncTaskAnswerInput(_Input):
    account_id: uuid.UUID
    answer_text: str = Field(min_length=1, max_length=20000)
    parent_attempt_id: uuid.UUID | None = None
    hint_level: int = Field(default=0, ge=0, le=3)


class _SyncTaskProgressInput(_Input):
    account_id: uuid.UUID
    base_revision: int = Field(ge=1)
    progress: Literal["in_progress", "paused", "completed", "dismissed"]


class _SyncEventCaptureInput(_Input):
    account_id: uuid.UUID
    binding_id: uuid.UUID
    selected_untracked: list[str] = Field(default_factory=list, max_length=80)


def _cloud_origin(raw: str) -> str:
    parsed = urlsplit(raw.strip())
    if (
        not parsed.hostname or parsed.username or parsed.password
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or parsed.scheme not in {"https", "http"}
        or parsed.scheme == "http" and parsed.hostname not in {
            "localhost", "127.0.0.1", "::1"
        }
    ):
        raise ValueError("云服务地址必须是 HTTPS 地址或本机 loopback HTTP origin")
    port = parsed.port
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    suffix = "" if port in {None, 443 if parsed.scheme == "https" else 80} else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


TInput = TypeVar("TInput", bound=_Input)


def _ledger(state: ServeState) -> GrowthLocalRepository:
    return GrowthLocalRepository(state.state_dir / "growth.sqlite")


def _list(state: ServeState) -> dict[str, Any]:
    with _ledger(state) as ledger:
        db = ledger.connection
        snapshot_bindings: dict[str, list[str]] = {}
        for row in db.execute(
            "SELECT snapshot_id, binding_id FROM snapshot_bindings "
            "ORDER BY captured_at DESC"
        ):
            snapshot_bindings.setdefault(str(row["snapshot_id"]), []).append(
                str(row["binding_id"])
            )
        return {
            "local_topics": [dict(row) for row in db.execute(
                "SELECT topic_id, title, status, created_at FROM growth_topics "
                "ORDER BY created_at DESC"
            )],
            "local_topic_projects": [dict(row) for row in db.execute(
                "SELECT DISTINCT growth_tasks.topic_id, features.project_id "
                "FROM growth_tasks JOIN opportunities USING(opportunity_id) "
                "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
                "JOIN features USING(feature_id) UNION SELECT topic_id, project_id "
                "FROM local_explorations WHERE topic_id IS NOT NULL"
            )],
            "local_explorations": [dict(row) for row in db.execute(
                "SELECT session_id, project_id, messages_json, suggestion_json, topic_id, title "
                "FROM local_explorations ORDER BY updated_at DESC LIMIT 10"
            )],
            "projects": [dict(row) | {
                name: bool(row[name]) for name in (
                    "local_processing", "model_allowed", "cloud_allowed"
                )
            } for row in db.execute(
                "SELECT projects.project_id, projects.name, projects.created_at, "
                "project_policies.local_processing, project_policies.model_allowed, "
                "project_policies.cloud_allowed, project_policies.last_publish_error "
                "FROM projects JOIN project_policies USING(project_id) "
                "ORDER BY projects.created_at DESC"
            )],
            "bindings": [dict(row) | {"provider_names": configured_provider_names(
                Path(row["canonical_local_path"]))} for row in db.execute(
                "SELECT binding_id, project_id, canonical_local_path, created_at "
                "FROM bindings ORDER BY created_at DESC"
            )],
            "features": [dict(row) for row in db.execute(
                "SELECT feature_id, project_id, label, base_ref, status, created_at "
                "FROM features ORDER BY created_at DESC"
            )],
            "snapshots": [
                dict(row) | {"binding_ids": snapshot_bindings.get(str(row["snapshot_id"]), [])}
                for row in db.execute(
                    "SELECT snapshot_id, feature_id, head_sha, diff_hash, created_at "
                    "FROM snapshots ORDER BY created_at DESC"
                )
            ],
            "analysis_runs": [
                {
                    **{key: row[key] for key in (
                        "analysis_id", "snapshot_id", "mode", "status",
                        "upstream_run_id", "created_at", "supersedes_analysis_id",
                    )},
                    "needs_recapture": "本机源码版本已变化" in (row["error"] or ""),
                }
                for row in db.execute(
                    "SELECT analysis_id, snapshot_id, mode, status, upstream_run_id, "
                    "supersedes_analysis_id, "
                    "error, created_at FROM analysis_runs ORDER BY created_at DESC"
                )
            ],
            "opportunities": [dict(row) | {
                "source_commit_shas": [
                    json.loads(row["capture_scope"]).get("sha")
                ] if json.loads(row["capture_scope"]).get("kind") == "commit" else [],
            } for row in db.execute(
                "SELECT opportunities.opportunity_id, opportunities.analysis_id, "
                "opportunities.ordinal, opportunities.title, opportunities.reason, "
                "opportunities.learning_goal, opportunities.source_refs_json, "
                "opportunities.estimated_minutes, opportunities.uncertainties_json, "
                "opportunities.recommendation_status, "
                "opportunities.suggested_card_count, opportunities.recommendation_key, "
                "opportunities.canonical_card_id, features.project_id, "
                "snapshots.capture_scope FROM opportunities "
                "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
                "JOIN features USING(feature_id) "
                "ORDER BY opportunities.created_at DESC, opportunities.ordinal"
            )],
            "topic_proposals": [dict(row) for row in db.execute(
                "SELECT proposal_id, opportunity_id, title, status, topic_id "
                "FROM topic_proposals ORDER BY created_at DESC, ordinal"
            )],
            "growth_tasks": [dict(row) | {
                "card_id": row["task_id"],
                "front_question": row["question"],
                "source_commit_shas": [item[0] for item in db.execute(
                    "SELECT commit_sha FROM card_source_commits WHERE card_id=? "
                    "ORDER BY created_at,commit_sha", (row["task_id"],)
                )],
            } for row in db.execute(
                "SELECT tasks.task_id, tasks.opportunity_id, tasks.topic_id, "
                "tasks.question, tasks.progress, tasks.module_id, "
                "tasks.module_index_revision, features.project_id, "
                "cards.learning_goal, cards.back_answer, cards.back_explanation, "
                "cards.card_version, cards.source_link_status "
                "FROM growth_tasks AS tasks JOIN knowledge_cards AS cards "
                "ON cards.card_id=tasks.task_id "
                "JOIN opportunities ON opportunities.opportunity_id=tasks.opportunity_id "
                "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
                "JOIN features USING(feature_id) ORDER BY tasks.created_at DESC"
            )],
        }


def _create_project(state: ServeState, body: _ProjectInput) -> dict[str, str]:
    if not body.name.strip():
        raise ValueError("项目名不能为空")
    with _ledger(state) as ledger:
        return {"project_id": ledger.create_project(body.name.strip(), trace_id=str(uuid.uuid4()))}


def _import_project(state: ServeState, body: _ImportInput) -> dict[str, str]:
    root = repository_root(Path(body.path))
    head = git(root, "rev-parse", "HEAD").decode().strip()
    branch = git(root, "branch", "--show-current").decode("utf-8", errors="replace").strip()
    base = head
    if branch not in {"main", "master"}:
        for candidate in ("refs/heads/main", "refs/heads/master", "refs/remotes/origin/HEAD"):
            try:
                base = git(root, "merge-base", candidate, "HEAD").decode().strip()
                break
            except subprocess.CalledProcessError:
                continue
        else:
            base = git(root, "rev-list", "--max-parents=0", "HEAD").decode().splitlines()[0]
    with _ledger(state) as ledger:
        existing = ledger.connection.execute(
            "SELECT bindings.project_id, bindings.binding_id, projects.name FROM bindings "
            "JOIN projects USING(project_id) WHERE canonical_local_path=? LIMIT 1",
            (str(root),),
        ).fetchone()
        if existing:
            project_id = str(existing["project_id"])
            binding_id = str(existing["binding_id"])
            feature = ledger.connection.execute(
                "SELECT feature_id FROM features WHERE project_id=? "
                "ORDER BY created_at DESC LIMIT 1", (project_id,),
            ).fetchone()
            if feature:
                scan_history(ledger, binding_id)
                return {"project_id": project_id, "binding_id": binding_id,
                        "feature_id": str(feature["feature_id"]), "reused": "true"}
        else:
            project_id = ledger.create_project(body.name.strip(), trace_id=str(uuid.uuid4()))
            binding_id = ledger.bind_repo(project_id, root, trace_id=str(uuid.uuid4()))
        if body.model_allowed:
            policy = ledger.project_policy(project_id)
            ledger.set_project_policy(project_id, local_processing=True,
                                      model_allowed=True,
                                      cloud_allowed=policy["cloud_allowed"])
        feature_id = ledger.start_feature(
            project_id, binding_id, branch or "当前开发", base,
            trace_id=str(uuid.uuid4()),
        )
        scan_history(ledger, binding_id)
        return {"project_id": project_id, "binding_id": binding_id,
                "feature_id": feature_id, "reused": "false"}


def _bind(state: ServeState, project_id: str, body: _BindingInput) -> dict[str, str]:
    with _ledger(state) as ledger:
        if ledger.connection.execute(
            "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
        ).fetchone() is None:
            raise ValueError("项目不存在")
        return {"binding_id": ledger.bind_repo(
            project_id, Path(body.path), trace_id=str(uuid.uuid4())
        )}


def _start_feature(
    state: ServeState, project_id: str, body: _FeatureInput
) -> dict[str, str]:
    with _ledger(state) as ledger:
        return {"feature_id": ledger.start_feature(
            project_id, str(body.binding_id), body.label.strip(), body.base_ref,
            trace_id=str(uuid.uuid4()),
        )}


def _capture(state: ServeState, feature_id: str, body: _CaptureInput) -> dict[str, Any]:
    with _ledger(state) as ledger:
        snapshot_id = ledger.capture_snapshot(
            feature_id, str(body.binding_id), trace_id=str(uuid.uuid4()),
            selected_untracked=set(body.selected_untracked),
            commit_sha=body.commit_sha,
        )
        row = ledger.connection.execute(
            "SELECT head_sha, resolved_base_sha, diff_hash, created_at "
            "FROM snapshots WHERE snapshot_id=?", (snapshot_id,),
        ).fetchone()
        paths = [str(ref[0]) for ref in ledger.connection.execute(
            "SELECT relative_path FROM source_refs WHERE snapshot_id=? "
            "ORDER BY relative_path", (snapshot_id,),
        )]
        return {"snapshot_id": snapshot_id, **dict(row), "changed_paths": paths}


def _commits(
    state: ServeState, binding_id: str, feature_id: str, cursor: int = 0,
    limit: int = 50, author_emails: list[str] | None = None,
    author_groups: list[str] | None = None, refresh: bool = False,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        binding = ledger.connection.execute(
            "SELECT project_id FROM bindings WHERE binding_id=?", (binding_id,),
        ).fetchone()
        if binding is None:
            raise ValueError("仓库绑定不存在")
        if feature_id:
            feature = ledger.connection.execute(
                "SELECT start_base_sha FROM features WHERE feature_id=? AND project_id=?",
                (feature_id, binding["project_id"]),
            ).fetchone()
            if feature is None:
                raise ValueError("功能与仓库绑定不匹配")
        if refresh:
            scan_history(ledger, binding_id)
        result = list_history(
            ledger, binding_id, cursor=cursor, limit=limit,
            author_emails=author_emails, author_groups=author_groups,
        )
        result["base_sha"] = feature["start_base_sha"] if feature_id else None
        return result


def _local_explore_preview(
    state: ServeState, project_id: str, body: _ExploreInput,
) -> tuple[Any, Path, list[dict[str, Any]], list[dict[str, Any]]]:
    with _ledger(state) as ledger:
        ledger.require_project_permission(project_id, "model_allowed")
        binding = ledger.connection.execute(
            "SELECT canonical_local_path FROM bindings WHERE binding_id=? AND project_id=?",
            (str(body.binding_id), project_id),
        ).fetchone()
        if binding is None:
            raise ValueError("项目与仓库绑定不匹配")
        root = Path(binding["canonical_local_path"])
        session = ledger.connection.execute(
            "SELECT messages_json FROM local_explorations WHERE session_id=? AND project_id=?",
            (str(body.session_id), project_id),
        ).fetchone()
        messages = json.loads(session["messages_json"]) if session else []
        messages = [*messages, {"role": "user", "content_text": body.message.strip()}]
        topics = [dict(row) for row in ledger.connection.execute(
            "SELECT DISTINCT growth_topics.topic_id, growth_topics.title, growth_topics.status "
            "FROM growth_topics WHERE growth_topics.status='active' AND ("
            "EXISTS (SELECT 1 FROM growth_tasks JOIN opportunities USING(opportunity_id) "
            "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
            "JOIN features USING(feature_id) WHERE growth_tasks.topic_id=growth_topics.topic_id "
            "AND features.project_id=?) OR EXISTS (SELECT 1 FROM local_explorations "
            "WHERE local_explorations.topic_id=growth_topics.topic_id "
            "AND local_explorations.project_id=?)) ORDER BY growth_topics.created_at DESC LIMIT 10",
            (project_id, project_id),
        )]
        notes = [dict(row) for row in ledger.connection.execute(
            "SELECT engineering_notes.note_id, engineering_notes.content_text, "
            "engineering_notes.source_refs_json FROM engineering_notes "
            "JOIN growth_tasks USING(task_id) JOIN opportunities USING(opportunity_id) "
            "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
            "JOIN features USING(feature_id) WHERE features.project_id=? "
            "ORDER BY engineering_notes.created_at DESC LIMIT 5", (project_id,),
        )]
        for note in notes:
            note["source_refs"] = json.loads(note.pop("source_refs_json"))
        attempts = [dict(row) for row in ledger.connection.execute(
            "SELECT learning_attempts.attempt_id, learning_attempts.parent_attempt_id, "
            "learning_attempts.answer_text, learning_attempts.feedback_json, "
            "growth_tasks.question, growth_tasks.source_refs_json "
            "FROM learning_attempts JOIN growth_tasks USING(task_id) "
            "JOIN opportunities USING(opportunity_id) JOIN analysis_runs USING(analysis_id) "
            "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
            "WHERE features.project_id=? ORDER BY learning_attempts.created_at DESC LIMIT 5",
            (project_id,),
        )]
        for attempt in attempts:
            attempt["source_refs"] = json.loads(attempt.pop("source_refs_json"))
            feedback_json = attempt.pop("feedback_json")
            attempt["feedback"] = json.loads(feedback_json) if feedback_json else None
        code_row = ledger.connection.execute(
            "SELECT snapshots.snapshot_id,snapshots.head_sha,snapshots.patch_text,"
            "growth_tasks.source_refs_json,"
            "(SELECT binding_id FROM snapshot_bindings "
            "WHERE snapshot_id=snapshots.snapshot_id ORDER BY binding_id LIMIT 1) "
            "AS source_binding_id "
            "FROM growth_tasks JOIN opportunities USING(opportunity_id) "
            "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
            "JOIN features USING(feature_id) WHERE features.project_id=? "
            "ORDER BY growth_tasks.updated_at DESC LIMIT 1", (project_id,),
        ).fetchone()
        approved_sources: list[dict[str, Any]] = []
        if code_row:
            project_name = ledger.connection.execute(
                "SELECT name FROM projects WHERE project_id=?", (project_id,),
            ).fetchone()[0]
            for source_id in json.loads(code_row["source_refs_json"])[:8]:
                source = ledger.connection.execute(
                    "SELECT source_ref_id,relative_path FROM source_refs "
                    "WHERE snapshot_id=? AND source_ref_id=?",
                    (code_row["snapshot_id"], str(source_id)),
                ).fetchone()
                if source and source["relative_path"] in code_row["patch_text"]:
                    approved_sources.append({
                        "source_ref_id": source["source_ref_id"],
                        "project_id": project_id, "project_name": project_name,
                        "binding_id": code_row["source_binding_id"],
                        "commit_sha": code_row["head_sha"],
                        "relative_path": source["relative_path"],
                    })
        chat = {"session": {"session_id": str(body.session_id),
                            "project_id": project_id}, "messages": messages}
        preview = prepare_chat_preview(root, body.provider_name, chat, {
            "memory_status": "local", "topics": topics, "notes": notes,
            "attempts": attempts,
            "code_context": str(code_row["patch_text"]) if code_row else "",
        })
        return preview, root, messages, approved_sources


def _local_explore_generate(
    state: ServeState, project_id: str, body: _ExploreGenerateInput,
) -> dict[str, Any]:
    preview, root, messages, approved_sources = _local_explore_preview(
        state, project_id, body,
    )
    if not body.approved or preview.approval_hash != body.approved_payload_hash:
        raise ValueError("对话发送内容尚未确认或已变化")
    reply, response = generate_chat_reply(root, preview)
    now = datetime.now(UTC).isoformat()
    reply_text = reply.reply.casefold()
    cited_sources = [source for source in approved_sources
                     if source["relative_path"].casefold() in reply_text]
    messages.append({"role": "assistant", "content_text": reply.reply,
                     "source_refs": cited_sources})
    suggestion = reply.suggestion.model_dump() if reply.suggestion else None
    with _ledger(state) as ledger, ledger.connection:
        ledger.connection.execute(
            "INSERT INTO local_explorations "
            "(session_id,project_id,messages_json,suggestion_json,topic_id,created_at,updated_at,title) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET messages_json=excluded.messages_json, "
            "suggestion_json=excluded.suggestion_json, updated_at=excluded.updated_at, "
            "title=COALESCE(local_explorations.title,excluded.title)",
            (str(body.session_id), project_id, json.dumps(messages, ensure_ascii=False),
             json.dumps(suggestion, ensure_ascii=False) if suggestion else None,
             now, now, suggestion["title"] if suggestion else None),
        )
        session_title = ledger.connection.execute(
            "SELECT title FROM local_explorations WHERE session_id=?",
            (str(body.session_id),),
        ).fetchone()[0]
        ledger.record_event(str(uuid.uuid4()), "exploration", str(body.session_id),
                            "exploration_replied", {"project_id": project_id,
                                                    "provider_request_id": response.request_id})
    return {"session_id": str(body.session_id), "messages": messages,
            "suggestion": suggestion, "title": session_title}


def _local_explore_topic(state: ServeState, project_id: str, session_id: str) -> dict[str, str]:
    with _ledger(state) as ledger, ledger.connection:
        session = ledger.connection.execute(
            "SELECT suggestion_json, topic_id FROM local_explorations "
            "WHERE session_id=? AND project_id=?", (session_id, project_id),
        ).fetchone()
        if session is None or not session["suggestion_json"]:
            raise ValueError("当前对话没有待确认的主题建议")
        if session["topic_id"]:
            return {"topic_id": str(session["topic_id"])}
        suggestion = json.loads(session["suggestion_json"])
        title = str(suggestion["title"]).strip()
        existing = ledger.connection.execute(
            "SELECT topic_id FROM growth_topics WHERE title=? AND status='active'",
            (title,),
        ).fetchone()
        topic_id = str(existing["topic_id"]) if existing else str(uuid.uuid4())
        if not existing:
            ledger.connection.execute("INSERT INTO growth_topics VALUES (?, ?, 'active', ?)",
                                      (topic_id, title, datetime.now(UTC).isoformat()))
        ledger.connection.execute(
            "UPDATE local_explorations SET topic_id=? WHERE session_id=?",
            (topic_id, session_id),
        )
        ledger.record_event(str(uuid.uuid4()), "topic", topic_id, "exploration_topic_confirmed",
                            {"session_id": session_id, "project_id": project_id})
        return {"topic_id": topic_id}


def _materials_preview(state: ServeState, task_id: str, body: _MaterialsInput) -> Any:
    with _ledger(state) as ledger:
        row = ledger.connection.execute(
            "SELECT analysis_runs.snapshot_id, growth_tasks.source_refs_json "
            "FROM growth_tasks JOIN opportunities USING(opportunity_id) "
            "JOIN analysis_runs USING(analysis_id) WHERE growth_tasks.task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError("卡片不存在")
        return prepare_model_preview(
            ledger, str(row["snapshot_id"]), str(body.binding_id),
            body.provider_name, json.loads(row["source_refs_json"]),
        )


def _run_materials(state: ServeState, task_id: str, body: _MaterialsGenerateInput) -> dict[str, str]:
    try:
        preview = _materials_preview(state, task_id, body)
        with _ledger(state) as ledger:
            row = ledger.connection.execute(
                "SELECT bindings.canonical_local_path FROM bindings WHERE binding_id=?",
                (str(body.binding_id),),
            ).fetchone()
            assert row is not None
            root = Path(row["canonical_local_path"])
        result = run_learn_pipeline(LearnRequest(
            workspace_root=root, patch_text=preview.selected_patch,
            provider_name=body.provider_name, privacy_mode="redacted_remote",
            lang="zh-CN", force_learn=True, active_practice=True,
        ))
        run_path = root / ".ahadiff" / "runs" / result.run_id
        if not (run_path / "lesson" / "lesson.compact.md").is_file() or not (
            run_path / "quiz" / "quiz.jsonl"
        ).is_file():
            raise ValueError("AhaDiff 未生成完整讲解和练习")
        with _ledger(state) as ledger, ledger.connection:
            ledger.connection.execute(
                "UPDATE growth_task_materials SET status='ready', run_id=?, error=NULL "
                "WHERE task_id=?", (result.run_id, task_id),
            )
        return {"run_id": result.run_id, "status": "ready"}
    except Exception as exc:
        with _ledger(state) as ledger, ledger.connection:
            ledger.connection.execute(
                "UPDATE growth_task_materials SET status='failed', error=? WHERE task_id=?",
                (f"{type(exc).__name__}: {str(exc)[:200]}", task_id),
            )
        raise


def _materials_read(state: ServeState, task_id: str) -> dict[str, Any]:
    with _ledger(state) as ledger:
        row = ledger.connection.execute(
            "SELECT materials.run_id, materials.status, materials.error, "
            "bindings.canonical_local_path FROM growth_task_materials materials "
            "JOIN growth_tasks USING(task_id) JOIN opportunities USING(opportunity_id) "
            "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
            "JOIN snapshot_bindings USING(snapshot_id) JOIN bindings USING(binding_id) "
            "WHERE materials.task_id=? ORDER BY snapshot_bindings.captured_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if row is None:
        return {"status": "none", "run_id": None, "lesson": None, "questions": []}
    result: dict[str, Any] = {"status": row["status"], "run_id": row["run_id"],
                              "error": row["error"], "lesson": None, "questions": []}
    if row["status"] != "ready" or not row["run_id"]:
        return result
    run_path = Path(row["canonical_local_path"]) / ".ahadiff" / "runs" / row["run_id"]
    result["lesson"] = (run_path / "lesson" / "lesson.compact.md").read_text(
        encoding="utf-8")[:12000]
    questions = load_quiz_questions(run_path / "quiz" / "quiz.jsonl")[:5]
    with _ledger(state) as ledger:
        result["attempts"] = [dict(attempt) for attempt in ledger.connection.execute(
            "SELECT attempt_id, question_id, answer_text, created_at "
            "FROM growth_material_attempts WHERE task_id=? ORDER BY created_at",
            (task_id,),
        )]
    answered_ids = {attempt["question_id"] for attempt in result["attempts"]}
    result["questions"] = []
    for question in questions:
        item = {"question_id": question.question_id, "question": question.question,
                "quiz_kind": question.quiz_kind, "exercise_kind": question.exercise_kind,
                "practice_only": True, "review_card_id": None,
                "review_due_date": None, "review_reps": 0}
        if question.question_id in answered_ids:
            item["expected_answer"] = question.expected_answer
            item["explanation"] = question.explanation
        result["questions"].append(item)
    misconception_path = run_path / "quiz" / "misconception_cards.jsonl"
    result["misconceptions"] = []
    if misconception_path.is_file():
        for line in misconception_path.read_text(encoding="utf-8").splitlines()[:5]:
            card = json.loads(line)
            result["misconceptions"].append({
                "card_id": card.get("card_id"), "misconception": card.get("misconception"),
                "correction": card.get("correction"), "evidence_ref": card.get("evidence_ref"),
            })
    return result


def _material_reveal(state: ServeState, task_id: str, question_id: str,
                     answer: str) -> dict[str, str]:
    material = _materials_read(state, task_id)
    if material["status"] != "ready" or question_id not in {
        item["question_id"] for item in material["questions"]
    }:
        raise ValueError("练习题不存在")
    with _ledger(state) as ledger, ledger.connection:
        root = ledger.connection.execute(
            "SELECT canonical_local_path FROM bindings JOIN snapshot_bindings USING(binding_id) "
            "JOIN snapshots USING(snapshot_id) JOIN analysis_runs USING(snapshot_id) "
            "JOIN opportunities USING(analysis_id) JOIN growth_tasks USING(opportunity_id) "
            "WHERE task_id=? LIMIT 1", (task_id,),
        ).fetchone()
        assert root is not None
        run_path = Path(root[0]) / ".ahadiff" / "runs" / material["run_id"]
        question = next(q for q in load_quiz_questions(run_path / "quiz" / "quiz.jsonl")
                        if q.question_id == question_id)
        attempt_id = str(uuid.uuid4())
        ledger.connection.execute(
            "INSERT INTO growth_material_attempts VALUES (?, ?, ?, ?, ?)",
            (attempt_id, task_id, question_id, answer, datetime.now(UTC).isoformat()),
        )
        ledger.record_event(str(uuid.uuid4()), "material_attempt", attempt_id,
                            "material_answered", {"task_id": task_id,
                                                  "question_id": question_id})
    return {"attempt_id": attempt_id, "expected_answer": question.expected_answer,
            "explanation": question.explanation}


def _material_rate(state: ServeState, task_id: str, question_id: str,
                   body: _MaterialRatingInput) -> dict[str, Any]:
    raise ValueError("材料题仅供练习；请在正式知识卡的复习页评分")


def _untracked(state: ServeState, binding_id: str) -> dict[str, list[str]]:
    with _ledger(state) as ledger:
        row = ledger.connection.execute(
            "SELECT canonical_local_path FROM bindings WHERE binding_id=?", (binding_id,),
        ).fetchone()
        if row is None:
            raise ValueError("仓库绑定不存在")
        raw = git(Path(row[0]), "ls-files", "--others", "--exclude-standard", "-z")
        paths = sorted(
            item.decode("utf-8", errors="replace")
            for item in raw.split(b"\x00") if item
            and not is_internal_state_path(item.decode("utf-8", errors="replace"))
        )
        return {"paths": paths[:80]}


def _model_sources(
    state: ServeState, snapshot_id: str, binding_id: str,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        row = ledger.connection.execute(
            "SELECT bindings.canonical_local_path FROM snapshots "
            "JOIN snapshot_bindings USING (snapshot_id) "
            "JOIN bindings USING (binding_id) "
            "WHERE snapshots.snapshot_id=? AND bindings.binding_id=?",
            (snapshot_id, binding_id),
        ).fetchone()
        if row is None:
            raise ValueError("快照与仓库绑定不匹配")
        sources = [dict(item) for item in ledger.connection.execute(
            "SELECT source_ref_id, relative_path, blob_hash, deleted, "
            "LENGTH(content) AS byte_count FROM source_refs "
            "WHERE snapshot_id=? ORDER BY relative_path",
            (snapshot_id,),
        )]
        return {
            "sources": sources,
            "provider_names": configured_provider_names(Path(row["canonical_local_path"])),
        }


def _code_index(
    state: ServeState, snapshot_id: str, body: _CodeIndexInput,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        row = ledger.connection.execute(
            "SELECT snapshots.resolved_base_sha, snapshots.head_sha, "
            "snapshots.effective_tree_hash, snapshots.diff_hash, "
            "snapshots.capture_scope, features.base_ref, "
            "bindings.canonical_local_path FROM snapshots "
            "JOIN features USING (feature_id) "
            "JOIN snapshot_bindings USING (snapshot_id) "
            "JOIN bindings USING (binding_id) "
            "WHERE snapshots.snapshot_id=? AND bindings.binding_id=?",
            (snapshot_id, str(body.binding_id)),
        ).fetchone()
    if row is None:
        raise ValueError("快照与仓库绑定不匹配")
    repo = Path(row["canonical_local_path"])
    scope = json.loads(row["capture_scope"])
    selected = set(scope["selected_untracked"])
    current = capture_worktree(repo, str(row["base_ref"]), selected)
    if (
        current.resolved_base_sha != row["resolved_base_sha"]
        or current.head_sha != row["head_sha"]
        or current.effective_tree_hash != row["effective_tree_hash"]
        or current.diff_hash != row["diff_hash"]
    ):
        raise RuntimeError("snapshot_stale")
    with TemporaryDirectory(prefix="growth-code-index-") as temp:
        index = build_index(
            repo, base_ref=str(row["base_ref"]), selected_untracked=selected,
            snapshot_id=snapshot_id, expected=current, index_root=Path(temp),
        )
        flow = index.call_path(
            body.symbols, snapshot_id=snapshot_id,
            effective_tree_hash=current.effective_tree_hash,
        )
        return {
            "snapshot_id": snapshot_id,
            "effective_tree_hash": current.effective_tree_hash,
            "index_revision": index.index_revision,
            "codegraph_version": index.codegraph_version,
            "indexed_files": index.indexed_files,
            "flow": flow,
        }


def _model_preview(
    state: ServeState, snapshot_id: str, body: _ModelPreviewInput,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        preview = prepare_model_preview(
            ledger, snapshot_id, str(body.binding_id), body.provider_name,
            [str(item) for item in body.source_ref_ids],
        )
        return {
            "payload_text": preview.payload_text,
            "approval_hash": preview.approval_hash,
            "provider_host": preview.provider_host,
            "model_name": preview.model_name,
            "source_ref_ids": preview.source_ref_ids,
        }


def _analysis_precheck(
    state: ServeState, snapshot_id: str, binding_id: str,
    *, retry_id: str | None = None,
) -> None:
    with _ledger(state) as ledger:
        if retry_id is None:
            row = ledger.connection.execute(
                "SELECT 1 FROM snapshots JOIN snapshot_bindings USING (snapshot_id) "
                "JOIN bindings USING (binding_id) "
                "WHERE snapshots.snapshot_id=? AND bindings.binding_id=?",
                (snapshot_id, binding_id),
            ).fetchone()
        else:
            row = ledger.connection.execute(
                "SELECT 1 FROM analysis_runs JOIN snapshots USING (snapshot_id) "
                "JOIN snapshot_bindings USING (snapshot_id) "
                "JOIN bindings USING (binding_id) "
                "LEFT JOIN analysis_model_requests USING (analysis_id) "
                "WHERE analysis_runs.analysis_id=? AND analysis_runs.status='failed' "
                "AND bindings.binding_id=? "
                "AND (analysis_runs.mode!='live' "
                "OR analysis_model_requests.binding_id=bindings.binding_id)",
                (retry_id, binding_id),
            ).fetchone()
        if row is None:
            raise ValueError("快照或失败分析与本机仓库绑定不匹配")


def _queue_analysis(
    state: ServeState, snapshot_id: str, body: _AnalysisInput,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        event_id = str(body.event_id) if body.event_id else None
        account_id = str(body.account_id) if body.account_id else None
        if event_id is not None:
            assert account_id is not None
            previous = event_analysis_request(
                ledger, account_id, event_id, snapshot_id, str(body.binding_id),
            )
            if previous is not None:
                row = ledger.connection.execute(
                    "SELECT status, upstream_run_id FROM analysis_runs WHERE analysis_id=?",
                    (previous,),
                ).fetchone()
                assert row is not None
                return {"analysis_id": previous, "created": False,
                        "status": row["status"],
                        "upstream_run_id": row["upstream_run_id"]}
        if body.mode == "live":
            assert body.provider_name is not None
            assert body.source_ref_ids is not None
            assert body.approved_payload_hash is not None
            analysis_id, created = ledger.queue_live_opportunities(
                snapshot_id, str(body.binding_id), event_id or str(body.request_id),
                provider_name=body.provider_name,
                source_ref_ids=[str(item) for item in body.source_ref_ids],
                approved_payload_hash=body.approved_payload_hash,
                trace_id=str(uuid.uuid4()),
                reanalysis=body.reanalysis,
            )
            if event_id is not None:
                assert account_id is not None
                link_event_analysis(
                    ledger, account_id, event_id, analysis_id,
                    trace_id=str(uuid.uuid4()),
                )
        else:
            analysis_id, created = ledger.queue_analysis(
                snapshot_id, str(body.binding_id), str(body.request_id),
                trace_id=str(uuid.uuid4()),
                reanalysis=body.reanalysis,
            )
        row = ledger.connection.execute(
            "SELECT status, upstream_run_id FROM analysis_runs WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        assert row is not None
        return {"analysis_id": analysis_id, "created": created,
                "status": row["status"],
                "upstream_run_id": row["upstream_run_id"]}


def _existing_event_analysis(
    state: ServeState, snapshot_id: str, body: _AnalysisInput,
) -> dict[str, Any] | None:
    assert body.account_id is not None and body.event_id is not None
    with _ledger(state) as ledger:
        previous = event_analysis_request(
            ledger, str(body.account_id), str(body.event_id),
            snapshot_id, str(body.binding_id),
        )
        if previous is None:
            return None
        row = ledger.connection.execute(
            "SELECT status FROM analysis_runs WHERE analysis_id=?", (previous,),
        ).fetchone()
        assert row is not None
        return {"analysis_id": previous, "status": row["status"], "reused": True}


def _run_analysis(
    state: ServeState, analysis_id: str, binding_id: str,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        ledger.execute_queued_analysis(
            analysis_id, binding_id, trace_id=str(uuid.uuid4()),
        )
        row = ledger.connection.execute(
            "SELECT status, upstream_run_id FROM analysis_runs WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        assert row is not None
        return {"analysis_id": analysis_id, "status": row["status"],
                "upstream_run_id": row["upstream_run_id"]}


def _queue_retry_analysis(
    state: ServeState, analysis_id: str, body: _AnalysisRetryInput,
) -> None:
    with _ledger(state) as ledger:
        ledger.queue_retry_analysis(
            analysis_id, str(body.binding_id), trace_id=str(uuid.uuid4())
        )


def _reject_analysis(state: ServeState, analysis_id: str) -> None:
    with _ledger(state) as ledger:
        ledger.reject_queued_analysis(analysis_id, "分析任务排队容量已满")


def _decide_topic(
    state: ServeState, proposal_id: str, body: _TopicDecisionInput,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        existing_title = None
        if (body.existing_topic_id is None) != (body.account_id is None):
            raise ValueError("关联已有主题必须指定主题和账号")
        if body.existing_topic_id is not None:
            if body.decision != "confirm":
                raise ValueError("只能在确认时关联已有主题")
            project = ledger.connection.execute(
                "SELECT features.project_id FROM topic_proposals "
                "JOIN opportunities USING(opportunity_id) "
                "JOIN analysis_runs USING(analysis_id) "
                "JOIN snapshots USING(snapshot_id) "
                "JOIN features USING(feature_id) WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if project is None:
                raise ValueError("主题建议不存在")
            policy = ledger.project_policy(str(project["project_id"]))
            if not policy["cloud_allowed"] or policy["cloud_account_id"] != str(body.account_id):
                raise ValueError("已有主题不属于当前项目绑定的云账号")
            cached = ledger.connection.execute(
                "SELECT entities.payload_json FROM sync_entities entities "
                "JOIN sync_accounts accounts ON accounts.account_id=entities.account_id "
                "WHERE entities.account_id=? AND accounts.bootstrapped=1 "
                "AND entities.entity_type='topic' AND entities.entity_id=? "
                "AND entities.deleted_at IS NULL",
                (str(body.account_id), str(body.existing_topic_id)),
            ).fetchone()
            topic = json.loads(cached["payload_json"]) if cached else {}
            alias = ledger.connection.execute(
                "SELECT 1 FROM sync_entities WHERE account_id=? "
                "AND entity_type='topic_alias' AND entity_id=? AND deleted_at IS NULL",
                (str(body.account_id), str(body.existing_topic_id)),
            ).fetchone()
            if (not topic or topic.get("status", "active") != "active"
                    or topic.get("canonical_topic_id") or alias is not None):
                raise ValueError("已有主题未同步、已归档或为合并别名")
            existing_title = str(topic.get("title") or "").strip()
            if not existing_title:
                raise ValueError("已有主题标题缺失")
        topic_id = GrowthLearningService(ledger).decide_topic(
            proposal_id, body.decision, trace_id=str(uuid.uuid4()),
            existing_topic_id=str(body.existing_topic_id) if body.existing_topic_id else None,
            existing_topic_title=existing_title,
        )
        return {"topic_id": topic_id, "decision": body.decision}


def _create_learning_task(
    state: ServeState, opportunity_id: str, body: _TaskCreateInput,
) -> dict[str, str]:
    with _ledger(state) as ledger:
        opportunity = ledger.connection.execute(
            "SELECT learning_goal, card_json FROM opportunities WHERE opportunity_id=?",
            (opportunity_id,),
        ).fetchone()
        if opportunity is None:
            raise ValueError("学习机会不存在")
        card = json.loads(opportunity["card_json"]) if opportunity["card_json"] else None
        # 正式卡的正面以用户确认的单一学习目标为准，避免生成题目偏离机会主题。
        question = str(opportunity["learning_goal"]).strip()
        task_id = GrowthLearningService(ledger).create_task(
            opportunity_id, str(body.topic_id), question,
            trace_id=str(uuid.uuid4()),
            followups=card["followups"] if card else None,
            module_id=str(body.module_id) if body.module_id else None,
            module_index_revision=body.module_index_revision,
        )
        return {"task_id": task_id}


def _task_chain(state: ServeState, task_id: str) -> dict[str, Any]:
    with _ledger(state) as ledger:
        return GrowthLearningService(ledger).export_task_chain(task_id)


def _submit_answer(
    state: ServeState, task_id: str, body: _AnswerInput,
) -> dict[str, str]:
    with _ledger(state) as ledger:
        attempt_id = GrowthLearningService(ledger).submit_answer(
            task_id, body.answer_text, trace_id=str(uuid.uuid4()),
            actor_origin="user",
            parent_attempt_id=(str(body.parent_attempt_id) if body.parent_attempt_id else None),
            hint_level=body.hint_level,
        )
        return {"attempt_id": attempt_id}


def _request_hint(
    state: ServeState, task_id: str, body: _HintInput,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        return GrowthLearningService(ledger).request_hint(
            task_id, str(body.request_id), trace_id=str(uuid.uuid4()),
        )


def _save_note(state: ServeState, task_id: str, body: _NoteInput) -> dict[str, str]:
    with _ledger(state) as ledger:
        note_id = GrowthLearningService(ledger).save_user_note(
            task_id, body.content_text, trace_id=str(uuid.uuid4()),
        )
        return {"note_id": note_id}


def _set_task_progress(
    state: ServeState, task_id: str, body: _TaskProgressInput,
) -> dict[str, str]:
    with _ledger(state) as ledger:
        GrowthLearningService(ledger).set_task_progress(
            task_id, body.target, trace_id=str(uuid.uuid4()),
        )
        return {"status": body.target}


def _feedback_preview(
    state: ServeState, attempt_id: str, binding_id: str,
) -> dict[str, str]:
    with _ledger(state) as ledger:
        preview = prepare_feedback_preview(ledger, attempt_id, binding_id)
        return {
            "payload_text": preview.payload_text,
            "approval_hash": preview.approval_hash,
            "provider_host": preview.provider_host,
            "model_name": preview.model_name,
        }


def _queue_feedback(
    state: ServeState, attempt_id: str, body: _FeedbackSubmitInput,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        created, status = GrowthFeedbackService(ledger).queue(
            attempt_id, str(body.binding_id), str(body.request_id),
            body.approved_payload_hash, retry=body.retry,
            trace_id=str(uuid.uuid4()),
        )
        return {"attempt_id": attempt_id, "created": created, "status": status}


def _run_feedback(state: ServeState, attempt_id: str) -> dict[str, str]:
    with _ledger(state) as ledger:
        GrowthFeedbackService(ledger).execute(attempt_id, trace_id=str(uuid.uuid4()))
        return {"attempt_id": attempt_id}


def _reject_feedback(state: ServeState, attempt_id: str) -> None:
    with _ledger(state) as ledger:
        GrowthFeedbackService(ledger).reject_queued(attempt_id, "反馈任务排队容量已满")


async def _parse(request: Request, model: type[TInput]) -> TInput | JSONResponse:
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=422)


def _failure(exc: Exception) -> JSONResponse:
    if isinstance(exc, ValueError | subprocess.CalledProcessError | FileNotFoundError):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_invalid_git_reference", status=422
        )
    if isinstance(exc, sqlite3.IntegrityError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_relationship", status=409)
    raise exc


async def growth_local_list(request: Request) -> JSONResponse:
    require_write_token(request)
    state = serve_state(request)
    return JSONResponse(await to_thread.run_sync(_list, state))


async def growth_local_sync_preview(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SyncPreviewInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        with _ledger(serve_state(request)) as ledger:
            plan = publication_plan(ledger, str(body.project_id))
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=422)
    return JSONResponse(plan)


def _source_excerpt_preview(
    ledger: GrowthLocalRepository, source_ref_id: str, start_line: int, end_line: int,
) -> dict[str, Any]:
    if end_line < start_line or end_line - start_line >= 120:
        raise ValueError("片段行号无效或超过 120 行")
    source = ledger.connection.execute(
        "SELECT refs.source_ref_id, refs.snapshot_id, refs.relative_path, "
        "refs.blob_hash, refs.content, refs.deleted, features.project_id "
        "FROM source_refs refs JOIN snapshots USING(snapshot_id) "
        "JOIN features USING(feature_id) WHERE refs.source_ref_id=?",
        (source_ref_id,),
    ).fetchone()
    if source is None or source["deleted"]:
        raise ValueError("源码引用不存在或已删除")
    content = bytes(source["content"])
    if hashlib.sha256(content).hexdigest() != source["blob_hash"]:
        raise ValueError("本机源码内容与快照哈希不一致")
    try:
        lines = content.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise ValueError("非 UTF-8 源码不能作为云片段") from exc
    if end_line > len(lines):
        raise ValueError("片段行号超出源码范围")
    excerpt = "".join(lines[start_line - 1:end_line])
    if (not excerpt.strip() or len(excerpt.encode("utf-8")) > 8192
            or "\x00" in excerpt
            or scan_text_for_secrets(excerpt, source_name="growth_cloud_excerpt")
            or re.search(r"(?i)(?:[a-z]:[\\/]|/(?:home|users)/[^/\s]+)", excerpt)):
        raise ValueError("片段为空、超限或包含敏感内容")
    content_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    approval = hashlib.sha256(
        f"growth-excerpt-v1:{source_ref_id}:{source['blob_hash']}:"
        f"{start_line}:{end_line}:{content_hash}".encode()
    ).hexdigest()
    return {
        "source_ref_id": source_ref_id, "snapshot_id": source["snapshot_id"],
        "project_id": source["project_id"], "relative_path": source["relative_path"],
        "blob_hash": source["blob_hash"], "start_line": start_line,
        "end_line": end_line, "content_text": excerpt,
        "content_hash": content_hash, "approval_hash": approval,
    }


async def growth_local_source_excerpt_preview(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SourceExcerptPreviewInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        with _ledger(serve_state(request)) as ledger:
            preview = _source_excerpt_preview(
                ledger, str(request.path_params["source_ref_id"]),
                body.start_line, body.end_line,
            )
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=422)
    return JSONResponse(preview)


async def growth_local_source_excerpt_queue(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SourceExcerptQueueInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        with _ledger(serve_state(request)) as ledger:
            preview = _source_excerpt_preview(
                ledger, str(request.path_params["source_ref_id"]),
                body.start_line, body.end_line,
            )
            if preview["approval_hash"] != body.approval_hash:
                raise ValueError("批准时片段版本已变化")
            account = ledger.connection.execute(
                "SELECT device_id,bootstrapped FROM sync_accounts WHERE account_id=?",
                (str(body.account_id),),
            ).fetchone()
            if account is None or not account["bootstrapped"]:
                raise ValueError("片段云同步须先连接账号")
            store = GrowthSyncStore(
                ledger, body.account_id, uuid.UUID(account["device_id"]),
            )
            excerpt_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"growth-excerpt-v1:{body.account_id}:{preview['source_ref_id']}",
            )
            receipt = store.queue_source_excerpt(
                project_id=preview["project_id"],
                source_ref_id=preview["source_ref_id"],
                payload={
                    "excerpt_id": str(excerpt_id),
                    "source_ref_id": preview["source_ref_id"],
                    "snapshot_id": preview["snapshot_id"],
                    "start_line": preview["start_line"],
                    "end_line": preview["end_line"],
                    "content_text": preview["content_text"],
                    "content_hash": preview["content_hash"],
                },
            )
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=409)
    return JSONResponse(receipt, status_code=201 if receipt["state"] == "pending" else 200)


async def growth_local_sync_workspace(request: Request) -> JSONResponse:
    require_write_token(request)
    with _ledger(serve_state(request)) as ledger:
        accounts = ledger.connection.execute(
            "SELECT account_id, device_id, cloud_origin, cursor FROM sync_accounts "
            "WHERE bootstrapped=1 ORDER BY account_id"
        ).fetchall()
        notes: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        account_summaries: list[dict[str, Any]] = []
        for account in accounts:
            store = GrowthSyncStore(
                ledger, uuid.UUID(account["account_id"]), uuid.UUID(account["device_id"])
            )
            notes.extend(store.note_workspace())
            tasks.extend(store.task_workspace())
            counts = {row["state"]: int(row["total"]) for row in ledger.connection.execute(
                "SELECT state, COUNT(*) AS total FROM sync_outbox "
                "WHERE account_id=? GROUP BY state", (account["account_id"],),
            )}
            account_summaries.append({
                "account_id": account["account_id"],
                "device_id": account["device_id"],
                "cloud_origin": account["cloud_origin"],
                "sync_summary": {
                    "account_id": account["account_id"],
                    "device_id": account["device_id"],
                    "cursor": int(account["cursor"]),
                    "acknowledged_cursor": int(account["cursor"]),
                    "pulled": 0, "uploaded": 0, "queued": 0,
                    "outbox": {state: counts.get(state, 0) for state in (
                        "pending", "acked", "conflict", "deleted"
                    )},
                },
            })
        return JSONResponse({
            "accounts": account_summaries,
            "notes": notes, "tasks": tasks,
        })


async def growth_local_sync_source_locations(request: Request) -> JSONResponse:
    """Resolve a synced card against an explicitly bound local repository."""
    require_write_token(request)
    task_id = str(request.path_params["task_id"])
    binding_id = request.query_params.get("binding_id", "")
    with _ledger(serve_state(request)) as ledger:
        binding = ledger.connection.execute(
            "SELECT project_id, canonical_local_path FROM bindings WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        if binding is None:
            return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_binding", status=404)
        accounts = ledger.connection.execute(
            "SELECT account_id, device_id FROM sync_accounts WHERE bootstrapped=1"
        ).fetchall()
        task = next((item for account in accounts for item in GrowthSyncStore(
            ledger, uuid.UUID(account["account_id"]), uuid.UUID(account["device_id"])
        ).task_workspace() if item["task_id"] == task_id
            and item["project_id"] == binding["project_id"]), None)
        if task is None:
            return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_task", status=404)
        root = Path(binding["canonical_local_path"]).resolve()
        locations = []
        for source in task["approved_sources"]:
            relative = source["relative_path"]
            status = "missing"
            local_path = None
            if isinstance(relative, str) and relative:
                candidate = (root / relative).resolve()
                if candidate.is_relative_to(root) and candidate.is_file():
                    local_path = str(candidate)
                    status = ("matched" if hashlib.sha256(candidate.read_bytes()).hexdigest()
                              == source["blob_hash"] else "version_mismatch")
            locations.append({
                "source_ref_id": source["source_ref_id"], "relative_path": relative,
                "expected_blob_hash": source["blob_hash"], "status": status,
                "local_path": local_path,
            })
        return JSONResponse({"task_id": task_id, "project_id": binding["project_id"],
                             "binding_id": binding_id, "locations": locations})


async def growth_local_sync_binding(request: Request) -> JSONResponse:
    """Adopt the cached cloud project ID and bind a second device's own Git path."""
    require_write_token(request)
    body = await _parse(request, _BindingInput)
    if isinstance(body, JSONResponse):
        return body
    task_id = str(request.path_params["task_id"])
    try:
        root = repository_root(Path(body.path))
        with _ledger(serve_state(request)) as ledger:
            accounts = ledger.connection.execute(
                "SELECT account_id, device_id FROM sync_accounts WHERE bootstrapped=1"
            ).fetchall()
            match = next(((account, store, task) for account in accounts
                          for store in [GrowthSyncStore(
                              ledger, uuid.UUID(account["account_id"]),
                              uuid.UUID(account["device_id"]))]
                          for task in store.task_workspace() if task["task_id"] == task_id
                          and task["project_id"]), None)
            if match is None:
                return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_task", status=404)
            account, store, task = match
            project_id = task["project_id"]
            project = store._cached_payload("project", project_id)
            if not project or project.get("deleted_at") or not project.get("name"):
                return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_project", status=404)
            with ledger.connection:
                ledger.connection.execute(
                    "INSERT OR IGNORE INTO projects(project_id,name,created_at) VALUES (?,?,?)",
                    (project_id, project["name"], project.get("updated_at", "")),
                )
                ledger.connection.execute(
                    "INSERT OR IGNORE INTO project_policies(project_id,updated_at) VALUES (?,?)",
                    (project_id, project.get("updated_at", "")),
                )
                ledger.connection.execute(
                    "UPDATE project_policies SET cloud_allowed=1, cloud_account_id=?, "
                    "cloud_origin=(SELECT cloud_origin FROM sync_accounts WHERE account_id=?), "
                    "updated_at=? WHERE project_id=?",
                    (account["account_id"], account["account_id"],
                     project.get("updated_at", ""), project_id),
                )
                binding_id = ledger.bind_repo(project_id, root, trace_id=str(uuid.uuid4()))
            return JSONResponse({"project_id": project_id, "binding_id": binding_id,
                                 "path": str(root)}, status_code=201)
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError,
            sqlite3.IntegrityError) as exc:
        return _failure(exc)


async def growth_local_cloud_export(request: Request) -> JSONResponse:
    """Create a cloud-owned export and return both formats to this device."""
    require_write_token(request)
    body = await _parse(request, _CloudExportInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        origin = _cloud_origin(body.cloud_url)
        with _ledger(serve_state(request)) as ledger:
            account = ledger.connection.execute(
                "SELECT device_id, cloud_origin FROM sync_accounts "
                "WHERE account_id=? AND bootstrapped=1", (str(body.account_id),),
            ).fetchone()
        if account is None or account["cloud_origin"] != origin:
            raise ValueError("云账号或来源与本机已同步账号不一致")
        headers = {
            "Authorization": f"Bearer {body.access_token.get_secret_value()}",
            "X-Device-Id": account["device_id"],
        }
        export_id = str(body.export_id)
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"growth-export:{body.account_id}:{export_id}",
        )
        async with httpx.AsyncClient(
            base_url=origin, timeout=httpx.Timeout(120.0, connect=5.0),
            follow_redirects=False,
        ) as client:
            identity = await client.get("/v1/account", headers=headers)
            identity.raise_for_status()
            if identity.json().get("account_id") != str(body.account_id):
                raise ValueError("访问令牌与本机同步账号不一致")
            created = await client.post(
                "/v1/exports", headers=headers,
                json={"operation_id": str(operation_id), "export_id": export_id},
            )
            created.raise_for_status()
            json_response = await client.get(
                f"/v1/exports/{export_id}", params={"format": "json"}, headers=headers,
            )
            json_response.raise_for_status()
            markdown_response = await client.get(
                f"/v1/exports/{export_id}", params={"format": "markdown"}, headers=headers,
            )
            markdown_response.raise_for_status()
        return JSONResponse({
            "export_id": export_id,
            "json_text": json.dumps(json_response.json(), ensure_ascii=False, indent=2),
            "markdown_text": markdown_response.text,
        })
    except (ValueError, httpx.HTTPError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_cloud_export_failed", status=409)


async def growth_local_sync_event_inbox(request: Request) -> JSONResponse:
    require_write_token(request)
    with _ledger(serve_state(request)) as ledger:
        return JSONResponse({"events": event_inbox(ledger)})


def _capture_synced_event(
    state: ServeState, event_id: str, body: _SyncEventCaptureInput,
) -> dict[str, Any]:
    with _ledger(state) as ledger:
        return capture_development_event(
            ledger, str(body.account_id), event_id, str(body.binding_id),
            selected_untracked=set(body.selected_untracked),
            trace_id=str(uuid.uuid4()),
        )


async def growth_local_sync_event_capture(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SyncEventCaptureInput)
    if isinstance(body, JSONResponse):
        return body
    state = serve_state(request)
    lock = state.write_lock
    assert lock is not None
    try:
        async with lock:
            result = await to_thread.run_sync(
                _capture_synced_event, state, str(request.path_params["event_id"]), body,
            )
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_event_capture_not_ready", status=409
        )
    return JSONResponse(result, status_code=200 if result["reused"] else 201)


async def _queue_sync_note(request: Request, action: str) -> JSONResponse:
    require_write_token(request)
    input_type = {
        "revision": _SyncNoteRevisionInput,
        "resolve": _SyncNoteResolveInput,
        "delete": _SyncNoteDeleteInput,
    }[action]
    body = await _parse(request, input_type)
    if isinstance(body, JSONResponse):
        return body
    note_id = request.path_params["note_id"]
    state = serve_state(request)
    lock = state.write_lock
    assert lock is not None
    try:
        async with lock:
            with _ledger(state) as ledger:
                account = ledger.connection.execute(
                    "SELECT device_id FROM sync_accounts WHERE account_id=? "
                    "AND bootstrapped=1", (str(body.account_id),),
                ).fetchone()
                if account is None:
                    raise ValueError("同步账号尚未完成初始拉取")
                store = GrowthSyncStore(
                    ledger, body.account_id, uuid.UUID(account["device_id"])
                )
                if action == "revision":
                    assert isinstance(body, _SyncNoteRevisionInput)
                    operation_id = store.queue_note_revision(
                        note_id, body.base_revision, body.content_text,
                    )
                elif action == "resolve":
                    assert isinstance(body, _SyncNoteResolveInput)
                    operation_id = store.queue_note_resolution(
                        note_id, body.base_revision, body.conflict_ids, body.content_text,
                    )
                else:
                    assert isinstance(body, _SyncNoteDeleteInput)
                    operation_id = store.queue_note_delete(note_id, body.base_revision)
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=409)
    return JSONResponse({"operation_id": operation_id, "state": "pending"}, status_code=201)


async def growth_local_sync_note_revision(request: Request) -> JSONResponse:
    return await _queue_sync_note(request, "revision")


async def growth_local_sync_note_resolve(request: Request) -> JSONResponse:
    return await _queue_sync_note(request, "resolve")


async def growth_local_sync_note_delete(request: Request) -> JSONResponse:
    return await _queue_sync_note(request, "delete")


async def _queue_sync_task(request: Request, action: str) -> JSONResponse:
    require_write_token(request)
    input_type = {
        "draft": _SyncTaskDraftInput,
        "rebase": _SyncTaskDraftInput,
        "answer": _SyncTaskAnswerInput,
        "progress": _SyncTaskProgressInput,
    }[action]
    body = await _parse(request, input_type)
    if isinstance(body, JSONResponse):
        return body
    task_id = request.path_params["task_id"]
    state = serve_state(request)
    lock = state.write_lock
    assert lock is not None
    try:
        async with lock:
            with _ledger(state) as ledger:
                account = ledger.connection.execute(
                    "SELECT device_id FROM sync_accounts WHERE account_id=? "
                    "AND bootstrapped=1", (str(body.account_id),),
                ).fetchone()
                if account is None:
                    raise ValueError("同步账号尚未完成初始拉取")
                store = GrowthSyncStore(
                    ledger, body.account_id, uuid.UUID(account["device_id"])
                )
                if action == "draft":
                    assert isinstance(body, _SyncTaskDraftInput)
                    result = {"operation_id": store.queue_task_draft(
                        task_id, body.content_text,
                        after_attempt_id=body.after_attempt_id,
                    )}
                elif action == "rebase":
                    assert isinstance(body, _SyncTaskDraftInput)
                    result = {"operation_id": store.rebase_task_draft(
                        task_id, body.content_text,
                        after_attempt_id=body.after_attempt_id,
                    )}
                elif action == "answer":
                    assert isinstance(body, _SyncTaskAnswerInput)
                    attempt_id, operation_id = store.queue_answer(
                        task_id, body.answer_text,
                        parent_attempt_id=body.parent_attempt_id,
                        hint_level=body.hint_level,
                    )
                    result = {"attempt_id": attempt_id, "operation_id": operation_id}
                else:
                    assert isinstance(body, _SyncTaskProgressInput)
                    task = store.cached_entity("task", str(task_id))
                    pending = ledger.connection.execute(
                        "SELECT 1 FROM sync_outbox WHERE account_id=? "
                        "AND dependency_type='task' AND dependency_id=? "
                        "AND state='pending' LIMIT 1",
                        (str(body.account_id), str(task_id)),
                    ).fetchone()
                    if task.get("revision") != body.base_revision or pending is not None:
                        raise ValueError("进度需在同步当前卡片后修改")
                    result = {"operation_id": store.queue_task_progress(
                        task_id, body.base_revision, body.progress,
                    )}
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=409)
    return JSONResponse({**result, "state": "pending"}, status_code=201)


async def growth_local_sync_task_draft(request: Request) -> JSONResponse:
    return await _queue_sync_task(request, "draft")


async def growth_local_sync_task_rebase(request: Request) -> JSONResponse:
    return await _queue_sync_task(request, "rebase")


async def growth_local_sync_task_answer(request: Request) -> JSONResponse:
    return await _queue_sync_task(request, "answer")


async def growth_local_sync_task_progress(request: Request) -> JSONResponse:
    return await _queue_sync_task(request, "progress")


async def growth_local_sync(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _SyncInput)
    if isinstance(body, JSONResponse):
        return body
    if (body.publish_project_id is None) != (body.approved_digest is None):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_relationship", status=422)
    try:
        origin = _cloud_origin(body.cloud_url)
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_cloud_url", status=422)
    state = serve_state(request)
    lock = state.write_lock
    assert lock is not None
    try:
        async with lock, httpx.AsyncClient(
            base_url=origin, timeout=20.0, follow_redirects=False,
        ) as client:
            with _ledger(state) as ledger:
                result = await connect_and_sync(
                    ledger, client, body.access_token.get_secret_value(),
                    publish_project_id=str(body.publish_project_id)
                    if body.publish_project_id else None,
                    approved_digest=body.approved_digest,
                    refresh_history=body.refresh_history,
                )
    except httpx.HTTPStatusError as exc:
        try:
            remote_body = cast("object", exc.response.json())
            code: object = cast("dict[str, object]", remote_body).get(
                "code", "CLOUD_REJECTED"
            ) if isinstance(
                remote_body, dict
            ) else "CLOUD_REJECTED"
        except ValueError:
            code = "CLOUD_REJECTED"
        if not isinstance(code, str) or not code.isascii() or not code.replace("_", "").isalnum():
            code = "CLOUD_REJECTED"
        return JSONResponse(
            {"error_code": "CLOUD_REJECTED", "code": code,
             "remote_status": exc.response.status_code}, status_code=502,
        )
    except SyncOriginMismatchError:
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_cloud_origin_mismatch", status=409
        )
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=409)
    except (httpx.RequestError, KeyError, TypeError):
        return error_response(ErrorCode.PROVIDER_TRANSPORT, "growth_cloud_unavailable", status=502)
    return JSONResponse(result)


async def growth_local_project(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ProjectInput)
    if isinstance(body, JSONResponse):
        return body
    state = serve_state(request)
    try:
        result = await to_thread.run_sync(_create_project, state, body)
    except ValueError as exc:
        return _failure(exc)
    return JSONResponse(result, status_code=201)


async def growth_local_import(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ImportInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(_import_project, serve_state(request), body)
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        return _failure(exc)
    return JSONResponse(result, status_code=200 if result["reused"] == "true" else 201)


async def growth_local_project_policy(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ProjectPolicyInput)
    if isinstance(body, JSONResponse):
        return body
    state = serve_state(request)
    lock = state.write_lock
    assert lock is not None
    try:
        async with lock:
            with _ledger(state) as ledger:
                result = ledger.set_project_policy(
                    str(request.path_params["project_id"]),
                    local_processing=body.local_processing,
                    model_allowed=body.model_allowed,
                    cloud_allowed=body.cloud_allowed,
                )
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=422)
    return JSONResponse(result)


async def growth_local_binding(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _BindingInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _bind, serve_state(request), str(request.path_params["project_id"]), body
        )
    except (
        ValueError, subprocess.CalledProcessError, FileNotFoundError, sqlite3.IntegrityError
    ) as exc:
        return _failure(exc)
    return JSONResponse(result, status_code=201)


async def growth_local_feature(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _FeatureInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _start_feature, serve_state(request), str(request.path_params["project_id"]), body
        )
    except (
        ValueError, subprocess.CalledProcessError, FileNotFoundError, sqlite3.IntegrityError
    ) as exc:
        return _failure(exc)
    return JSONResponse(result, status_code=201)


async def growth_local_capture(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _CaptureInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _capture, serve_state(request), str(request.path_params["feature_id"]), body
        )
    except (
        ValueError, subprocess.CalledProcessError, FileNotFoundError, sqlite3.IntegrityError
    ) as exc:
        return _failure(exc)
    return JSONResponse(result, status_code=201)


async def growth_local_commits(request: Request) -> JSONResponse:
    require_write_token(request)
    feature_id = request.query_params.get("feature_id", "")
    try:
        cursor = int(request.query_params.get("cursor", "0"))
        limit = int(request.query_params.get("limit", "50"))
        result = await to_thread.run_sync(
            _commits, serve_state(request), str(request.path_params["binding_id"]), feature_id,
            cursor, limit, request.query_params.getlist("author_email"),
            request.query_params.getlist("author_group"),
            request.query_params.get("scan") == "true",
        )
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        return _failure(exc)
    return JSONResponse(result)


async def growth_local_authors_group(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _AuthorGroupInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        with _ledger(serve_state(request)) as ledger:
            result = group_authors(
                ledger, str(request.path_params["binding_id"]),
                author_emails=body.author_emails, group_name=body.group_name,
            )
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_authors", status=422)
    return JSONResponse(result)


async def growth_local_card_commits(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _CardCommitsInput)
    if isinstance(body, JSONResponse):
        return body
    card_id = str(request.path_params["card_id"])
    try:
        with _ledger(serve_state(request)) as ledger:
            card = ledger.connection.execute(
                "SELECT features.project_id FROM knowledge_cards JOIN growth_tasks "
                "ON growth_tasks.task_id=knowledge_cards.card_id "
                "JOIN opportunities USING(opportunity_id) JOIN analysis_runs USING(analysis_id) "
                "JOIN snapshots USING(snapshot_id) JOIN features USING(feature_id) "
                "WHERE knowledge_cards.card_id=?", (card_id,),
            ).fetchone()
            binding = ledger.connection.execute(
                "SELECT project_id FROM bindings WHERE binding_id=?",
                (str(body.binding_id),),
            ).fetchone()
            if card is None or binding is None or card["project_id"] != binding["project_id"]:
                raise ValueError("卡片与仓库绑定不匹配")
            shas = list(dict.fromkeys(body.commit_shas))
            if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in shas):
                raise ValueError("提交 SHA 无效")
            known = {row[0] for row in ledger.connection.execute(
                "SELECT commit_sha FROM git_history_commits WHERE binding_id=? "
                "AND reachable=1 AND commit_sha IN (" + ",".join("?" for _ in shas) + ")",
                (str(body.binding_id), *shas),
            )}
            if known != set(shas):
                raise ValueError("提交不在已扫描的本机历史中")
            with ledger.connection:
                for sha in shas:
                    ledger.connection.execute(
                        "INSERT OR IGNORE INTO card_source_commits VALUES (?,?,?,?,?)",
                        (card_id, sha, str(body.binding_id), "confirmed",
                         datetime.now(UTC).isoformat()),
                    )
                ledger.connection.execute(
                    "UPDATE knowledge_cards SET source_link_status='linked' WHERE card_id=?",
                    (card_id,),
                )
            return JSONResponse({"card_id": card_id, "commit_shas": shas})
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_card_sources", status=422)


async def growth_local_recommendation_decisions(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _RecommendationDecisionInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        with _ledger(serve_state(request)) as ledger:
            result = apply_decisions(
                ledger, str(body.request_id),
                confirm_groups=[group.model_dump(mode="json") for group in body.confirm_groups],
                defer_ids=[str(item) for item in body.defer_ids],
                ignore_ids=[str(item) for item in body.ignore_ids],
            )
    except (ValueError, sqlite3.IntegrityError):
        return error_response(ErrorCode.INPUT_VALIDATION,
                              "growth_recommendation_decision_failed", status=409)
    return JSONResponse(result)


async def growth_local_cards_apkg(request: Request) -> Response:
    require_write_token(request)
    raw_project_id = request.query_params.get("project_id")
    try:
        project_id = str(uuid.UUID(raw_project_id)) if raw_project_id else None
        with _ledger(serve_state(request)) as ledger:
            if project_id and ledger.connection.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (project_id,),
            ).fetchone() is None:
                raise ValueError("项目不存在")
            content = export_knowledge_cards_apkg(ledger, project_id)
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION,
                              "growth_cards_export_unavailable", status=422)
    return Response(
        content, media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="growth-cards.apkg"'},
    )


async def growth_local_explore_preview(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ExploreInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        preview, _, _, _ = await to_thread.run_sync(
            _local_explore_preview, serve_state(request),
            str(request.path_params["project_id"]), body,
        )
    except ValueError as exc:
        return _failure(exc)
    return JSONResponse({"payload_text": preview.payload_text,
                         "approval_hash": preview.approval_hash,
                         "provider_host": preview.provider_host,
                         "model_name": preview.model_name})


async def growth_local_explore_generate(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ExploreGenerateInput)
    if isinstance(body, JSONResponse):
        return body
    state = serve_state(request)
    lock = state.write_lock
    assert lock is not None
    try:
        async with lock:
            result = await to_thread.run_sync(
                _local_explore_generate, state, str(request.path_params["project_id"]), body,
            )
    except ValueError as exc:
        return _failure(exc)
    return JSONResponse(result)


async def growth_local_explore_topic(request: Request) -> JSONResponse:
    require_write_token(request)
    try:
        result = await to_thread.run_sync(
            _local_explore_topic, serve_state(request), str(request.path_params["project_id"]),
            str(request.path_params["session_id"]),
        )
    except ValueError as exc:
        return _failure(exc)
    return JSONResponse(result)


async def growth_local_materials_preview(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _MaterialsInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        preview = await to_thread.run_sync(
            _materials_preview, serve_state(request), str(request.path_params["task_id"]), body,
        )
    except ValueError as exc:
        return _failure(exc)
    approval_hash = hashlib.sha256((
        preview.selected_patch + "\n" + preview.provider_config_hash
    ).encode()).hexdigest()
    return JSONResponse({"patch_text": apply_redactions(
        preview.selected_patch, scan_text_for_secrets(
            preview.selected_patch, source_name="growth_material_patch")),
        "approval_hash": approval_hash, "provider_host": preview.provider_host,
        "model_name": preview.model_name})


async def growth_local_materials_generate(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _MaterialsGenerateInput)
    if isinstance(body, JSONResponse):
        return body
    state = serve_state(request)
    task_id = str(request.path_params["task_id"])
    try:
        preview = await to_thread.run_sync(_materials_preview, state, task_id, body)
        approval_hash = hashlib.sha256((
            preview.selected_patch + "\n" + preview.provider_config_hash
        ).encode()).hexdigest()
        if not body.approved or approval_hash != body.approved_payload_hash:
            raise ValueError("完整材料的源码范围尚未确认或已变化")
        with _ledger(state) as ledger, ledger.connection:
            existing = ledger.connection.execute(
                "SELECT status, run_id FROM growth_task_materials WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing and existing["status"] in {"queued", "running", "ready"}:
                return JSONResponse({"status": existing["status"], "run_id": existing["run_id"],
                                     "reused": True})
            ledger.connection.execute(
                "INSERT INTO growth_task_materials VALUES (?, NULL, 'queued', NULL, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET status='queued', error=NULL",
                (task_id, datetime.now(UTC).isoformat()),
            )
    except ValueError as exc:
        return _failure(exc)
    runner = state.task_runner
    if runner is None:
        return error_response(ErrorCode.REQUEST_TIMEOUT, "task_runner_unavailable", status=503)

    async def work(_handle: Any) -> dict[str, str]:
        with _ledger(state) as ledger, ledger.connection:
            ledger.connection.execute(
                "UPDATE growth_task_materials SET status='running' WHERE task_id=?",
                (task_id,),
            )
        return await to_thread.run_sync(_run_materials, state, task_id, body)

    job_id = runner.submit_if_capacity(
        "growth_materials", work, max_pending=2, thread_backed=True, redact_errors=True,
    )
    if job_id is None:
        with _ledger(state) as ledger, ledger.connection:
            ledger.connection.execute(
                "UPDATE growth_task_materials SET status='failed', error='queue full' "
                "WHERE task_id=?", (task_id,),
            )
        return error_response(ErrorCode.LOCK_CONFLICT, "too_many_growth_tasks", status=409)
    return JSONResponse({"task_id": job_id, "status": "queued"}, status_code=202)


async def growth_local_materials_read(request: Request) -> JSONResponse:
    require_write_token(request)
    try:
        result = await to_thread.run_sync(
            _materials_read, serve_state(request), str(request.path_params["task_id"]),
        )
    except (ValueError, OSError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_materials_unavailable", status=409)
    return JSONResponse(result)


async def growth_local_materials_reveal(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _MaterialAnswerInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _material_reveal, serve_state(request), str(request.path_params["task_id"]),
            str(request.path_params["question_id"]), body.answer_text,
        )
    except (ValueError, StopIteration):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_material_question_invalid", status=409)
    return JSONResponse(result)


async def growth_local_materials_rate(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _MaterialRatingInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _material_rate, serve_state(request), str(request.path_params["task_id"]),
            str(request.path_params["question_id"]), body,
        )
    except (ValueError, OSError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_material_rating_invalid", status=409)
    return JSONResponse(result)


async def growth_local_untracked(request: Request) -> JSONResponse:
    require_write_token(request)
    try:
        result = await to_thread.run_sync(
            _untracked, serve_state(request), str(request.path_params["binding_id"])
        )
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        return _failure(exc)
    return JSONResponse(result)


async def growth_local_model_sources(request: Request) -> JSONResponse:
    require_write_token(request)
    try:
        binding_id = str(uuid.UUID(request.query_params["binding_id"]))
        result = await to_thread.run_sync(
            _model_sources, serve_state(request),
            str(request.path_params["snapshot_id"]), binding_id,
        )
    except (KeyError, ValueError, subprocess.CalledProcessError):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_sources_unavailable", status=422
        )
    return JSONResponse(result)


async def growth_local_code_index(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _CodeIndexInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _code_index, serve_state(request),
            str(request.path_params["snapshot_id"]), body,
        )
    except RuntimeError as exc:
        if str(exc) == "snapshot_stale":
            return error_response(
                ErrorCode.INPUT_VALIDATION, "growth_snapshot_stale", status=409
            )
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_code_index_unavailable", status=503
        )
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_code_index_unavailable", status=422
        )
    return JSONResponse(result)


async def growth_local_model_preview(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _ModelPreviewInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _model_preview, serve_state(request),
            str(request.path_params["snapshot_id"]), body,
        )
    except Exception:
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_model_preview_unavailable", status=422
        )
    return JSONResponse(result)


async def growth_local_analysis(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _AnalysisInput)
    if isinstance(body, JSONResponse):
        return body
    if (body.account_id is None) != (body.event_id is None):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=422)
    if body.event_id is not None and body.mode != "live":
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=422)
    if body.mode == "live":
        if (
            not body.approved or not body.provider_name or not body.source_ref_ids
            or not body.approved_payload_hash
        ):
            return error_response(
                ErrorCode.INPUT_VALIDATION, "growth_model_approval_required", status=422
            )
    elif (
        body.approved or body.provider_name is not None
        or body.source_ref_ids is not None or body.approved_payload_hash is not None
    ):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_invalid_input", status=422
        )
    state = serve_state(request)
    snapshot_id = str(request.path_params["snapshot_id"])
    if body.event_id is not None:
        try:
            previous = await to_thread.run_sync(
                _existing_event_analysis, state, snapshot_id, body,
            )
        except ValueError:
            return error_response(
                ErrorCode.INPUT_VALIDATION, "growth_event_capture_not_ready", status=409
            )
        if previous is not None:
            return JSONResponse(previous)
    try:
        await to_thread.run_sync(
            _analysis_precheck, state, snapshot_id, str(body.binding_id)
        )
    except ValueError:
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_analysis_not_ready", status=409
        )
    if body.mode == "live":
        assert body.provider_name is not None
        assert body.source_ref_ids is not None
        assert body.approved_payload_hash is not None
        try:
            preview = await to_thread.run_sync(
                _model_preview, state, snapshot_id,
                _ModelPreviewInput(
                    binding_id=body.binding_id,
                    provider_name=body.provider_name,
                    source_ref_ids=body.source_ref_ids,
                ),
            )
        except Exception:
            return error_response(
                ErrorCode.INPUT_VALIDATION, "growth_model_preview_unavailable", status=409
            )
        if preview["approval_hash"] != body.approved_payload_hash:
            return error_response(
                ErrorCode.INPUT_VALIDATION, "growth_model_approval_expired", status=409
            )
    runner = state.task_runner
    if runner is None:
        return error_response(ErrorCode.REQUEST_TIMEOUT, "task_runner_unavailable", status=503)
    try:
        if body.event_id is not None:
            lock = state.write_lock
            assert lock is not None
            async with lock:
                queued = await to_thread.run_sync(_queue_analysis, state, snapshot_id, body)
        else:
            queued = await to_thread.run_sync(_queue_analysis, state, snapshot_id, body)
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError, sqlite3.IntegrityError):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_analysis_not_ready", status=409
        )
    analysis_id = str(queued["analysis_id"])
    if not queued["created"]:
        return JSONResponse({"analysis_id": analysis_id, "status": queued["status"],
                             "reused": True})

    async def work(_handle: Any) -> dict[str, Any]:
        result = await to_thread.run_sync(
            _run_analysis, state, analysis_id, str(body.binding_id)
        )
        return {**result, "run_id": result["upstream_run_id"]}

    task_id = runner.submit_if_capacity(
        "growth_analysis", work, max_pending=4,
        thread_backed=True, redact_errors=True,
    )
    if task_id is None:
        await to_thread.run_sync(_reject_analysis, state, analysis_id)
        return error_response(ErrorCode.LOCK_CONFLICT, "too_many_growth_tasks", status=409)
    return JSONResponse({"task_id": task_id, "analysis_id": analysis_id}, status_code=202)


async def growth_local_analysis_retry(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _AnalysisRetryInput)
    if isinstance(body, JSONResponse):
        return body
    state = serve_state(request)
    analysis_id = str(request.path_params["analysis_id"])
    try:
        await to_thread.run_sync(
            lambda: _analysis_precheck(
                state, "", str(body.binding_id), retry_id=analysis_id
            )
        )
    except ValueError:
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_analysis_not_retryable", status=409
        )
    runner = state.task_runner
    if runner is None:
        return error_response(ErrorCode.REQUEST_TIMEOUT, "task_runner_unavailable", status=503)
    try:
        await to_thread.run_sync(_queue_retry_analysis, state, analysis_id, body)
    except ValueError:
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_analysis_not_retryable", status=409
        )

    async def work(_handle: Any) -> dict[str, Any]:
        result = await to_thread.run_sync(
            _run_analysis, state, analysis_id, str(body.binding_id)
        )
        return {**result, "run_id": result["upstream_run_id"]}

    task_id = runner.submit_if_capacity(
        "growth_analysis", work, max_pending=4,
        thread_backed=True, redact_errors=True,
    )
    if task_id is None:
        await to_thread.run_sync(_reject_analysis, state, analysis_id)
        return error_response(ErrorCode.LOCK_CONFLICT, "too_many_growth_tasks", status=409)
    return JSONResponse({"task_id": task_id, "analysis_id": analysis_id}, status_code=202)


async def growth_local_topic_decision(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _TopicDecisionInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _decide_topic, serve_state(request),
            str(request.path_params["proposal_id"]), body,
        )
    except (ValueError, sqlite3.IntegrityError):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_topic_decision_failed", status=409
        )
    return JSONResponse(result)


async def growth_local_task_create(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _TaskCreateInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _create_learning_task, serve_state(request),
            str(request.path_params["opportunity_id"]), body,
        )
    except (ValueError, sqlite3.IntegrityError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_task_create_failed", status=409)
    return JSONResponse(result, status_code=201)


async def growth_local_task_chain(request: Request) -> JSONResponse:
    require_write_token(request)
    try:
        result = await to_thread.run_sync(
            _task_chain, serve_state(request), str(request.path_params["task_id"]),
        )
    except ValueError:
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_task_missing", status=404)
    return JSONResponse(result)


async def growth_local_answer(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _AnswerInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _submit_answer, serve_state(request), str(request.path_params["task_id"]), body,
        )
    except (ValueError, sqlite3.IntegrityError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_answer_failed", status=409)
    return JSONResponse(result, status_code=201)


async def growth_local_hint(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _HintInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _request_hint, serve_state(request), str(request.path_params["task_id"]), body,
        )
    except (ValueError, sqlite3.IntegrityError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_hint_failed", status=409)
    return JSONResponse(result, status_code=201)


async def growth_local_note(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _NoteInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _save_note, serve_state(request), str(request.path_params["task_id"]), body,
        )
    except (ValueError, sqlite3.IntegrityError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_note_failed", status=409)
    return JSONResponse(result, status_code=201)


async def growth_local_task_progress(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _TaskProgressInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _set_task_progress, serve_state(request),
            str(request.path_params["task_id"]), body,
        )
    except (ValueError, sqlite3.IntegrityError):
        return error_response(ErrorCode.INPUT_VALIDATION, "growth_progress_failed", status=409)
    return JSONResponse(result)


async def growth_local_feedback_preview(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _FeedbackPreviewInput)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await to_thread.run_sync(
            _feedback_preview, serve_state(request),
            str(request.path_params["attempt_id"]), str(body.binding_id),
        )
    except (ValueError, FileNotFoundError):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_feedback_preview_failed", status=409
        )
    return JSONResponse(result)


async def growth_local_feedback_submit(request: Request) -> JSONResponse:
    require_write_token(request)
    body = await _parse(request, _FeedbackSubmitInput)
    if isinstance(body, JSONResponse):
        return body
    if not body.approved:
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_model_approval_required", status=422
        )
    state = serve_state(request)
    attempt_id = str(request.path_params["attempt_id"])
    runner = state.task_runner
    if runner is None:
        return error_response(ErrorCode.REQUEST_TIMEOUT, "task_runner_unavailable", status=503)
    try:
        queued = await to_thread.run_sync(_queue_feedback, state, attempt_id, body)
    except (ValueError, sqlite3.IntegrityError):
        return error_response(
            ErrorCode.INPUT_VALIDATION, "growth_feedback_not_ready", status=409
        )
    if not queued["created"]:
        return JSONResponse({"attempt_id": attempt_id, "status": queued["status"],
                             "reused": True})

    async def work(_handle: Any) -> dict[str, str]:
        return await to_thread.run_sync(_run_feedback, state, attempt_id)

    task_id = runner.submit_if_capacity(
        "growth_feedback", work, max_pending=4,
        thread_backed=True, redact_errors=True,
    )
    if task_id is None:
        await to_thread.run_sync(_reject_feedback, state, attempt_id)
        return error_response(ErrorCode.LOCK_CONFLICT, "too_many_growth_tasks", status=409)
    return JSONResponse({"task_id": task_id, "attempt_id": attempt_id}, status_code=202)
