from __future__ import annotations

import contextlib
import logging
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from ahadiff.contracts import AuthTokenResponse, ErrorCode
from ahadiff.core.config import apply_global_env_file, apply_repo_env_file
from ahadiff.core.errors import AhaDiffError, InputError
from ahadiff.growth.local import GrowthLocalRepository

from ._errors import error_response
from .auth import require_token_bootstrap_request, serve_state
from .middleware import LoopbackGuardMiddleware, RequestTimeoutMiddleware, WriteRateLimitMiddleware
from .routes_audit import get_audit
from .routes_capture import get_capture_recommended
from .routes_challenge import (
    get_challenge,
    get_challenge_feedback,
    post_challenge_abort,
    post_challenge_advance,
    post_challenge_build,
    post_challenge_review,
)
from .routes_config import get_config, get_doctor, put_config
from .routes_db import post_db_check
from .routes_demo import get_demo_learn_preview
from .routes_export import get_export_apkg, get_export_results, post_export_preview
from .routes_graph import get_concept_graph, get_graph_status, post_graph_refresh
from .routes_growth import (
    growth_local_analysis,
    growth_local_analysis_retry,
    growth_local_answer,
    growth_local_binding,
    growth_local_capture,
    growth_local_commits,
    growth_local_authors_group,
    growth_local_card_commits,
    growth_local_cards_apkg,
    growth_local_recommendation_decisions,
    growth_local_explore_generate,
    growth_local_explore_preview,
    growth_local_explore_topic,
    growth_local_materials_generate,
    growth_local_materials_preview,
    growth_local_materials_read,
    growth_local_materials_reveal,
    growth_local_materials_rate,
    growth_local_cloud_export,
    growth_local_code_index,
    growth_local_feature,
    growth_local_feedback_preview,
    growth_local_feedback_submit,
    growth_local_hint,
    growth_local_import,
    growth_local_list,
    growth_local_model_preview,
    growth_local_model_sources,
    growth_local_note,
    growth_local_project,
    growth_local_project_policy,
    growth_local_source_excerpt_preview,
    growth_local_source_excerpt_queue,
    growth_local_sync,
    growth_local_sync_event_capture,
    growth_local_sync_event_inbox,
    growth_local_sync_note_delete,
    growth_local_sync_note_resolve,
    growth_local_sync_note_revision,
    growth_local_sync_preview,
    growth_local_sync_binding,
    growth_local_sync_source_locations,
    growth_local_sync_task_answer,
    growth_local_sync_task_draft,
    growth_local_sync_task_progress,
    growth_local_sync_task_rebase,
    growth_local_sync_workspace,
    growth_local_task_chain,
    growth_local_task_create,
    growth_local_task_progress,
    growth_local_topic_decision,
    growth_local_untracked,
)
from .routes_growth_chat import (
    growth_chat_decision,
    growth_chat_generate,
    growth_chat_list,
    growth_chat_message,
    growth_chat_preview,
    growth_chat_read,
    growth_chat_start,
    growth_synced_feedback_preview,
    growth_synced_feedback_submit,
    growth_synced_task_journey,
)
from .routes_growth_knowledge import (
    growth_knowledge_aggregate,
    growth_knowledge_answer,
    growth_knowledge_card,
    growth_knowledge_chat,
    growth_knowledge_chat_stream,
    growth_knowledge_complete,
    growth_knowledge_commit,
    growth_knowledge_decision,
    growth_knowledge_discussion,
    growth_knowledge_explanation_confirm,
    growth_knowledge_flip,
    growth_knowledge_learning,
    growth_knowledge_mark,
    growth_knowledge_material_generate,
    growth_knowledge_material_preview,
    growth_knowledge_revise,
    growth_knowledge_split,
    growth_knowledge_sync_conflict_resolve,
    growth_knowledge_sync_conflicts,
    growth_knowledge_note,
    growth_knowledge_vault,
    growth_knowledge_wiki_articles,
    growth_knowledge_wiki_draft,
    growth_knowledge_wiki_edit,
    growth_knowledge_wiki_publish,
    growth_knowledge_workspace,
)
from .routes_growth_modules import growth_module_edit, growth_module_list, growth_module_map
from .routes_growth_reviews import (
    growth_local_review_due, growth_local_review_submit,
    growth_review_due, growth_review_submit,
)
from .routes_growth_topics import (
    growth_topic_create,
    growth_topic_list,
    growth_topic_merge,
    growth_topic_note_create,
    growth_topic_read,
    growth_topic_split,
    growth_topic_update,
)
from .routes_improve import get_improve_preflight
from .routes_install import (
    get_install_targets,
    install_target,
    preview_install_target,
    uninstall_target,
)
from .routes_learn import post_learn, post_learn_estimate
from .routes_locale import get_locale, put_locale
from .routes_providers import (
    create_provider,
    delete_provider,
    discover_models,
    fetch_provider_models,
    get_provider_model_limits,
    preview_provider_model_limits,
    probe_provider_route,
    save_provider_models,
    update_provider,
)
from .routes_quiz import get_quiz_questions, reveal_quiz_question
from .routes_review import (
    get_review_mastery,
    get_review_queue,
    get_weak_concepts,
    post_review_queue_state,
    post_review_rate,
)
from .routes_runs import (
    get_anchors,
    get_claims,
    get_concepts,
    get_concepts_ledger,
    get_diff,
    get_distractor_gate,
    get_document,
    get_graphify_signoff,
    get_judge,
    get_judge_failure,
    get_lesson,
    get_misconceptions,
    get_quiz,
    get_ratchet_history,
    get_ratchet_transparency,
    get_review_context,
    get_run,
    get_run_concepts,
    get_score,
    get_spec_alignment_artifact,
    list_runs,
)
from .routes_search import search_api
from .routes_signals import helpfulness, mark_wrong, quiz_answer, srs_review
from .routes_snapshots import delete_snapshot_route, get_snapshot, get_snapshots, post_snapshot
from .routes_stats import (
    get_learning_effectiveness,
    get_providers,
    get_review_heatmap,
    get_serve_status,
    get_spec_alignment,
    get_stats,
    get_usage,
)
from .routes_tasks import cancel_task, get_task, list_tasks, task_progress_sse
from .routes_watch import get_watch_status
from .static import mount_viewer_static

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from starlette.requests import Request

    from .state import ServeState


_log = logging.getLogger(__name__)

_HTTP_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.INPUT_BAD_FIELD,
    401: ErrorCode.AUTH_REQUIRED,
    403: ErrorCode.LOOPBACK_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.INPUT_BAD_FIELD,
    408: ErrorCode.REQUEST_TIMEOUT,
    413: ErrorCode.RUN_ARTIFACT_TOO_LARGE,
    415: ErrorCode.INPUT_BAD_FIELD,
    422: ErrorCode.INPUT_VALIDATION,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    501: ErrorCode.FEATURE_UNAVAILABLE,
    502: ErrorCode.PROVIDER_TRANSPORT,
    503: ErrorCode.REQUEST_TIMEOUT,
    504: ErrorCode.REQUEST_TIMEOUT,
}

_GENERIC_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INTERNAL_ERROR: "internal_error",
    ErrorCode.STORAGE_REVIEW_DB: "review_database_unavailable",
    ErrorCode.STORAGE_USAGE_DB: "usage_database_unavailable",
    ErrorCode.STORAGE_FS: "local_storage_unavailable",
    ErrorCode.PROVIDER_TRANSPORT: "provider_transport_error",
    ErrorCode.PROVIDER_HTTP: "provider_http_error",
}


def create_app(state: ServeState, *, viewer_dist: Path | None = None) -> Starlette:
    runtime_state = state.with_runtime_lock()
    apply_global_env_file(config_dir=runtime_state.global_config_root)
    apply_repo_env_file(runtime_state.state_dir / ".env")

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncGenerator[None]:
        with GrowthLocalRepository(runtime_state.state_dir / "growth.sqlite") as ledger:
            ledger.recover_interrupted_analyses()
            ledger.recover_interrupted_feedback()
        yield
        runner = getattr(runtime_state, "task_runner", None)
        if runner is not None:
            try:
                await runner.shutdown(timeout=5.0)
            except Exception:
                _log.debug("task runner shutdown error", exc_info=True)
        watcher = getattr(runtime_state, "file_watcher", None)
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                _log.debug("file watcher stop error", exc_info=True)

    app = Starlette(
        debug=False,
        lifespan=_lifespan,
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/api/auth/token", auth_token, methods=["GET", "POST"]),
            Route("/api/growth/local", growth_local_list, methods=["GET"]),
            Route("/api/growth/local/knowledge", growth_knowledge_workspace, methods=["GET"]),
            Route("/api/growth/local/knowledge/sync/conflicts",
                  growth_knowledge_sync_conflicts, methods=["GET"]),
            Route("/api/growth/local/knowledge/sync/conflicts/{card_id:uuid}/resolve",
                  growth_knowledge_sync_conflict_resolve, methods=["POST"]),
            Route("/api/growth/local/knowledge/commits/{commit_sha:str}",
                  growth_knowledge_commit, methods=["GET"]),
            Route("/api/growth/local/knowledge/aggregate", growth_knowledge_aggregate,
                  methods=["POST"]),
            Route("/api/growth/local/knowledge/candidates/{candidate_id:uuid}/decision",
                  growth_knowledge_decision, methods=["POST"]),
            Route("/api/growth/local/knowledge/candidates/{candidate_id:uuid}/split",
                  growth_knowledge_split, methods=["POST"]),
            Route("/api/growth/local/knowledge/candidates/{candidate_id:uuid}/discussion",
                  growth_knowledge_discussion, methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}",
                  growth_knowledge_card, methods=["GET"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/materials/preview",
                  growth_knowledge_material_preview, methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/materials",
                  growth_knowledge_material_generate, methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/revision",
                  growth_knowledge_revise, methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/learning",
                  growth_knowledge_learning, methods=["PUT"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/flip",
                  growth_knowledge_flip, methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/answers",
                  growth_knowledge_answer, methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/explanations/"
                  "{question_id:uuid}/confirm", growth_knowledge_explanation_confirm,
                  methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/complete",
                  growth_knowledge_complete, methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/mark",
                  growth_knowledge_mark, methods=["PUT"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/note",
                  growth_knowledge_note, methods=["PUT"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/chat",
                  growth_knowledge_chat, methods=["POST"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/chat/stream",
                  growth_knowledge_chat_stream, methods=["POST"]),
            Route("/api/growth/local/knowledge/vault", growth_knowledge_vault,
                  methods=["GET", "PUT"]),
            Route("/api/growth/local/knowledge/wiki", growth_knowledge_wiki_articles,
                  methods=["GET"]),
            Route("/api/growth/local/knowledge/cards/{card_id:uuid}/wiki/draft",
                  growth_knowledge_wiki_draft, methods=["POST"]),
            Route("/api/growth/local/knowledge/wiki/drafts/{draft_id:uuid}",
                  growth_knowledge_wiki_edit, methods=["PUT"]),
            Route("/api/growth/local/knowledge/wiki/drafts/{draft_id:uuid}/publish",
                  growth_knowledge_wiki_publish, methods=["POST"]),
            Route("/api/growth/local/sync", growth_local_sync, methods=["POST"]),
            Route("/api/growth/local/sync/preview", growth_local_sync_preview, methods=["POST"]),
            Route("/api/growth/local/source-refs/{source_ref_id:uuid}/excerpt/preview",
                  growth_local_source_excerpt_preview, methods=["POST"]),
            Route("/api/growth/local/source-refs/{source_ref_id:uuid}/excerpt",
                  growth_local_source_excerpt_queue, methods=["POST"]),
            Route("/api/growth/local/sync/workspace", growth_local_sync_workspace, methods=["GET"]),
            Route("/api/growth/local/sync/tasks/{task_id:uuid}/source-locations",
                  growth_local_sync_source_locations, methods=["GET"]),
            Route("/api/growth/local/sync/tasks/{task_id:uuid}/binding",
                  growth_local_sync_binding, methods=["POST"]),
            Route("/api/growth/local/cloud/tasks/{task_id:uuid}/journey",
                  growth_synced_task_journey, methods=["POST"]),
            Route("/api/growth/local/cloud/exports", growth_local_cloud_export,
                  methods=["POST"]),
            Route("/api/growth/local/cloud/reviews/due", growth_review_due,
                  methods=["POST"]),
            Route("/api/growth/local/reviews/due", growth_local_review_due,
                  methods=["GET"]),
            Route("/api/growth/local/cards/{card_id:uuid}/reviews",
                  growth_local_review_submit, methods=["POST"]),
            Route("/api/growth/local/cloud/tasks/{task_id:uuid}/reviews",
                  growth_review_submit, methods=["POST"]),
            Route("/api/growth/local/cloud/chats", growth_chat_list, methods=["POST"]),
            Route("/api/growth/local/cloud/chats/start", growth_chat_start,
                  methods=["POST"]),
            Route("/api/growth/local/cloud/chats/{session_id:uuid}", growth_chat_read,
                  methods=["POST"]),
            Route("/api/growth/local/cloud/chats/{session_id:uuid}/messages",
                  growth_chat_message, methods=["POST"]),
            Route("/api/growth/local/cloud/chats/{session_id:uuid}/preview",
                  growth_chat_preview, methods=["POST"]),
            Route("/api/growth/local/cloud/chats/{session_id:uuid}/generate",
                  growth_chat_generate, methods=["POST"]),
            Route("/api/growth/local/cloud/chats/{session_id:uuid}/suggestions/"
                  "{suggestion_id:uuid}/decision", growth_chat_decision,
                  methods=["POST"]),
            Route("/api/growth/local/cloud/attempts/{attempt_id:uuid}/feedback/preview",
                  growth_synced_feedback_preview, methods=["POST"]),
            Route("/api/growth/local/cloud/attempts/{attempt_id:uuid}/feedback",
                  growth_synced_feedback_submit, methods=["POST"]),
            Route("/api/growth/local/cloud/topics", growth_topic_list,
                  methods=["POST"]),
            Route("/api/growth/local/cloud/topics/create", growth_topic_create,
                  methods=["POST"]),
            Route("/api/growth/local/cloud/topics/{topic_id:uuid}",
                  growth_topic_read, methods=["POST"]),
            Route("/api/growth/local/cloud/topics/{topic_id:uuid}/notes",
                  growth_topic_note_create, methods=["POST"]),
            Route("/api/growth/local/cloud/topics/{topic_id:uuid}/update",
                  growth_topic_update, methods=["POST"]),
            Route("/api/growth/local/cloud/topics/{topic_id:uuid}/merge",
                  growth_topic_merge, methods=["POST"]),
            Route("/api/growth/local/cloud/topics/{topic_id:uuid}/split",
                  growth_topic_split, methods=["POST"]),
            Route("/api/growth/local/cloud/projects/{project_id:uuid}/modules",
                  growth_module_list, methods=["POST"]),
            Route("/api/growth/local/cloud/modules/map",
                  growth_module_map, methods=["POST"]),
            Route("/api/growth/local/cloud/modules/{module_id:uuid}/update",
                  growth_module_edit, methods=["POST"]),
            Route("/api/growth/local/sync/events", growth_local_sync_event_inbox,
                  methods=["GET"]),
            Route("/api/growth/local/sync/events/{event_id:uuid}/capture",
                  growth_local_sync_event_capture, methods=["POST"]),
            Route(
                "/api/growth/local/sync/notes/{note_id:uuid}/revisions",
                growth_local_sync_note_revision, methods=["POST"],
            ),
            Route(
                "/api/growth/local/sync/notes/{note_id:uuid}/resolve",
                growth_local_sync_note_resolve, methods=["POST"],
            ),
            Route(
                "/api/growth/local/sync/notes/{note_id:uuid}",
                growth_local_sync_note_delete, methods=["DELETE"],
            ),
            Route(
                "/api/growth/local/sync/tasks/{task_id:uuid}/draft",
                growth_local_sync_task_draft, methods=["POST"],
            ),
            Route(
                "/api/growth/local/sync/tasks/{task_id:uuid}/rebase",
                growth_local_sync_task_rebase, methods=["POST"],
            ),
            Route(
                "/api/growth/local/sync/tasks/{task_id:uuid}/attempts",
                growth_local_sync_task_answer, methods=["POST"],
            ),
            Route(
                "/api/growth/local/sync/tasks/{task_id:uuid}/progress",
                growth_local_sync_task_progress, methods=["POST"],
            ),
            Route("/api/growth/local/projects", growth_local_project, methods=["POST"]),
            Route("/api/growth/local/import", growth_local_import, methods=["POST"]),
            Route(
                "/api/growth/local/projects/{project_id:uuid}/policy",
                growth_local_project_policy, methods=["PUT"],
            ),
            Route(
                "/api/growth/local/projects/{project_id:uuid}/bindings",
                growth_local_binding, methods=["POST"],
            ),
            Route(
                "/api/growth/local/projects/{project_id:uuid}/features",
                growth_local_feature, methods=["POST"],
            ),
            Route(
                "/api/growth/local/features/{feature_id:uuid}/snapshots",
                growth_local_capture, methods=["POST"],
            ),
            Route(
                "/api/growth/local/bindings/{binding_id:uuid}/commits",
                growth_local_commits, methods=["GET"],
            ),
            Route(
                "/api/growth/local/bindings/{binding_id:uuid}/authors/group",
                growth_local_authors_group, methods=["POST"],
            ),
            Route(
                "/api/growth/local/cards/{card_id:uuid}/commits",
                growth_local_card_commits, methods=["POST"],
            ),
            Route("/api/growth/local/cards.apkg", growth_local_cards_apkg,
                  methods=["GET"]),
            Route(
                "/api/growth/local/recommendations/decisions",
                growth_local_recommendation_decisions, methods=["POST"],
            ),
            Route(
                "/api/growth/local/projects/{project_id:uuid}/explore/preview",
                growth_local_explore_preview, methods=["POST"],
            ),
            Route(
                "/api/growth/local/projects/{project_id:uuid}/explore",
                growth_local_explore_generate, methods=["POST"],
            ),
            Route(
                "/api/growth/local/projects/{project_id:uuid}/explore/{session_id:uuid}/topic",
                growth_local_explore_topic, methods=["POST"],
            ),
            Route("/api/growth/local/tasks/{task_id:uuid}/materials/preview",
                  growth_local_materials_preview, methods=["POST"]),
            Route("/api/growth/local/tasks/{task_id:uuid}/materials",
                  growth_local_materials_generate, methods=["POST"]),
            Route("/api/growth/local/tasks/{task_id:uuid}/materials",
                  growth_local_materials_read, methods=["GET"]),
            Route("/api/growth/local/tasks/{task_id:uuid}/materials/quiz/{question_id}/reveal",
                  growth_local_materials_reveal, methods=["POST"]),
            Route("/api/growth/local/tasks/{task_id:uuid}/materials/quiz/{question_id}/rate",
                  growth_local_materials_rate, methods=["POST"]),
            Route(
                "/api/growth/local/bindings/{binding_id:uuid}/untracked",
                growth_local_untracked, methods=["GET"],
            ),
            Route(
                "/api/growth/local/snapshots/{snapshot_id:uuid}/analysis",
                growth_local_analysis, methods=["POST"],
            ),
            Route(
                "/api/growth/local/snapshots/{snapshot_id:uuid}/model-sources",
                growth_local_model_sources, methods=["GET"],
            ),
            Route(
                "/api/growth/local/snapshots/{snapshot_id:uuid}/code-index",
                growth_local_code_index, methods=["POST"],
            ),
            Route(
                "/api/growth/local/snapshots/{snapshot_id:uuid}/model-preview",
                growth_local_model_preview, methods=["POST"],
            ),
            Route(
                "/api/growth/local/analyses/{analysis_id:uuid}/retry",
                growth_local_analysis_retry, methods=["POST"],
            ),
            Route(
                "/api/growth/local/topic-proposals/{proposal_id:uuid}/decision",
                growth_local_topic_decision, methods=["POST"],
            ),
            Route(
                "/api/growth/local/opportunities/{opportunity_id:uuid}/tasks",
                growth_local_task_create, methods=["POST"],
            ),
            Route(
                "/api/growth/local/tasks/{task_id:uuid}",
                growth_local_task_chain, methods=["GET"],
            ),
            Route(
                "/api/growth/local/tasks/{task_id:uuid}/answers",
                growth_local_answer, methods=["POST"],
            ),
            Route(
                "/api/growth/local/tasks/{task_id:uuid}/hints",
                growth_local_hint, methods=["POST"],
            ),
            Route(
                "/api/growth/local/tasks/{task_id:uuid}/notes",
                growth_local_note, methods=["POST"],
            ),
            Route(
                "/api/growth/local/tasks/{task_id:uuid}/progress",
                growth_local_task_progress, methods=["POST"],
            ),
            Route(
                "/api/growth/local/attempts/{attempt_id:uuid}/feedback-preview",
                growth_local_feedback_preview, methods=["POST"],
            ),
            Route(
                "/api/growth/local/attempts/{attempt_id:uuid}/feedback",
                growth_local_feedback_submit, methods=["POST"],
            ),
            Route("/api/locale", get_locale, methods=["GET"]),
            Route("/api/locale", put_locale, methods=["PUT"]),
            Route("/api/runs", list_runs, methods=["GET"]),
            Route("/api/run/{run_id}", get_run, methods=["GET"]),
            Route("/api/run/{run_id}/lesson", get_lesson, methods=["GET"]),
            Route("/api/run/{run_id}/claims", get_claims, methods=["GET"]),
            Route("/api/run/{run_id}/quiz", get_quiz, methods=["GET"]),
            Route("/api/run/{run_id}/quiz/questions", get_quiz_questions, methods=["GET"]),
            Route(
                "/api/run/{run_id}/quiz/{question_id}/reveal",
                reveal_quiz_question,
                methods=["POST"],
            ),
            Route("/api/run/{run_id}/misconceptions", get_misconceptions, methods=["GET"]),
            Route("/api/run/{run_id}/distractor-gate", get_distractor_gate, methods=["GET"]),
            Route("/api/run/{run_id}/diff", get_diff, methods=["GET"]),
            Route("/api/run/{run_id}/document", get_document, methods=["GET"]),
            Route("/api/run/{run_id}/anchors", get_anchors, methods=["GET"]),
            Route("/api/run/{run_id}/review-context", get_review_context, methods=["GET"]),
            Route("/api/snapshots", get_snapshots, methods=["GET"]),
            Route("/api/snapshots", post_snapshot, methods=["POST"]),
            Route("/api/snapshots/{snapshot_id}", get_snapshot, methods=["GET"]),
            Route("/api/snapshots/{snapshot_id}", delete_snapshot_route, methods=["DELETE"]),
            Route("/api/run/{run_id}/graphify-signoff", get_graphify_signoff, methods=["GET"]),
            Route("/api/run/{run_id}/score", get_score, methods=["GET"]),
            Route("/api/run/{run_id}/judge", get_judge, methods=["GET"]),
            Route("/api/run/{run_id}/judge-failure", get_judge_failure, methods=["GET"]),
            Route("/api/run/{run_id}/spec-alignment", get_spec_alignment_artifact, methods=["GET"]),
            Route("/api/run/{run_id}/concepts", get_run_concepts, methods=["GET"]),
            Route("/api/concepts/ledger", get_concepts_ledger, methods=["GET"]),
            Route("/api/concepts", get_concepts, methods=["GET"]),
            Route("/api/ratchet/history", get_ratchet_history, methods=["GET"]),
            Route("/api/ratchet/transparency", get_ratchet_transparency, methods=["GET"]),
            Route("/api/improve/preflight", get_improve_preflight, methods=["GET"]),
            Route("/api/review/queue", get_review_queue, methods=["GET"]),
            Route("/api/review/rate", post_review_rate, methods=["POST"]),
            Route("/api/review/queue-state", post_review_queue_state, methods=["POST"]),
            Route("/api/search", search_api, methods=["GET"]),
            Route("/api/concepts/weak", get_weak_concepts, methods=["GET"]),
            Route("/api/review/mastery", get_review_mastery, methods=["GET"]),
            Route("/api/usage", get_usage, methods=["GET"]),
            Route("/api/audit", get_audit, methods=["GET"]),
            Route("/api/spec/alignment", get_spec_alignment, methods=["GET"]),
            Route("/api/capture/recommended", get_capture_recommended, methods=["GET"]),
            Route("/api/config", get_config, methods=["GET"]),
            Route("/api/config", put_config, methods=["PUT"]),
            Route("/api/doctor", get_doctor, methods=["GET"]),
            Route("/api/install/targets", get_install_targets, methods=["GET"]),
            Route("/api/install/{target}/preview", preview_install_target, methods=["POST"]),
            Route("/api/install/{target}", install_target, methods=["POST"]),
            Route("/api/install/{target}/uninstall", uninstall_target, methods=["POST"]),
            Route("/api/stats", get_stats, methods=["GET"]),
            Route("/api/review/heatmap", get_review_heatmap, methods=["GET"]),
            Route("/api/export/results", get_export_results, methods=["GET"]),
            Route("/api/export/apkg", get_export_apkg, methods=["GET"]),
            Route("/api/export/preview", post_export_preview, methods=["POST"]),
            Route("/api/providers", get_providers, methods=["GET"]),
            Route("/api/providers", create_provider, methods=["POST"]),
            Route("/api/providers/discover-models", discover_models, methods=["POST"]),
            Route(
                "/api/providers/model-limits/preview",
                preview_provider_model_limits,
                methods=["POST"],
            ),
            Route(
                "/api/providers/{alias}/model-limits",
                get_provider_model_limits,
                methods=["GET"],
            ),
            Route("/api/providers/{alias}/probe", probe_provider_route, methods=["POST"]),
            Route("/api/providers/{alias}/models", fetch_provider_models, methods=["GET"]),
            Route("/api/providers/{alias}/models", save_provider_models, methods=["PUT"]),
            Route("/api/providers/{alias}", update_provider, methods=["PUT"]),
            Route("/api/providers/{alias}", delete_provider, methods=["DELETE"]),
            Route("/api/serve/status", get_serve_status, methods=["GET"]),
            Route("/api/stats/learning", get_learning_effectiveness, methods=["GET"]),
            Route("/api/signals/mark-wrong", mark_wrong, methods=["POST"]),
            Route("/api/signals/quiz-answer", quiz_answer, methods=["POST"]),
            Route("/api/signals/srs-review", srs_review, methods=["POST"]),
            Route("/api/signals/helpfulness", helpfulness, methods=["POST"]),
            Route("/api/graph/status", get_graph_status, methods=["GET"]),
            Route("/api/graph/concepts", get_concept_graph, methods=["GET"]),
            Route("/api/graph/refresh", post_graph_refresh, methods=["POST"]),
            Route("/api/db/check", post_db_check, methods=["POST"]),
            Route("/api/learn", post_learn, methods=["POST"]),
            Route("/api/learn/estimate", post_learn_estimate, methods=["POST"]),
            Route("/api/demo/learn-preview", get_demo_learn_preview, methods=["GET"]),
            Route("/api/tasks", list_tasks, methods=["GET"]),
            Route("/api/tasks/{task_id}", get_task, methods=["GET"]),
            Route("/api/tasks/{task_id}/cancel", cancel_task, methods=["POST"]),
            Route("/api/tasks/{task_id}/progress", task_progress_sse, methods=["GET"]),
            Route("/api/watch/status", get_watch_status, methods=["GET"]),
            Route("/api/challenge/build", post_challenge_build, methods=["POST"]),
            Route("/api/challenge/{challenge_id}", get_challenge, methods=["GET"]),
            Route(
                "/api/challenge/{challenge_id}/advance",
                post_challenge_advance,
                methods=["POST"],
            ),
            Route(
                "/api/challenge/{challenge_id}/abort",
                post_challenge_abort,
                methods=["POST"],
            ),
            Route(
                "/api/challenge/{challenge_id}/review",
                post_challenge_review,
                methods=["POST"],
            ),
            Route(
                "/api/challenge/{challenge_id}/feedback",
                get_challenge_feedback,
                methods=["GET"],
            ),
            Route(
                "/api/{rest_of_path:path}",
                api_not_found,
                methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
            ),
        ],
        exception_handlers={
            AhaDiffError: _handled_error,
            InputError: _handled_error,
            JSONDecodeError: _handled_error,
            PermissionError: _permission_error,
            ValidationError: _validation_error,
            HTTPException: _http_error,
        },
    )
    app.state.ahadiff = runtime_state
    app.add_middleware(WriteRateLimitMiddleware)
    app.add_middleware(LoopbackGuardMiddleware)
    app.add_middleware(RequestTimeoutMiddleware)
    mount_viewer_static(app, viewer_dist=viewer_dist)
    return app


async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def auth_token(request: Request) -> JSONResponse:
    # Every mutating route separately requires X-AhaDiff-Token so ambient browser state
    # or a discovered localhost port is not enough to perform writes against the repo DB.
    require_token_bootstrap_request(request)
    state = serve_state(request)
    return JSONResponse(AuthTokenResponse(token=state.token).model_dump(mode="json"))


async def api_not_found(request: Request) -> JSONResponse:
    return error_response(
        ErrorCode.NOT_FOUND,
        "not_found",
        details={"path": request.url.path},
    )


async def _handled_error(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, AhaDiffError):
        return error_response(
            exc.code,
            _public_error_message(exc.code, str(exc)),
            details=exc.details or None,
        )
    if isinstance(exc, JSONDecodeError):
        return error_response(ErrorCode.INPUT_INVALID_JSON, "invalid_json")
    return error_response(ErrorCode.INTERNAL_ERROR, _GENERIC_MESSAGES[ErrorCode.INTERNAL_ERROR])


async def _permission_error(_request: Request, exc: Exception) -> JSONResponse:
    del exc
    return error_response(ErrorCode.STORAGE_FS, _GENERIC_MESSAGES[ErrorCode.STORAGE_FS])


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, ValidationError):
        return error_response(
            ErrorCode.INPUT_VALIDATION,
            "validation_error",
            details={"errors": exc.errors(include_context=False, include_input=False)},
        )
    return error_response(ErrorCode.INPUT_VALIDATION, str(exc))


async def _http_error(_request: Request, exc: Exception) -> JSONResponse:
    status_code = exc.status_code if isinstance(exc, HTTPException) else 500
    detail: Any = exc.detail if isinstance(exc, HTTPException) else str(exc)
    code = _HTTP_TO_CODE.get(status_code, ErrorCode.INTERNAL_ERROR)
    message = detail if isinstance(detail, str) else str(detail)
    return error_response(code, _public_error_message(code, message), status=status_code)


def _public_error_message(code: ErrorCode, message: str) -> str:
    if code in _GENERIC_MESSAGES:
        return _GENERIC_MESSAGES[code]
    if code is ErrorCode.LOCK_CONFLICT:
        return "another_ahadiff_process_is_running"
    return message or code.value.lower()


__all__ = ["create_app"]
