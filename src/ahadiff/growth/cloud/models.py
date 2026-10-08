"""Strict public request contracts; raw source and device paths are excluded."""

from __future__ import annotations

import uuid  # noqa: TC003 - Pydantic resolves UUID at runtime.
from datetime import datetime  # noqa: TC003 - Pydantic resolves datetime at runtime.
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA40 = r"^[0-9a-f]{40}$"
SHA64 = r"^[0-9a-f]{64}$"
SHA40_OR_64 = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationModel(StrictModel):
    operation_id: uuid.UUID


class SyncOperationItem(StrictModel):
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]


class SyncOperationsBatch(StrictModel):
    operations: list[SyncOperationItem] = Field(min_length=1, max_length=100)


class KnowledgeBundleUpsert(OperationModel):
    base_revision: int = Field(ge=0)
    project_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    bundle: dict[str, Any]

    @model_validator(mode="after")
    def projects_unique(self) -> KnowledgeBundleUpsert:
        if len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError("项目 ID 不能重复")
        return self


class DeviceRegistration(OperationModel):
    device_id: uuid.UUID


class DeviceCursorAck(OperationModel):
    last_change_seq: int = Field(ge=0)


class DeviceRevocation(OperationModel):
    pass


class ProjectPolicy(StrictModel):
    cloud_allowed: bool = True
    model_allowed: bool = False


class ProjectCreate(OperationModel):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    sync_policy: ProjectPolicy = Field(default_factory=ProjectPolicy)


class FeatureCreate(OperationModel):
    feature_id: uuid.UUID
    project_id: uuid.UUID
    label: str = Field(min_length=1, max_length=200)
    base_ref: str = Field(min_length=1, max_length=200)
    start_base_sha: str = Field(pattern=SHA40)


class SourceRefMetadata(StrictModel):
    source_ref_id: uuid.UUID
    relative_path: str = Field(min_length=1, max_length=1000)
    blob_hash: str = Field(pattern=SHA64)

    @field_validator("relative_path")
    @classmethod
    def only_relative_repo_paths(cls, value: str) -> str:
        return _relative_repo_path(value)


class SourceExcerptCreate(OperationModel):
    """A separately approved, immutable slice of one captured source."""

    excerpt_id: uuid.UUID
    source_ref_id: uuid.UUID
    snapshot_id: uuid.UUID
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_text: str = Field(min_length=1, max_length=8192)
    content_hash: str = Field(pattern=SHA64)

    @model_validator(mode="after")
    def valid_span(self) -> SourceExcerptCreate:
        if self.end_line < self.start_line:
            raise ValueError("片段末行不能早于起始行")
        if len(self.content_text.encode("utf-8")) > 8192:
            raise ValueError("批准片段不能超过 8192 字节")
        return self


def _relative_repo_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        value in {".", ".."}
        or path.is_absolute()
        or "\\" in value
        or ":" in value
        or ".." in path.parts
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("源码引用必须使用仓库内相对路径")
    return value


class CaptureScope(StrictModel):
    kind: str = Field(pattern=r"^(worktree|commit)$")
    sha: str | None = Field(default=None, pattern=SHA40)
    selected_untracked: list[str] = Field(default_factory=list, max_length=80)

    @field_validator("selected_untracked")
    @classmethod
    def only_relative_untracked(cls, paths: list[str]) -> list[str]:
        return [_relative_repo_path(path) for path in paths]


class TopicProposalMetadata(StrictModel):
    proposal_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)


class OpportunityMetadata(StrictModel):
    opportunity_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    learning_goal: str = Field(min_length=1, max_length=500)
    source_refs: list[uuid.UUID] = Field(min_length=1, max_length=8)
    estimated_minutes: int = Field(ge=3, le=5)
    topic_proposals: list[TopicProposalMetadata] = Field(max_length=3)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)


class SnapshotCreate(OperationModel):
    project_id: uuid.UUID
    feature_id: uuid.UUID
    snapshot_id: uuid.UUID
    resolved_base_sha: str = Field(pattern=SHA40)
    head_sha: str = Field(pattern=SHA40)
    effective_tree_hash: str = Field(pattern=SHA40_OR_64)
    diff_hash: str = Field(pattern=SHA64)
    capture_scope: CaptureScope
    privacy_policy_version: str = Field(min_length=1, max_length=50)
    source_refs: list[SourceRefMetadata] = Field(max_length=80)


class AnalysisResult(SnapshotCreate):
    analysis_id: uuid.UUID
    input_fingerprint: str = Field(pattern=SHA64)
    mode: str = Field(pattern=r"^(dry_run|live)$")
    status: str = Field(pattern=r"^(succeeded|failed)$")
    upstream_run_id: str | None = Field(default=None, max_length=200)
    generation_origin: str = Field(default="replay", pattern=r"^(live|replay)$")
    provider_name: str | None = Field(default=None, max_length=100)
    model_name: str | None = Field(default=None, max_length=100)
    provider_request_id: str | None = Field(default=None, max_length=200)
    opportunities: list[OpportunityMetadata] = Field(
        default_factory=list[OpportunityMetadata], max_length=3
    )

    @model_validator(mode="after")
    def live_generation_needs_live_analysis(self) -> AnalysisResult:
        if self.generation_origin == "live" and (
            self.mode != "live" or not self.provider_name or not self.model_name
        ):
            raise ValueError("live 机会需要 live 分析及 provider/model")
        return self


class TopicDecision(OperationModel):
    decision: str = Field(pattern=r"^(confirm|reject)$")
    topic_id: uuid.UUID | None = None
    reuse_existing: bool = False

    @model_validator(mode="after")
    def topic_id_matches_decision(self) -> TopicDecision:
        if (self.decision == "confirm") != (self.topic_id is not None):
            raise ValueError("确认需提供 topic_id，拒绝时不能提供")
        if self.reuse_existing and self.decision != "confirm":
            raise ValueError("拒绝主题建议时不能关联已有主题")
        return self


class TaskCreate(OperationModel):
    task_id: uuid.UUID
    opportunity_id: uuid.UUID
    topic_id: uuid.UUID
    question: str = Field(min_length=1, max_length=600)
    learning_goal: str | None = Field(default=None, max_length=500)
    back_answer: str = Field(default="", max_length=1200)
    back_explanation: str = Field(default="", max_length=1200)
    card_version: int = Field(default=1, ge=1)
    source_commit_shas: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("source_commit_shas")
    @classmethod
    def valid_source_commit_shas(cls, shas: list[str]) -> list[str]:
        if len(set(shas)) != len(shas) or any(
            len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha)
            for sha in shas
        ):
            raise ValueError("卡片来源提交 SHA 无效或重复")
        return shas
    followups: list[str] = Field(default_factory=list, max_length=2)
    module_id: uuid.UUID | None = None
    module_index_revision: str | None = Field(default=None, pattern=SHA64)

    @model_validator(mode="after")
    def module_reference_pair(self) -> TaskCreate:
        if (self.module_id is None) != (self.module_index_revision is None):
            raise ValueError("模块 ID 与索引修订必须同时提供")
        return self


class TaskProgressUpdate(OperationModel):
    base_revision: int = Field(ge=1)
    progress: str = Field(pattern=r"^(in_progress|paused|completed|dismissed)$")


class AttemptCreate(OperationModel):
    attempt_id: uuid.UUID
    parent_attempt_id: uuid.UUID | None = None
    answer_text: str = Field(min_length=1, max_length=20000)
    hint_level: int = Field(ge=0, le=3)
    actor_origin: str = Field(pattern=r"^(user|simulated_user)$")


class TaskDraftUpdate(OperationModel):
    base_revision: int = Field(ge=0)
    content_text: str = Field(max_length=20000)
    after_attempt_id: uuid.UUID | None = None


class FeedbackBody(StrictModel):
    summary: str = Field(min_length=1, max_length=1200)
    user_claims: list[str] = Field(default_factory=list, max_length=8)
    code_facts: list[str] = Field(default_factory=list, max_length=8)
    general_principles: list[str] = Field(default_factory=list, max_length=8)
    inferences: list[str] = Field(default_factory=list, max_length=8)
    corrections: list[str] = Field(default_factory=list, max_length=8)
    source_refs: list[uuid.UUID] = Field(max_length=8)


class AttemptFeedback(OperationModel):
    feedback_origin: str = Field(pattern=r"^(live|replay)$")
    feedback: FeedbackBody


class NoteCreate(OperationModel):
    note_id: uuid.UUID
    task_id: uuid.UUID | None = None
    topic_id: uuid.UUID
    content_text: str = Field(min_length=1, max_length=100000)


class NoteRevision(OperationModel):
    base_revision: int = Field(ge=1)
    content_text: str = Field(min_length=1, max_length=100000)


class NoteResolve(NoteRevision):
    conflict_ids: list[uuid.UUID] = Field(min_length=1, max_length=8)


class NoteDelete(OperationModel):
    base_revision: int = Field(ge=1)


class ReviewSubmit(OperationModel):
    review_id: uuid.UUID
    base_revision: int = Field(ge=1)
    answer_text: str = Field(min_length=1, max_length=20000)
    answer: str = Field(pattern=r"^(easy|good|hard|wrong)$")
    hint_level: int = Field(ge=0, le=3)
    actor_origin: str = Field(pattern=r"^(user|simulated_user)$")


class ModuleNode(StrictModel):
    symbol: str = Field(min_length=3, max_length=300)
    file_path: str = Field(min_length=1, max_length=1000)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    source_hash: str = Field(pattern=SHA64)

    @field_validator("file_path")
    @classmethod
    def relative_file_path(cls, path: str) -> str:
        return _relative_repo_path(path)


class ModuleEdgeEvidence(StrictModel):
    file_path: str = Field(min_length=1, max_length=1000)
    line: int = Field(ge=1)

    @field_validator("file_path")
    @classmethod
    def relative_file_path(cls, path: str) -> str:
        return _relative_repo_path(path)


class ModuleEdge(StrictModel):
    from_symbol: str = Field(alias="from", min_length=3, max_length=300)
    to_symbol: str = Field(alias="to", min_length=3, max_length=300)
    status: str = Field(pattern=r"^(indexed|unknown)$")
    evidence: ModuleEdgeEvidence | None = None

    @model_validator(mode="after")
    def evidence_matches_status(self) -> ModuleEdge:
        if (self.status == "indexed") != (self.evidence is not None):
            raise ValueError("索引调用边必须有证据，未知边不能伪造证据")
        return self


class ModuleFlow(StrictModel):
    snapshot_id: uuid.UUID
    index_revision: str = Field(pattern=SHA64)
    effective_tree_hash: str = Field(pattern=SHA64)
    nodes: list[ModuleNode] = Field(min_length=1, max_length=100)
    edges: list[ModuleEdge] = Field(max_length=200)


class ModuleMap(OperationModel):
    module_id: uuid.UUID
    project_id: uuid.UUID
    snapshot_id: uuid.UUID
    index_revision: str = Field(pattern=SHA64)
    effective_tree_hash: str = Field(pattern=SHA64)
    suggested_name: str = Field(min_length=1, max_length=200)
    member_paths: list[str] = Field(min_length=1, max_length=100)
    flow: ModuleFlow
    base_revision: int | None = Field(default=None, ge=1)

    @field_validator("member_paths")
    @classmethod
    def relative_member_paths(cls, paths: list[str]) -> list[str]:
        normalized = [_relative_repo_path(path) for path in paths]
        if len(set(normalized)) != len(normalized):
            raise ValueError("模块成员不能重复")
        return normalized

    @model_validator(mode="after")
    def flow_matches_index(self) -> ModuleMap:
        if (self.flow.snapshot_id != self.snapshot_id
                or self.flow.index_revision != self.index_revision
                or self.flow.effective_tree_hash != self.effective_tree_hash):
            raise ValueError("模块流程与索引修订不一致")
        nodes = {node.symbol: node for node in self.flow.nodes}
        if len(nodes) != len(self.flow.nodes):
            raise ValueError("模块流程符号重复")
        if not {node.file_path for node in self.flow.nodes} <= set(self.member_paths):
            raise ValueError("模块流程文件不属于模块成员")
        for edge in self.flow.edges:
            if edge.from_symbol not in nodes or edge.to_symbol not in nodes:
                raise ValueError("模块调用边必须连接流程中的符号")
            if edge.evidence is not None:
                target = nodes[edge.to_symbol]
                if (edge.evidence.file_path != target.file_path
                        or edge.evidence.line != target.start_line):
                    raise ValueError("调用边证据与目标源码位置不一致")
        return self


class ModuleEdit(OperationModel):
    base_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    locked: bool
    member_paths: list[str] = Field(min_length=1, max_length=100)
    resolve_pending: bool = False

    @field_validator("member_paths")
    @classmethod
    def relative_member_paths(cls, paths: list[str]) -> list[str]:
        normalized = [_relative_repo_path(path) for path in paths]
        if len(set(normalized)) != len(normalized):
            raise ValueError("模块成员不能重复")
        return normalized


class TopicCreate(OperationModel):
    topic_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)


class ChatStart(OperationModel):
    session_id: uuid.UUID
    project_id: uuid.UUID | None = None


class ChatUserMessage(OperationModel):
    message_id: uuid.UUID
    content_text: str = Field(min_length=1, max_length=20000)


class ChatSuggestion(StrictModel):
    suggestion_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class ChatAssistantReplay(OperationModel):
    message_id: uuid.UUID
    content_text: str = Field(min_length=1, max_length=20000)
    suggestion: ChatSuggestion | None = None


class ChatAssistantLive(ChatAssistantReplay):
    provider_name: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=200)
    provider_request_id: str | None = Field(default=None, max_length=200)
    request_payload_hash: str = Field(pattern=SHA64)
    response_payload_hash: str = Field(pattern=SHA64)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)


class ChatSuggestionDecision(OperationModel):
    decision: str = Field(pattern=r"^(confirm|reject)$")
    topic_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def topic_matches_decision(self) -> ChatSuggestionDecision:
        if (self.decision == "confirm") != (self.topic_id is not None):
            raise ValueError("确认必须提供主题 ID；拒绝不能提供")
        return self


class TopicUpdate(OperationModel):
    base_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    status: str = Field(pattern=r"^(active|archived)$")


class TopicMerge(OperationModel):
    target_topic_id: uuid.UUID
    source_base_revision: int = Field(ge=1)
    target_base_revision: int = Field(ge=1)


def _empty_uuid_ids() -> list[uuid.UUID]:
    return []


class TopicSplitChild(StrictModel):
    topic_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    note_ids: list[uuid.UUID] = Field(default_factory=_empty_uuid_ids, max_length=100)
    task_ids: list[uuid.UUID] = Field(default_factory=_empty_uuid_ids, max_length=100)


class TopicSplit(OperationModel):
    base_revision: int = Field(ge=1)
    children: list[TopicSplitChild] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def children_unique(self) -> TopicSplit:
        ids = [child.topic_id for child in self.children]
        if len(set(ids)) != len(ids):
            raise ValueError("拆分子主题 ID 不能重复")
        return self


class DevelopmentEvent(OperationModel):
    schema_version: int = Field(ge=1, le=1)
    event_id: uuid.UUID
    project_id: uuid.UUID
    feature_id: uuid.UUID | None = None
    type: str = Field(pattern=r"^coding_task_finished$")
    occurred_at: datetime
    source: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    target_device_id: uuid.UUID | None = None
    expected_head_sha: str = Field(pattern=SHA40)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("开发事件时间必须包含时区")
        return value


class DevelopmentEventClaim(OperationModel):
    expected_head_sha: str = Field(pattern=SHA40)


class ExportCreate(OperationModel):
    export_id: uuid.UUID
