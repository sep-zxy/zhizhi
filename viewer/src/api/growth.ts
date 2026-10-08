import { apiFetch, apiFetchBlob } from './client';

export interface GrowthProject {
  project_id: string;
  name: string;
  created_at: string;
  local_processing: boolean;
  model_allowed: boolean;
  cloud_allowed: boolean;
  last_publish_error: string | null;
}

export interface GrowthBinding {
  binding_id: string;
  project_id: string;
  canonical_local_path: string;
  created_at: string;
  provider_names: string[];
}

export interface GrowthFeature {
  feature_id: string;
  project_id: string;
  label: string;
  base_ref: string;
  status: string;
  created_at: string;
}

export interface GrowthSnapshot {
  snapshot_id: string;
  feature_id: string;
  binding_ids: string[];
  head_sha: string;
  diff_hash: string;
  created_at: string;
}

export interface GrowthCommit {
  author_group?: string;
  linked_card_count?: number;
  author_name?: string;
  author_email?: string;
  is_new?: boolean;
  learning_status?: string;
  sha: string;
  title: string;
  authored_at: string;
  category: string;
  paths: string[];
}

export interface GrowthCodeIndex {
  snapshot_id: string;
  effective_tree_hash: string;
  index_revision: string;
  codegraph_version: string;
  indexed_files: string[];
  flow: {
    nodes: Array<{
      symbol: string;
      file_path: string;
      start_line: number;
      end_line: number;
      source_hash: string;
    }>;
    edges: Array<{
      from: string;
      to: string;
      status: 'indexed' | 'unknown';
      evidence: { file_path: string; line: number } | null;
    }>;
  };
}

export interface GrowthCloudModule {
  module_id: string;
  project_id: string;
  snapshot_id: string;
  index_revision: string;
  name: string;
  member_paths: string[];
  revision: number;
  locked: boolean;
  pending_adjustment: {
    snapshot_id: string;
    index_revision: string;
    member_paths: string[];
  } | null;
}

export interface GrowthModuleMapOperation {
  operation_id: string;
  module_id: string;
  project_id: string;
  snapshot_id: string;
  index_revision: string;
  effective_tree_hash: string;
  suggested_name: string;
  member_paths: string[];
  flow: GrowthCodeIndex['flow'] & {
    snapshot_id: string; index_revision: string; effective_tree_hash: string;
  };
  base_revision: number | null;
}

export interface GrowthAnalysisRun {
  analysis_id: string;
  snapshot_id: string;
  mode: 'dry_run' | 'live';
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  upstream_run_id: string | null;
  supersedes_analysis_id: string | null;
  needs_recapture: boolean;
  created_at: string;
}

export interface GrowthAnalysisSubmission {
  task_id?: string;
  analysis_id: string;
  status?: GrowthAnalysisRun['status'];
  reused?: boolean;
}

export interface GrowthLocalState {
  local_topics: Array<{ topic_id: string; title: string; status: string; created_at: string }>;
  local_topic_projects: Array<{ topic_id: string; project_id: string }>;
  local_explorations: Array<{
    session_id: string; project_id: string; messages_json: string; title?: string | null;
    suggestion_json: string | null; topic_id: string | null;
  }>;
  projects: GrowthProject[];
  bindings: GrowthBinding[];
  features: GrowthFeature[];
  snapshots: GrowthSnapshot[];
  analysis_runs: GrowthAnalysisRun[];
  opportunities: GrowthOpportunity[];
  topic_proposals: GrowthTopicProposal[];
  growth_tasks: GrowthTaskSummary[];
}

export interface GrowthLocalExplorePreview {
  payload_text: string;
  approval_hash: string;
  provider_host: string;
  model_name: string;
}

export interface GrowthExploreSource {
  source_ref_id?: string;
  project_id: string;
  project_name?: string;
  binding_id?: string;
  commit_sha: string;
  commit_title?: string;
  relative_path?: string;
}
export interface GrowthExploreMessage {
  role: 'user' | 'assistant';
  content_text: string;
  source_refs?: GrowthExploreSource[];
}
export interface GrowthLocalExploreResult {
  session_id: string;
  messages: GrowthExploreMessage[];
  title?: string | null;
  suggestion: { title: string; reason: string } | null;
}

export interface GrowthMaterials {
  status: 'none' | 'queued' | 'running' | 'ready' | 'failed';
  run_id: string | null;
  error?: string | null;
  lesson: string | null;
  questions: Array<{
    question_id: string; question: string; quiz_kind: string; exercise_kind: string;
    expected_answer?: string; explanation?: string;
    review_card_id?: string | null; review_due_date?: string | null; review_reps?: number;
  }>;
  attempts?: Array<{ attempt_id: string; question_id: string; answer_text: string }>;
  misconceptions?: Array<{
    card_id: string; misconception: string; correction: string; evidence_ref: string;
  }>;
}

export interface GrowthMaterialsPreview {
  patch_text: string; approval_hash: string; provider_host: string; model_name: string;
}

export function previewGrowthMaterials(
  taskId: string, bindingId: string, providerName: string,
): Promise<GrowthMaterialsPreview> {
  return post(`/api/growth/local/tasks/${taskId}/materials/preview`, {
    binding_id: bindingId, provider_name: providerName,
  });
}

export function generateGrowthMaterials(
  taskId: string, bindingId: string, providerName: string, approvalHash: string,
): Promise<{ task_id?: string; status: string }> {
  return post(`/api/growth/local/tasks/${taskId}/materials`, {
    binding_id: bindingId, provider_name: providerName,
    approved_payload_hash: approvalHash, approved: true,
  });
}

export function getGrowthMaterials(taskId: string): Promise<GrowthMaterials> {
  return apiFetch(`/api/growth/local/tasks/${taskId}/materials`);
}

export function revealGrowthMaterialAnswer(taskId: string, questionId: string, answerText: string
): Promise<{ expected_answer: string; explanation: string }> {
  return post(`/api/growth/local/tasks/${taskId}/materials/quiz/${questionId}/reveal`, {
    answer_text: answerText,
  });
}

export function rateGrowthMaterialCard(
  taskId: string, questionId: string, answer: 'wrong' | 'hard' | 'good' | 'easy',
  idempotencyKey: string,
): Promise<{ card_id: string; due_date: string | null; reused: boolean }> {
  return post(`/api/growth/local/tasks/${taskId}/materials/quiz/${questionId}/rate`, {
    answer, idempotency_key: idempotencyKey,
  });
}

export function previewGrowthLocalExplore(
  projectId: string, sessionId: string, bindingId: string,
  providerName: string, message: string,
): Promise<GrowthLocalExplorePreview> {
  return post(`/api/growth/local/projects/${projectId}/explore/preview`, {
    session_id: sessionId, binding_id: bindingId, provider_name: providerName, message,
  });
}

export function sendGrowthLocalExplore(
  projectId: string, sessionId: string, bindingId: string,
  providerName: string, message: string, approvalHash: string,
): Promise<GrowthLocalExploreResult> {
  return post(`/api/growth/local/projects/${projectId}/explore`, {
    session_id: sessionId, binding_id: bindingId, provider_name: providerName, message,
    approved_payload_hash: approvalHash, approved: true,
  });
}

export function confirmGrowthLocalExploreTopic(
  projectId: string, sessionId: string,
): Promise<{ topic_id: string }> {
  return post(`/api/growth/local/projects/${projectId}/explore/${sessionId}/topic`, {});
}

export interface GrowthTopicProposal {
  proposal_id: string;
  opportunity_id: string;
  title: string;
  status: 'pending' | 'confirmed' | 'rejected';
  topic_id: string | null;
}

export interface GrowthTaskSummary {
  project_id?: string;
  card_id?: string;
  front_question?: string;
  learning_goal?: string;
  back_answer?: string;
  back_explanation?: string;
  card_version?: number;
  source_link_status?: string;
  source_commit_shas?: string[];
  task_id: string;
  opportunity_id: string;
  topic_id: string;
  question: string;
  module_id?: string | null;
  module_index_revision?: string | null;
  progress: 'ready' | 'in_progress' | 'paused' | 'completed' | 'dismissed';
}

export interface GrowthTaskChain {
  task: GrowthTaskSummary;
  opportunity: GrowthOpportunity;
  card: {
    quiz_kind: 'guided' | 'recall' | 'transfer';
    exercise_kind: 'prediction' | 'completion' | 'error_reason';
    context: string;
    question: string;
    expected_answer: string;
    hints: string[];
    followups: string[];
  } | null;
  sources: Array<{ source_ref_id: string; relative_path: string }>;
  source_patch: string;
  hints: Array<{ hint_id: string; level: number; hint_text: string }>;
  attempts: Array<{
    attempt_id: string;
    parent_attempt_id: string | null;
    answer_text: string;
    hint_level: number;
    status: 'accepted' | 'feedback_pending' | 'feedback_ready' | 'feedback_failed';
    feedback_json: string | null;
    feedback_origin: 'live' | 'replay' | null;
    feedback_error: string | null;
  }>;
  notes: Array<{ note_id: string; content_text: string; author: 'user' }>;
}

export interface GrowthOpportunity {
  project_id?: string;
  source_commit_shas?: string[];
  recommendation_key?: string;
  canonical_card_id?: string | null;
  recommendation_status?: string;
  suggested_card_count?: number;
  opportunity_id: string;
  analysis_id: string;
  ordinal: number;
  title: string;
  reason: string;
  learning_goal: string;
  source_refs_json: string;
  estimated_minutes: number;
  uncertainties_json: string;
}

export function decideGrowthRecommendations(input: {
  request_id: string;
  confirm_groups: Array<{ opportunity_ids: string[]; proposal_id: string; existing_topic_id?: string | null }>;
  defer_ids: string[]; ignore_ids: string[];
}): Promise<{ cards: Array<{ card_id: string; task_id: string; opportunity_ids: string[]; reused: boolean }>;
  created_card_count: number }> {
  return post('/api/growth/local/recommendations/decisions', input);
}

export interface GrowthModelSource {
  source_ref_id: string;
  relative_path: string;
  blob_hash: string;
  deleted: number;
  byte_count: number;
}

export interface GrowthModelSources {
  sources: GrowthModelSource[];
  provider_names: string[];
}

export interface GrowthModelPreview {
  payload_text: string;
  approval_hash: string;
  provider_host: string;
  model_name: string;
  source_ref_ids: string[];
}

export interface GrowthSourceExcerptPreview {
  source_ref_id: string;
  snapshot_id: string;
  project_id: string;
  relative_path: string;
  blob_hash: string;
  start_line: number;
  end_line: number;
  content_text: string;
  content_hash: string;
  approval_hash: string;
}

export interface GrowthFeedbackPreview {
  payload_text: string;
  approval_hash: string;
  provider_host: string;
  model_name: string;
}

export interface GrowthFeedbackSubmission {
  task_id?: string;
  attempt_id: string;
  status?: 'queued' | 'running' | 'succeeded' | 'failed';
  reused?: boolean;
}

export interface GrowthChatSession {
  session_id: string;
  project_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface GrowthChat {
  session: GrowthChatSession;
  messages: Array<{
    message_id: string; role: 'user' | 'assistant'; origin: 'user' | 'live' | 'replay';
    content_text: string; created_at: string;
  }>;
  suggestions: Array<{
    suggestion_id: string; title: string; reason: string;
    status: 'pending' | 'confirmed' | 'rejected'; topic_id: string | null;
  }>;
}

export interface GrowthChatPreview {
  payload_text: string; approval_hash: string;
  provider_host: string; model_name: string;
}

export interface CapturedGrowthSnapshot {
  snapshot_id: string;
  head_sha: string;
  resolved_base_sha: string;
  diff_hash: string;
  created_at: string;
  changed_paths: string[];
}

export interface GrowthSyncSummary {
  account_id: string;
  device_id: string;
  cursor: number;
  acknowledged_cursor: number;
  pulled: number;
  uploaded: number;
  queued: number;
  outbox: { pending: number; acked: number; conflict: number; deleted: number };
}

export interface GrowthCloudExport {
  export_id: string;
  json_text: string;
  markdown_text: string;
}

export interface GrowthSyncPreview {
  project_id: string;
  digest: string;
  count: number;
  operations: Array<{
    method: string;
    path: string;
    payload: Record<string, unknown>;
    kind: string;
    source_id: string;
  }>;
}

export interface GrowthSyncedNote {
  account_id: string;
  note_id: string;
  project_id: string | null;
  revision: number;
  deleted_at: string | null;
  note: { content_text: string; task_id?: string | null; topic_id?: string | null };
  revisions: Array<{
    revision: number; content_text: string; source_conflict_ids: string[];
  }>;
  conflicts: Array<{
    conflict_id: string; base_revision: number; observed_server_revision: number;
    proposed_text: string; resolved_revision?: number | null;
  }>;
  drafts: Array<{
    base_revision: number; content_text: string; operation_id: string;
    state: 'pending' | 'synced' | 'conflict' | 'recovery';
    created_at: string; response: Record<string, unknown> | null;
  }>;
  pending_operations: Array<{ operation_id: string; method: string }>;
}

export interface GrowthSyncWorkspace {
  accounts: Array<{ account_id: string; device_id: string; cloud_origin: string | null; sync_summary?: GrowthSyncSummary }>;
  notes: GrowthSyncedNote[];
  tasks: GrowthSyncedTask[];
}

export interface GrowthDevelopmentEvent {
  account_id: string;
  device_id: string;
  event_id: string;
  project_id: string;
  feature_id: string | null;
  expected_head_sha: string;
  status: string;
  target_device_id: string | null;
  bindings: Array<{ binding_id: string; canonical_local_path: string }>;
  capture: { binding_id: string; snapshot_id: string; analysis_id: string | null } | null;
}

export interface GrowthEventCapture {
  event_id: string;
  snapshot_id: string;
  analysis_id: string | null;
  reused: boolean;
  head_sha: string;
}

export interface GrowthSyncedTask {
  account_id: string;
  task_id: string;
  project_id: string | null;
  revision: number;
  deleted_at: string | null;
  task: { question: string; progress: string; topic_id?: string };
  approved_sources: Array<{
    source_ref_id: string;
    relative_path: string;
    blob_hash: string;
    snapshot_id: string;
    approved_excerpt: null | {
      excerpt_id: string;
      start_line: number;
      end_line: number;
      content_text: string;
      content_hash: string;
    };
  }>;
  remote_draft: {
    revision: number; content_text: string; submitted_attempt_id: string | null;
  } | null;
  local_draft: {
    local_revision: number; content_text: string;
    submitted_attempt_id: string | null; latest_operation_id: string | null;
    state: 'pending' | 'synced' | 'conflict' | 'recovery'; updated_at: string;
  } | null;
  pending_attempts: Array<{
    attempt_id: string; parent_attempt_id: string | null; answer_text: string;
    hint_level: number; operation_id: string;
    state: 'pending' | 'synced' | 'conflict' | 'recovery'; created_at: string;
  }>;
  attempts: Array<{
    attempt_id: string; parent_attempt_id: string | null;
    answer_text: string; hint_level: number;
    status?: string; feedback?: Record<string, unknown>;
  }>;
  pending_operations: Array<{ operation_id: string; method: string; path: string }>;
  blocked_operations: Array<{
    operation_id: string; path: string;
    payload: { content_text?: string; answer_text?: string; progress?: string };
    response: { code?: string; blocked_by?: string } | null;
  }>;
}

function post<T>(path: string, payload: object): Promise<T> {
  return apiFetch<T>(path, { method: 'POST', body: JSON.stringify(payload) });
}

export function getGrowthLocalState(): Promise<GrowthLocalState> {
  return apiFetch('/api/growth/local');
}

export function previewGrowthCloud(projectId: string): Promise<GrowthSyncPreview> {
  return post('/api/growth/local/sync/preview', { project_id: projectId });
}

export function syncGrowthCloud(
  cloudUrl: string, accessToken: string, preview?: GrowthSyncPreview,
  refreshHistory = false,
): Promise<GrowthSyncSummary> {
  return post('/api/growth/local/sync', {
    cloud_url: cloudUrl,
    access_token: accessToken,
    refresh_history: refreshHistory,
    ...(preview ? {
      publish_project_id: preview.project_id,
      approved_digest: preview.digest,
    } : {}),
  });
}

export function createGrowthCloudExport(
  cloudUrl: string, accessToken: string, accountId: string, exportId: string,
): Promise<GrowthCloudExport> {
  return post('/api/growth/local/cloud/exports', {
    cloud_url: cloudUrl, access_token: accessToken, account_id: accountId,
    export_id: exportId,
  });
}

export type GrowthCloudAccess = {
  cloud_url: string; access_token: string; account_id: string;
};

export interface GrowthDueReview {
  account_id?: string; device_id?: string;
  card_id?: string; project_id?: string; back_answer?: string; back_explanation?: string;
  task_id: string; topic_id: string; question: string;
  due_at: string; revision: number; last_hint_level: number | null;
}

export async function listGrowthLocalReviews(accountId?: string): Promise<{ due: GrowthDueReview[] }> {
  if (accountId) {
    const result = await apiFetch<{ due: GrowthDueReview[] }>('/api/growth/local/reviews/due?account_id=' + encodeURIComponent(accountId));
    return { due: result.due.map((item) => ({ ...item, account_id: accountId })) };
  }
  const workspace = await getGrowthSyncWorkspace();
  const results = await Promise.allSettled(workspace.accounts.map(async (account) => {
    const result = await listGrowthLocalReviews(account.account_id);
    return result.due.map((item) => ({ ...item, device_id: account.device_id }));
  }));
  if (results.length && results.every((result) => result.status === 'rejected')) throw new Error('本机尚无可用复习缓存，请先同步账号。');
  return { due: results.flatMap((result) => result.status === 'fulfilled' ? result.value : []) };
}

export function queueGrowthReview(cardId: string, input: {
  account_id: string; device_id?: string; operation_id: string; review_id: string;
  base_revision: number; answer_text: string; answer: GrowthReviewRating; hint_level: number;
}): Promise<{ review_id: string; due_at?: string; sync_state?: string }> {
  return post(`/api/growth/local/cards/${cardId}/reviews`, input);
}

export type GrowthReviewRating = 'easy' | 'good' | 'hard' | 'wrong';

export function listGrowthDueReviews(access: GrowthCloudAccess
): Promise<{ due: GrowthDueReview[] }> {
  return post('/api/growth/local/cloud/reviews/due', access);
}

export function submitGrowthReview(access: GrowthCloudAccess, taskId: string,
  operationId: string, reviewId: string, baseRevision: number,
  answerText: string, answer: GrowthReviewRating, hintLevel: number,
): Promise<{ review_id: string; revision?: number; due_at?: string; hint_level?: number; sync_state?: string }> {
  return post(`/api/growth/local/cloud/tasks/${taskId}/reviews`, {
    ...access, operation_id: operationId, review_id: reviewId,
    base_revision: baseRevision, answer_text: answerText,
    answer, hint_level: hintLevel,
  });
}

export function listGrowthCloudModules(access: GrowthCloudAccess, projectId: string
): Promise<{ modules: GrowthCloudModule[] }> {
  return post(`/api/growth/local/cloud/projects/${projectId}/modules`, access);
}

export function mapGrowthCloudModule(access: GrowthCloudAccess, bindingId: string,
  operation: GrowthModuleMapOperation): Promise<{
    module_id: string; revision: number; pending_adjustment: boolean;
  }> {
  return post('/api/growth/local/cloud/modules/map', {
    ...access, binding_id: bindingId, operation,
  });
}

export function editGrowthCloudModule(access: GrowthCloudAccess, moduleId: string,
  operation: { operation_id: string; base_revision: number; name: string;
    locked: boolean; member_paths: string[]; resolve_pending: boolean }
): Promise<{ module_id: string; revision: number; name: string; locked: boolean }> {
  return post(`/api/growth/local/cloud/modules/${moduleId}/update`, {
    ...access, operation,
  });
}

export interface GrowthCloudTopic {
  topic_id: string; title: string; status: 'active' | 'archived';
  revision: number; parent_topic_id: string | null;
  canonical_topic_id?: string | null;
}

export interface GrowthCloudTopicDetail {
  requested_topic_id: string; canonical_topic_id: string; is_alias: boolean;
  topic: GrowthCloudTopic;
  notes: Array<{ note_id: string; topic_id: string; content_text: string; revision: number }>;
  tasks: Array<{ task_id: string; topic_id: string; question: string; revision: number }>;
  children: GrowthCloudTopic[];
  moves: Array<{ move_id: string; entity_type: string; entity_id: string;
    from_topic_id: string; to_topic_id: string; action: string }>;
}

export function listGrowthCloudTopics(access: GrowthCloudAccess
): Promise<{ topics: GrowthCloudTopic[] }> {
  return post('/api/growth/local/cloud/topics', access);
}

export function createGrowthCloudTopic(access: GrowthCloudAccess, operationId: string,
  topicId: string, title: string): Promise<{ topic_id: string }> {
  return post('/api/growth/local/cloud/topics/create', {
    ...access, operation_id: operationId, topic_id: topicId, title,
  });
}

export function readGrowthCloudTopic(access: GrowthCloudAccess, topicId: string
): Promise<GrowthCloudTopicDetail> {
  return post(`/api/growth/local/cloud/topics/${topicId}`, access);
}

export function createGrowthCloudTopicNote(access: GrowthCloudAccess, topicId: string,
  operationId: string, noteId: string, contentText: string,
  taskId: string | null = null): Promise<{ note_id: string }> {
  return post(`/api/growth/local/cloud/topics/${topicId}/notes`, {
    ...access, operation_id: operationId, note_id: noteId, task_id: taskId,
    content_text: contentText,
  });
}

export interface GrowthSyncedFeedbackPreview {
  payload_text: string;
  approval_hash: string;
  provider_host: string;
  model_name: string;
}

export function previewGrowthSyncedFeedback(
  access: GrowthCloudAccess, attemptId: string, providerName: string,
): Promise<GrowthSyncedFeedbackPreview> {
  return post(`/api/growth/local/cloud/attempts/${attemptId}/feedback/preview`, {
    ...access, provider_name: providerName,
  });
}

export function submitGrowthSyncedFeedback(
  access: GrowthCloudAccess, attemptId: string, providerName: string,
  approvalHash: string,
): Promise<{ attempt_id: string; status: string; feedback: Record<string, unknown> }> {
  return post(`/api/growth/local/cloud/attempts/${attemptId}/feedback`, {
    ...access, provider_name: providerName, approval_hash: approvalHash,
    approved: true,
  });
}

export function updateGrowthCloudTopic(access: GrowthCloudAccess, topicId: string,
  operationId: string, revision: number, title: string, status: 'active' | 'archived'
): Promise<{ topic_id: string; revision: number; status: string }> {
  return post(`/api/growth/local/cloud/topics/${topicId}/update`, {
    ...access, operation_id: operationId, base_revision: revision, title, status,
  });
}

export function mergeGrowthCloudTopics(access: GrowthCloudAccess, sourceId: string,
  targetId: string, operationId: string, sourceRevision: number, targetRevision: number
): Promise<{ source_topic_id: string; target_topic_id: string }> {
  return post(`/api/growth/local/cloud/topics/${sourceId}/merge`, {
    ...access, operation_id: operationId, target_topic_id: targetId,
    source_base_revision: sourceRevision, target_base_revision: targetRevision,
  });
}

export interface GrowthCloudSplitChild {
  topic_id: string; title: string; note_ids: string[]; task_ids: string[];
}

export function splitGrowthCloudTopic(access: GrowthCloudAccess, parentId: string,
  operationId: string, revision: number, children: GrowthCloudSplitChild[]
): Promise<{ revision: number; children: GrowthCloudTopic[] }> {
  return post(`/api/growth/local/cloud/topics/${parentId}/split`, {
    ...access, operation_id: operationId, base_revision: revision, children,
  });
}

type GrowthChatAccess = GrowthCloudAccess;

export function listGrowthChats(access: GrowthChatAccess): Promise<{ sessions: GrowthChatSession[] }> {
  return post('/api/growth/local/cloud/chats', access);
}

export function startGrowthChat(access: GrowthChatAccess, sessionId: string,
  projectId: string | null): Promise<{ session_id: string }> {
  return post('/api/growth/local/cloud/chats/start', {
    ...access, session_id: sessionId, project_id: projectId,
  });
}

export function readGrowthChat(access: GrowthChatAccess, sessionId: string): Promise<GrowthChat> {
  return post(`/api/growth/local/cloud/chats/${sessionId}`, access);
}

export function sendGrowthChatMessage(access: GrowthChatAccess, sessionId: string,
  messageId: string, contentText: string): Promise<{ message_id: string }> {
  return post(`/api/growth/local/cloud/chats/${sessionId}/messages`, {
    ...access, message_id: messageId, content_text: contentText,
  });
}

export function previewGrowthChat(access: GrowthChatAccess, sessionId: string,
  providerName: string): Promise<GrowthChatPreview> {
  return post(`/api/growth/local/cloud/chats/${sessionId}/preview`, {
    ...access, provider_name: providerName,
  });
}

export function generateGrowthChat(access: GrowthChatAccess, sessionId: string,
  providerName: string, requestId: string, messageId: string,
  approvedPayloadHash: string): Promise<{ sync_state: string }> {
  return post(`/api/growth/local/cloud/chats/${sessionId}/generate`, {
    ...access, provider_name: providerName, request_id: requestId,
    message_id: messageId, approved_payload_hash: approvedPayloadHash, approved: true,
  });
}

export function decideGrowthChatSuggestion(access: GrowthChatAccess, sessionId: string,
  suggestionId: string, decision: 'confirm' | 'reject',
): Promise<{ status: string; topic_id: string | null }> {
  return post(`/api/growth/local/cloud/chats/${sessionId}/suggestions/${suggestionId}/decision`, {
    ...access, decision,
  });
}

export function getGrowthSyncWorkspace(): Promise<GrowthSyncWorkspace> {
  return apiFetch('/api/growth/local/sync/workspace');
}

export interface GrowthSourceLocation {
  source_ref_id: string;
  relative_path: string;
  expected_blob_hash: string;
  status: 'matched' | 'version_mismatch' | 'missing';
  local_path: string | null;
}

export function getGrowthSyncedSourceLocations(taskId: string, bindingId: string): Promise<{
  task_id: string; project_id: string; binding_id: string;
  locations: GrowthSourceLocation[];
}> {
  return apiFetch(`/api/growth/local/sync/tasks/${taskId}/source-locations?binding_id=${encodeURIComponent(bindingId)}`);
}

export function bindGrowthSyncedRepository(taskId: string, path: string): Promise<{
  project_id: string; binding_id: string; path: string;
}> {
  return post(`/api/growth/local/sync/tasks/${taskId}/binding`, { path });
}

export interface GrowthTaskJourney {
  task: { task_id: string; question: string; progress: string; revision: number };
  timeline: Array<{ evidence_id: string; source_type: string; source_id: string;
    evidence_label: string; content_text: string; created_at: string }>;
  progress_events: Array<{ event_type: string; payload: { from: string; to: string };
    committed_at: string }>;
  next_question: null | { question: string; due_at: string; revision: number };
  memory_refs: Array<{ source_type: string; source_id: string;
    source_version: number; reme_path: string }>;
}

export function getGrowthTaskJourney(access: GrowthCloudAccess,
  taskId: string): Promise<GrowthTaskJourney> {
  return post(`/api/growth/local/cloud/tasks/${taskId}/journey`, access);
}

export function getGrowthDevelopmentEvents(): Promise<{ events: GrowthDevelopmentEvent[] }> {
  return apiFetch('/api/growth/local/sync/events');
}

export function captureGrowthDevelopmentEvent(
  event: GrowthDevelopmentEvent, bindingId: string, selectedUntracked: string[],
): Promise<GrowthEventCapture> {
  return post(`/api/growth/local/sync/events/${event.event_id}/capture`, {
    account_id: event.account_id, binding_id: bindingId,
    selected_untracked: selectedUntracked,
  });
}

export function queueGrowthNoteRevision(
  note: GrowthSyncedNote, contentText: string,
): Promise<{ operation_id: string; state: string }> {
  return post(`/api/growth/local/sync/notes/${note.note_id}/revisions`, {
    account_id: note.account_id, base_revision: note.revision, content_text: contentText,
  });
}

export function queueGrowthNoteResolution(
  note: GrowthSyncedNote, conflictIds: string[], contentText: string,
): Promise<{ operation_id: string; state: string }> {
  return post(`/api/growth/local/sync/notes/${note.note_id}/resolve`, {
    account_id: note.account_id, base_revision: note.revision,
    conflict_ids: conflictIds, content_text: contentText,
  });
}

export function queueGrowthNoteDelete(
  note: GrowthSyncedNote,
): Promise<{ operation_id: string; state: string }> {
  return apiFetch(`/api/growth/local/sync/notes/${note.note_id}`, {
    method: 'DELETE',
    body: JSON.stringify({ account_id: note.account_id, base_revision: note.revision }),
  });
}

export function queueGrowthTaskDraft(
  task: GrowthSyncedTask, contentText: string, afterAttemptId: string | null,
): Promise<{ operation_id: string; state: string }> {
  return post(`/api/growth/local/sync/tasks/${task.task_id}/draft`, {
    account_id: task.account_id, content_text: contentText,
    after_attempt_id: afterAttemptId,
  });
}

export function rebaseGrowthTaskDraft(
  task: GrowthSyncedTask, contentText: string, afterAttemptId: string | null,
): Promise<{ operation_id: string; state: string }> {
  return post(`/api/growth/local/sync/tasks/${task.task_id}/rebase`, {
    account_id: task.account_id, content_text: contentText,
    after_attempt_id: afterAttemptId,
  });
}

export function queueGrowthTaskAnswer(
  task: GrowthSyncedTask, answerText: string, parentAttemptId: string | null,
  hintLevel: number,
): Promise<{ operation_id: string; attempt_id: string; state: string }> {
  return post(`/api/growth/local/sync/tasks/${task.task_id}/attempts`, {
    account_id: task.account_id, answer_text: answerText,
    parent_attempt_id: parentAttemptId, hint_level: hintLevel,
  });
}

export function queueGrowthTaskProgress(
  task: GrowthSyncedTask, progress: string,
): Promise<{ operation_id: string; state: string }> {
  return post(`/api/growth/local/sync/tasks/${task.task_id}/progress`, {
    account_id: task.account_id, base_revision: task.revision, progress,
  });
}

export function createGrowthProject(name: string): Promise<{ project_id: string }> {
  return post('/api/growth/local/projects', { name });
}

export function importGrowthProject(
  name: string, path: string, modelAllowed: boolean,
): Promise<{ project_id: string; binding_id: string; feature_id: string; reused: string }> {
  return post('/api/growth/local/import', { name, path, model_allowed: modelAllowed });
}

export function setGrowthProjectPolicy(
  projectId: string, policy: Pick<GrowthProject,
    'local_processing' | 'model_allowed' | 'cloud_allowed'>,
): Promise<GrowthProject> {
  return apiFetch(`/api/growth/local/projects/${projectId}/policy`, {
    method: 'PUT', body: JSON.stringify(policy),
  });
}

export function bindGrowthRepository(
  projectId: string,
  path: string,
): Promise<{ binding_id: string }> {
  return post(`/api/growth/local/projects/${projectId}/bindings`, { path });
}

export function createGrowthFeature(
  projectId: string,
  bindingId: string,
  label: string,
  baseRef: string,
): Promise<{ feature_id: string }> {
  return post(`/api/growth/local/projects/${projectId}/features`, {
    binding_id: bindingId,
    label,
    base_ref: baseRef,
  });
}

export function captureGrowthSnapshot(
  featureId: string,
  bindingId: string,
  selectedUntracked: string[],
  commitSha?: string,
): Promise<CapturedGrowthSnapshot> {
  return post(`/api/growth/local/features/${featureId}/snapshots`, {
    binding_id: bindingId,
    selected_untracked: selectedUntracked,
    ...(commitSha ? { commit_sha: commitSha } : {}),
  });
}

export function getGrowthCommits(
  bindingId: string, featureId: string,
  options: { cursor?: string; authors?: string[]; groups?: string[]; scan?: boolean } = {},
): Promise<{ commits: GrowthCommit[]; base_sha: string; next_cursor: string | null;
  authors: Array<{ author_name: string; author_email: string; group_name: string; commit_count: number }>;
  scan?: { discovered_count: number; scanned_at?: string } }> {
  const query = new URLSearchParams({ feature_id: featureId, cursor: options.cursor ?? '0', limit: '50' });
  if (options.scan) query.set('scan', 'true');
  options.authors?.forEach((author) => query.append('author_email', author));
  options.groups?.forEach((group) => query.append('author_group', group));
  return apiFetch(`/api/growth/local/bindings/${bindingId}/commits?${query}`);
}

export function groupGrowthAuthors(bindingId: string, authorEmails: string[], groupName: string) {
  return post(`/api/growth/local/bindings/${bindingId}/authors/group`, {
    author_emails: authorEmails, group_name: groupName,
  });
}

export function exportGrowthCards(projectId: string): Promise<Blob> {
  return apiFetchBlob('/api/growth/local/cards.apkg' + (projectId ? '?project_id=' + encodeURIComponent(projectId) : ''));
}

export function indexGrowthSnapshot(
  snapshotId: string, bindingId: string, symbols: string[],
): Promise<GrowthCodeIndex> {
  return post(`/api/growth/local/snapshots/${snapshotId}/code-index`, {
    binding_id: bindingId, symbols,
  });
}

export function getGrowthUntracked(bindingId: string): Promise<{ paths: string[] }> {
  return apiFetch(`/api/growth/local/bindings/${bindingId}/untracked`);
}

export function startGrowthAnalysis(
  snapshotId: string,
  bindingId: string,
  requestId: string,
): Promise<GrowthAnalysisSubmission> {
  return post(`/api/growth/local/snapshots/${snapshotId}/analysis`, {
    binding_id: bindingId, mode: 'dry_run', request_id: requestId,
  });
}

export function retryGrowthAnalysis(
  analysisId: string,
  bindingId: string,
): Promise<GrowthAnalysisSubmission> {
  return post(`/api/growth/local/analyses/${analysisId}/retry`, {
    binding_id: bindingId,
  });
}

export function getGrowthModelSources(
  snapshotId: string,
  bindingId: string,
): Promise<GrowthModelSources> {
  return apiFetch(`/api/growth/local/snapshots/${snapshotId}/model-sources?binding_id=${bindingId}`);
}

export function previewGrowthSourceExcerpt(
  sourceRefId: string, startLine: number, endLine: number,
): Promise<GrowthSourceExcerptPreview> {
  return post(`/api/growth/local/source-refs/${sourceRefId}/excerpt/preview`, {
    start_line: startLine, end_line: endLine,
  });
}

export function queueGrowthSourceExcerpt(
  preview: GrowthSourceExcerptPreview, accountId: string,
): Promise<{ operation_id: string; state: string }> {
  return post(`/api/growth/local/source-refs/${preview.source_ref_id}/excerpt`, {
    account_id: accountId, start_line: preview.start_line,
    end_line: preview.end_line, approval_hash: preview.approval_hash,
    approved: true,
  });
}

export function previewGrowthModelRequest(
  snapshotId: string,
  bindingId: string,
  providerName: string,
  sourceRefIds: string[],
): Promise<GrowthModelPreview> {
  return post(`/api/growth/local/snapshots/${snapshotId}/model-preview`, {
    binding_id: bindingId,
    provider_name: providerName,
    source_ref_ids: sourceRefIds,
  });
}

export function startGrowthModelAnalysis(
  snapshotId: string,
  bindingId: string,
  providerName: string,
  preview: GrowthModelPreview,
  requestId: string,
  reanalysis = false,
  event?: Pick<GrowthDevelopmentEvent, 'account_id' | 'event_id'>,
): Promise<GrowthAnalysisSubmission> {
  return post(`/api/growth/local/snapshots/${snapshotId}/analysis`, {
    binding_id: bindingId,
    request_id: event?.event_id ?? requestId,
    mode: 'live',
    provider_name: providerName,
    source_ref_ids: preview.source_ref_ids,
    approved_payload_hash: preview.approval_hash,
    approved: true,
    reanalysis,
    ...(event ? { account_id: event.account_id, event_id: event.event_id } : {}),
  });
}

export function decideGrowthTopic(
  proposalId: string, decision: 'confirm' | 'reject',
  existing?: { account_id: string; existing_topic_id: string },
): Promise<{ topic_id: string | null }> {
  return post(`/api/growth/local/topic-proposals/${proposalId}/decision`, {
    decision, ...existing,
  });
}

export function createGrowthTask(
  opportunityId: string, topicId: string,
  module?: Pick<GrowthCloudModule, 'module_id' | 'index_revision'>,
): Promise<{ task_id: string }> {
  return post(`/api/growth/local/opportunities/${opportunityId}/tasks`, {
    topic_id: topicId,
    ...(module ? { module_id: module.module_id,
      module_index_revision: module.index_revision } : {}),
  });
}

export function getGrowthTaskChain(taskId: string): Promise<GrowthTaskChain> {
  return apiFetch(`/api/growth/local/tasks/${taskId}`);
}

export function submitGrowthAnswer(
  taskId: string, answerText: string, parentAttemptId: string | null, hintLevel: number,
): Promise<{ attempt_id: string }> {
  return post(`/api/growth/local/tasks/${taskId}/answers`, {
    answer_text: answerText, parent_attempt_id: parentAttemptId, hint_level: hintLevel,
  });
}

export function requestGrowthHint(
  taskId: string, requestId: string,
): Promise<{ hint_id: string; level: number; hint_text: string }> {
  return post(`/api/growth/local/tasks/${taskId}/hints`, { request_id: requestId });
}

export function saveGrowthNote(
  taskId: string, contentText: string,
): Promise<{ note_id: string }> {
  return post(`/api/growth/local/tasks/${taskId}/notes`, { content_text: contentText });
}

export function setGrowthTaskProgress(
  taskId: string, target: 'in_progress' | 'paused' | 'completed' | 'dismissed',
): Promise<{ status: string }> {
  return post(`/api/growth/local/tasks/${taskId}/progress`, { target });
}

export function previewGrowthFeedback(
  attemptId: string, bindingId: string,
): Promise<GrowthFeedbackPreview> {
  return post(`/api/growth/local/attempts/${attemptId}/feedback-preview`, {
    binding_id: bindingId,
  });
}

export function startGrowthFeedback(
  attemptId: string, bindingId: string, preview: GrowthFeedbackPreview, retry: boolean,
): Promise<GrowthFeedbackSubmission> {
  return post(`/api/growth/local/attempts/${attemptId}/feedback`, {
    binding_id: bindingId,
    request_id: crypto.randomUUID(),
    approved_payload_hash: preview.approval_hash,
    approved: true,
    retry,
  });
}
