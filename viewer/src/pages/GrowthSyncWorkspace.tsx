import { useCallback, useEffect, useState } from 'react';
import { bindGrowthSyncedRepository, getGrowthSyncWorkspace, getGrowthSyncedSourceLocations, getGrowthTaskJourney, previewGrowthSyncedFeedback, previewGrowthSourceExcerpt, queueGrowthSourceExcerpt, submitGrowthSyncedFeedback, queueGrowthNoteDelete, queueGrowthNoteResolution, queueGrowthNoteRevision, queueGrowthTaskAnswer, queueGrowthTaskDraft, queueGrowthTaskProgress, rebaseGrowthTaskDraft, type GrowthBinding, type GrowthCloudAccess, type GrowthModelSources, type GrowthSourceExcerptPreview, type GrowthSyncedFeedbackPreview, type GrowthSyncSummary, type GrowthSyncWorkspace, type GrowthSourceLocation, type GrowthTaskJourney } from '../api/growth';
import { useTranslation } from '../i18n/useTranslation';

interface FeedbackContent {
  summary: string;
  user_claims: string[];
  code_facts: string[];
  general_principles: string[];
  inferences: string[];
  corrections: string[];
}

export function GrowthFeedback({ raw }: { raw: string }) {
  const { t } = useTranslation();
  let feedback: FeedbackContent;
  try {
    feedback = JSON.parse(raw) as FeedbackContent;
  } catch {
    return <pre>{raw}</pre>;
  }
  const sections: Array<[string, string[]]> = [
    ['Growth.feedback_user_claims', feedback.user_claims],
    ['Growth.feedback_code_facts', feedback.code_facts],
    ['Growth.feedback_principles', feedback.general_principles],
    ['Growth.feedback_inferences', feedback.inferences],
    ['Growth.feedback_corrections', feedback.corrections],
  ];
  return <div className="growth-page__feedback">
    <p>{feedback.summary}</p>
    {sections.filter(([, values]) => values?.length).map(([label, values]) => <div key={label}>
      <strong>{t(label)}</strong>
      <ul>{values.map((value, index) => <li key={`${label}-${index}`}>{value}</li>)}</ul>
    </div>)}
  </div>;
}

export function GrowthSyncNotes({ refreshKey, disabled }: {
  refreshKey: GrowthSyncSummary | null; disabled: boolean;
}) {
  const { t } = useTranslation();
  const [workspace, setWorkspace] = useState<GrowthSyncWorkspace | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [editText, setEditText] = useState('');
  const [deleteApproved, setDeleteApproved] = useState(false);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState('');
  const load = useCallback(async () => {
    setWorkspace(await getGrowthSyncWorkspace());
  }, []);
  useEffect(() => { void load().catch(() => setMessage(t('Growth.note_workspace_failed'))); }, [load, refreshKey, t]);
  const notes = workspace?.notes.filter((note) => !refreshKey || note.account_id === refreshKey.account_id) ?? [];
  const selected = notes.find((note) => `${note.account_id}:${note.note_id}` === selectedId)
    ?? notes[0];
  const unresolved = selected?.conflicts.filter((conflict) => !conflict.resolved_revision) ?? [];
  const pending = (selected?.pending_operations.length ?? 0) > 0;
  const baseMissing = unresolved.some((conflict) => !selected?.revisions.some(
    (version) => version.revision === conflict.base_revision,
  ));

  async function queue(action: 'revision' | 'resolve' | 'delete') {
    if (!selected) return;
    setWorking(true);
    setMessage('');
    try {
      if (action === 'revision') await queueGrowthNoteRevision(selected, editText);
      if (action === 'resolve') await queueGrowthNoteResolution(
        selected, unresolved.map((item) => item.conflict_id), editText,
      );
      if (action === 'delete') await queueGrowthNoteDelete(selected);
      setEditText('');
      setDeleteApproved(false);
      await load();
      setMessage(t('Growth.note_queued'));
    } catch {
      setMessage(t('Growth.note_queue_failed'));
    } finally {
      setWorking(false);
    }
  }

  return <div className="growth-page__sync-notes">
    <h3>{t('Growth.note_workspace_title')}</h3>
    <p className="growth-page__hint">{t('Growth.note_workspace_hint')}</p>
    {notes.length === 0 ? <p>{t('Growth.note_workspace_empty')}</p> : <>
      <label htmlFor="growth-synced-note">{t('Growth.note_select')}</label>
      <select id="growth-synced-note" value={`${selected?.account_id}:${selected?.note_id}`}
        onChange={(event) => {
          setSelectedId(event.target.value);
          setEditText(''); setDeleteApproved(false); setMessage('');
        }}>
        {notes.map((note) => <option key={`${note.account_id}:${note.note_id}`}
          value={`${note.account_id}:${note.note_id}`}>
          {note.note.content_text.slice(0, 48)} · r{note.revision}
          {note.deleted_at ? ` · ${t('Growth.note_deleted')}` : ''}
        </option>)}
      </select>
      {selected && <>
        <p>{t('Growth.note_current_revision', { revision: selected.revision })}
          {' · '}{selected.note_id}</p>
        <div className="growth-page__originals">
          <div><strong>{t('Growth.note_server_text')}</strong>
            <pre>{selected.note.content_text}</pre></div>
          {unresolved.map((conflict) => <div key={conflict.conflict_id}>
            <p>{t('Growth.note_conflict', { id: conflict.conflict_id })}</p>
            <strong>{t('Growth.note_base_text', { revision: conflict.base_revision })}</strong>
            <pre>{selected.revisions.find((version) => version.revision === conflict.base_revision)
              ?.content_text ?? t('Growth.note_base_missing')}</pre>
            <strong>{t('Growth.note_local_text')}</strong>
            <pre>{conflict.proposed_text}</pre>
          </div>)}
          {selected.drafts.filter((draft) => draft.state === 'recovery').map((draft) =>
            <div key={draft.operation_id}>
              <strong>{t('Growth.note_recovery_text')}</strong><pre>{draft.content_text}</pre>
            </div>)}
        </div>
        <details><summary>{t('Growth.note_versions')}</summary>
          {selected.revisions.map((version) => <div key={version.revision}>
            <strong>r{version.revision}</strong><pre>{version.content_text}</pre>
          </div>)}
        </details>
        {selected.deleted_at ? <p>{t('Growth.note_deleted_hint')}</p> : <>
          <label htmlFor="growth-sync-note-edit">{unresolved.length
            ? t('Growth.note_merge_text') : t('Growth.note_revision_text')}</label>
          <textarea id="growth-sync-note-edit" value={editText}
            onChange={(event) => setEditText(event.target.value)}
            maxLength={100000} disabled={disabled || working || pending} />
          <button type="button" disabled={disabled || working || pending || baseMissing
            || !editText.trim()}
            onClick={() => { void queue(unresolved.length ? 'resolve' : 'revision'); }}>
            {unresolved.length ? t('Growth.note_queue_merge') : t('Growth.note_queue_revision')}
          </button>
          <label className="growth-page__approval"><input type="checkbox"
            checked={deleteApproved} onChange={(event) => setDeleteApproved(event.target.checked)}
            disabled={disabled || working || pending} />{t('Growth.note_delete_approve')}</label>
          <button type="button" disabled={disabled || working || pending || !deleteApproved}
            onClick={() => { void queue('delete'); }}>{t('Growth.note_queue_delete')}</button>
        </>}
        {selected.drafts.map((draft) => <div key={draft.operation_id}>
          <strong>{t('Growth.note_draft_state', { state: draft.state })}</strong>
          <p>{draft.operation_id}</p><pre>{draft.content_text}</pre>
        </div>)}
      </>}
    </>}
    {message && <p role="status">{message}</p>}
  </div>;
}

export function GrowthSourceExcerptApproval({ accountId, sources, disabled }: {
  accountId: string; sources: GrowthModelSources; disabled: boolean;
}) {
  const { t } = useTranslation();
  const available = sources.sources.filter((item) => item.deleted === 0);
  const [sourceRefId, setSourceRefId] = useState('');
  const [startLine, setStartLine] = useState(1);
  const [endLine, setEndLine] = useState(20);
  const [preview, setPreview] = useState<GrowthSourceExcerptPreview | null>(null);
  const [approved, setApproved] = useState(false);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState('');
  const selectedRefId = available.some((item) => item.source_ref_id === sourceRefId)
    ? sourceRefId : (available[0]?.source_ref_id ?? '');

  async function showPreview() {
    if (!selectedRefId) return;
    setWorking(true); setPreview(null); setApproved(false); setMessage('');
    try {
      setPreview(await previewGrowthSourceExcerpt(selectedRefId, startLine, endLine));
    } catch {
      setMessage(t('Growth.excerpt_preview_failed'));
    } finally {
      setWorking(false);
    }
  }

  async function approve() {
    if (!preview || !approved) return;
    setWorking(true); setMessage('');
    try {
      await queueGrowthSourceExcerpt(preview, accountId);
      setPreview(null); setApproved(false);
      setMessage(t('Growth.excerpt_queued'));
    } catch {
      setMessage(t('Growth.excerpt_queue_failed'));
    } finally {
      setWorking(false);
    }
  }

  if (available.length === 0) return null;
  return <div className="growth-page__sync-notes">
    <h3>{t('Growth.excerpt_title')}</h3>
    <p className="growth-page__hint">{t('Growth.excerpt_hint')}</p>
    <label htmlFor="growth-excerpt-source">{t('Growth.excerpt_source')}</label>
    <select id="growth-excerpt-source" value={selectedRefId} disabled={disabled || working}
      onChange={(event) => {
        setSourceRefId(event.target.value); setPreview(null); setApproved(false);
      }}>
      {available.map((source) => <option key={source.source_ref_id}
        value={source.source_ref_id}>{source.relative_path}</option>)}
    </select>
    <label htmlFor="growth-excerpt-start">{t('Growth.excerpt_start')}</label>
    <input id="growth-excerpt-start" type="number" min={1} value={startLine}
      onChange={(event) => { setStartLine(Number(event.target.value));
        setPreview(null); setApproved(false); }} />
    <label htmlFor="growth-excerpt-end">{t('Growth.excerpt_end')}</label>
    <input id="growth-excerpt-end" type="number" min={1} value={endLine}
      onChange={(event) => { setEndLine(Number(event.target.value));
        setPreview(null); setApproved(false); }} />
    <button type="button" disabled={disabled || working || !selectedRefId
      || startLine < 1 || endLine < startLine || endLine - startLine >= 120}
      onClick={() => { void showPreview(); }}>{t('Growth.excerpt_preview')}</button>
    {preview && <div>
      <p>{t('Growth.excerpt_exact', { path: preview.relative_path,
        start: preview.start_line, end: preview.end_line })}</p>
      <pre className="growth-page__payload">{preview.content_text}</pre>
      <label className="growth-page__approval"><input type="checkbox"
        checked={approved} onChange={(event) => setApproved(event.target.checked)} />
        {t('Growth.excerpt_approve')}</label>
      <button type="button" disabled={disabled || working || !approved}
        onClick={() => { void approve(); }}>{t('Growth.excerpt_queue')}</button>
    </div>}
    {message && <p role="status">{message}</p>}
  </div>;
}

export function GrowthSyncTasks({ refreshKey, disabled, bindings, access, onBindingAdded }: {
  refreshKey: GrowthSyncSummary | null; disabled: boolean; bindings: GrowthBinding[];
  access: GrowthCloudAccess | null; onBindingAdded: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const [workspace, setWorkspace] = useState<GrowthSyncWorkspace | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [answerText, setAnswerText] = useState('');
  const [parentAttemptId, setParentAttemptId] = useState('');
  const [hintLevel, setHintLevel] = useState(0);
  const [progress, setProgress] = useState('');
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState('');
  const [feedbackAttemptId, setFeedbackAttemptId] = useState('');
  const [feedbackProvider, setFeedbackProvider] = useState('');
  const [syncedFeedbackPreview, setSyncedFeedbackPreview] =
    useState<GrowthSyncedFeedbackPreview | null>(null);
  const [feedbackApproved, setFeedbackApproved] = useState(false);
  const [feedbackResult, setFeedbackResult] = useState<Record<string, unknown> | null>(null);
  const [sourceLocations, setSourceLocations] = useState<GrowthSourceLocation[] | null>(null);
  const [sourceLocationError, setSourceLocationError] = useState(false);
  const [syncedRepositoryPath, setSyncedRepositoryPath] = useState('');
  const [journey, setJourney] = useState<GrowthTaskJourney | null>(null);
  const [journeyError, setJourneyError] = useState(false);
  const load = useCallback(async () => {
    setWorkspace(await getGrowthSyncWorkspace());
  }, []);
  useEffect(() => { void load().catch(() => setMessage(t('Growth.task_workspace_failed'))); }, [load, refreshKey, t]);
  const tasks = workspace?.tasks.filter((task) => !refreshKey || task.account_id === refreshKey.account_id) ?? [];
  const selected = tasks.find((task) => `${task.account_id}:${task.task_id}` === selectedId)
    ?? tasks[0];
  const boundRepository = bindings.find((binding) => binding.project_id === selected?.project_id);

  async function verifySyncedSources() {
    if (!selected || !boundRepository) return;
    setSourceLocations(null); setSourceLocationError(false);
    try {
      const result = await getGrowthSyncedSourceLocations(
        selected.task_id, boundRepository.binding_id);
      setSourceLocations(result.locations);
    } catch { setSourceLocationError(true); }
  }

  async function bindSyncedRepository() {
    if (!selected || !syncedRepositoryPath.trim()) return;
    setWorking(true); setMessage('');
    try {
      await bindGrowthSyncedRepository(selected.task_id, syncedRepositoryPath.trim());
      setSyncedRepositoryPath('');
      await onBindingAdded();
      setMessage(t('Growth.source_binding_done'));
    } catch { setMessage(t('Growth.source_binding_failed')); }
    finally { setWorking(false); }
  }

  async function loadJourney() {
    if (!access || !selected) return;
    setJourney(null); setJourneyError(false);
    try { setJourney(await getGrowthTaskJourney(access, selected.task_id)); }
    catch { setJourneyError(true); }
  }
  const feedbackCandidates = selected?.attempts.filter((attempt) =>
    attempt.status !== 'feedback_ready') ?? [];
  const selectedFeedbackAttempt = feedbackCandidates.find((attempt) =>
    attempt.attempt_id === feedbackAttemptId) ?? feedbackCandidates.at(-1);
  const draftBlocked = selected?.local_draft?.state === 'conflict'
    || selected?.local_draft?.state === 'recovery';
  const afterAttemptId = selected?.local_draft
    && selected.local_draft.local_revision >= (selected.remote_draft?.revision ?? 0)
    ? selected.local_draft.submitted_attempt_id
    : selected?.remote_draft?.submitted_attempt_id ?? null;
  const progressChoices: Record<string, string[]> = {
    ready: ['ready', 'in_progress', 'dismissed'],
    in_progress: ['in_progress', 'paused', 'completed', 'dismissed'],
    paused: ['paused', 'in_progress', 'dismissed'],
    completed: ['completed', 'in_progress'],
    dismissed: ['dismissed', 'in_progress'],
  };

  async function queue(action: 'draft' | 'rebase' | 'answer' | 'progress') {
    if (!selected) return;
    setWorking(true);
    setMessage('');
    try {
      if (action === 'draft') await queueGrowthTaskDraft(selected, answerText, afterAttemptId);
      if (action === 'rebase') await rebaseGrowthTaskDraft(
        selected, answerText, selected.remote_draft?.submitted_attempt_id ?? null,
      );
      if (action === 'answer') await queueGrowthTaskAnswer(
        selected, answerText, parentAttemptId || null, hintLevel,
      );
      if (action === 'progress') await queueGrowthTaskProgress(
        selected, progress || selected.task.progress,
      );
      setAnswerText('');
      await load();
      setMessage(t('Growth.task_queued'));
    } catch {
      setMessage(t('Growth.task_queue_failed'));
    } finally {
      setWorking(false);
    }
  }

  async function previewSyncedFeedback() {
    if (!access || !selectedFeedbackAttempt || !feedbackProvider.trim()) return;
    setWorking(true); setMessage(''); setSyncedFeedbackPreview(null);
    setFeedbackApproved(false);
    try {
      setSyncedFeedbackPreview(await previewGrowthSyncedFeedback(
        access, selectedFeedbackAttempt.attempt_id, feedbackProvider.trim(),
      ));
    } catch {
      setMessage(t('Growth.feedback_preview_failed'));
    } finally {
      setWorking(false);
    }
  }

  async function sendSyncedFeedback() {
    if (!access || !selectedFeedbackAttempt || !syncedFeedbackPreview
      || !feedbackApproved) return;
    setWorking(true); setMessage('');
    try {
      const result = await submitGrowthSyncedFeedback(
        access, selectedFeedbackAttempt.attempt_id, feedbackProvider.trim(),
        syncedFeedbackPreview.approval_hash,
      );
      setFeedbackResult(result.feedback);
      setSyncedFeedbackPreview(null); setFeedbackApproved(false);
      await load();
      setMessage(t('Growth.feedback_done'));
    } catch {
      setMessage(t('Growth.feedback_failed'));
    } finally {
      setWorking(false);
    }
  }

  return <div className="growth-page__sync-notes">
    <h3>{t('Growth.task_workspace_title')}</h3>
    <p className="growth-page__hint">{t('Growth.task_workspace_hint')}</p>
    {tasks.length === 0 ? <p>{t('Growth.task_workspace_empty')}</p> : <>
      <label htmlFor="growth-synced-task">{t('Growth.task_select')}</label>
      <select id="growth-synced-task" value={`${selected?.account_id}:${selected?.task_id}`}
        onChange={(event) => {
          const next = tasks.find((task) => `${task.account_id}:${task.task_id}`
            === event.target.value);
          setSelectedId(event.target.value);
          setAnswerText(''); setHintLevel(0); setMessage('');
          setFeedbackAttemptId(''); setSyncedFeedbackPreview(null);
          setFeedbackResult(null);
          setSourceLocations(null); setSourceLocationError(false);
          setJourney(null); setJourneyError(false);
          setParentAttemptId(next?.remote_draft?.submitted_attempt_id ?? '');
          setProgress(next?.task.progress ?? 'in_progress');
        }}>
        {tasks.map((task) => <option key={`${task.account_id}:${task.task_id}`}
          value={`${task.account_id}:${task.task_id}`}>
          {task.task.question.slice(0, 60)} · {task.task.progress}
        </option>)}
      </select>
      {selected && <>
        <p>{t('Growth.task_current', { revision: selected.revision,
          progress: selected.task.progress })}</p>
        <pre>{selected.task.question}</pre>
        {(selected.approved_sources ?? []).map((source) => <div key={source.source_ref_id}>
          <p><code>{source.relative_path}</code> · <code>{source.blob_hash.slice(0, 12)}</code></p>
          {source.approved_excerpt && <>
            <p>{t('Growth.excerpt_read', { start: source.approved_excerpt.start_line,
              end: source.approved_excerpt.end_line })}</p>
            <pre className="growth-page__payload">{source.approved_excerpt.content_text}</pre>
          </>}
        </div>)}
        {selected.approved_sources?.some((source) => source.approved_excerpt) &&
          !bindings.some((binding) => binding.project_id === selected.project_id) &&
          <p role="status">{t('Growth.excerpt_unbound')}</p>}
        {selected.project_id && !boundRepository && <div className="growth-page__sync-notes">
          <label htmlFor="growth-synced-repository-path">{t('Growth.source_binding_path')}</label>
          <input id="growth-synced-repository-path" value={syncedRepositoryPath}
            onChange={(event) => setSyncedRepositoryPath(event.target.value)}
            disabled={disabled || working} maxLength={4096} />
          <button type="button" disabled={disabled || working || !syncedRepositoryPath.trim()}
            onClick={() => { void bindSyncedRepository(); }}>
            {t('Growth.source_bind')}</button>
        </div>}
        {boundRepository && selected.approved_sources.length > 0 && <>
          <button type="button" disabled={disabled || working}
            onClick={() => { void verifySyncedSources(); }}>
            {t('Growth.source_verify')}</button>
          {sourceLocationError && <p role="alert">{t('Growth.source_verify_failed')}</p>}
          {sourceLocations?.map((location) => <p key={location.source_ref_id}
            role="status"><code>{location.relative_path}</code> ·
            {' '}{t(`Growth.source_${location.status}`)}
            {location.status === 'matched' && location.local_path &&
              <> · <code>{location.local_path}</code></>}</p>)}
        </>}
        {access && <div className="growth-page__sync-notes">
          <button type="button" disabled={disabled || working}
            onClick={() => { void loadJourney(); }}>{t('Growth.journey_open')}</button>
          {journeyError && <p role="alert">{t('Growth.journey_failed')}</p>}
          {journey && <section aria-labelledby="growth-journey-title">
            <h4 id="growth-journey-title">{t('Growth.journey_title')}</h4>
            {journey.progress_events.map((event, index) => <p key={index}>
              {event.payload.from} → {event.payload.to} · {event.committed_at}</p>)}
            {journey.timeline.map((entry) => <div key={entry.evidence_id}>
              <strong>{entry.source_type === 'attempt'
                ? t('Growth.journey_answer') : t('Growth.journey_note')}</strong>
              <pre>{entry.content_text}</pre><small>{entry.created_at}</small>
            </div>)}
            {journey.next_question && <div>
              <strong>{t('Growth.journey_next_question')}</strong>
              <p>{journey.next_question.question}</p>
              <small>{journey.next_question.due_at}</small>
            </div>}
            <p>{t('Growth.journey_memory_refs', { count: journey.memory_refs.length })}</p>
          </section>}
        </div>}
        {selected.remote_draft && <div><strong>{t('Growth.task_remote_draft')}</strong>
          <pre>{selected.remote_draft.content_text}</pre></div>}
        {selected.local_draft && <div><strong>{t('Growth.task_local_draft', {
          state: selected.local_draft.state,
        })}</strong><pre>{selected.local_draft.content_text}</pre></div>}
        {selected.pending_attempts.map((attempt) => <div key={attempt.attempt_id}>
          <strong>{t('Growth.task_pending_answer', { state: attempt.state })}</strong>
          <pre>{attempt.answer_text}</pre>
        </div>)}
        {selected.blocked_operations.map((operation) => <div key={operation.operation_id}>
          <strong>{t('Growth.task_blocked_operation', {
            code: operation.response?.code ?? 'CONFLICT',
          })}</strong>
          <pre>{operation.payload.content_text ?? operation.payload.answer_text
            ?? operation.payload.progress ?? ''}</pre>
        </div>)}
        <details><summary>{t('Growth.task_answer_history')}</summary>
          {selected.attempts.map((attempt) => <div key={attempt.attempt_id}>
            <strong>{attempt.attempt_id}</strong><pre>{attempt.answer_text}</pre>
            {attempt.feedback && <GrowthFeedback raw={JSON.stringify(attempt.feedback)} />}
          </div>)}
        </details>
        {access && selectedFeedbackAttempt
          && <div className="growth-page__sync-notes">
            <label htmlFor="growth-sync-feedback-attempt">{t('Growth.task_feedback_attempt')}</label>
            <select id="growth-sync-feedback-attempt"
              value={selectedFeedbackAttempt.attempt_id}
              onChange={(event) => {
                setFeedbackAttemptId(event.target.value);
                setSyncedFeedbackPreview(null); setFeedbackApproved(false);
              }} disabled={disabled || working}>
              {feedbackCandidates.map((attempt) => <option key={attempt.attempt_id}
                value={attempt.attempt_id}>{attempt.attempt_id}</option>)}
            </select>
            <label htmlFor="growth-sync-feedback-provider">{t('Growth.task_feedback_provider')}</label>
            <input id="growth-sync-feedback-provider" value={feedbackProvider}
              onChange={(event) => {
                setFeedbackProvider(event.target.value);
                setSyncedFeedbackPreview(null); setFeedbackApproved(false);
              }} disabled={disabled || working} />
            <button type="button" disabled={disabled || working || !feedbackProvider.trim()}
              onClick={() => { void previewSyncedFeedback(); }}>
              {t('Growth.preview_feedback')}</button>
            {syncedFeedbackPreview && <>
              <p>{syncedFeedbackPreview.provider_host} · {syncedFeedbackPreview.model_name}</p>
              <pre className="growth-page__payload">{syncedFeedbackPreview.payload_text}</pre>
              <label className="growth-page__approval"><input type="checkbox"
                checked={feedbackApproved}
                onChange={(event) => setFeedbackApproved(event.target.checked)}
                disabled={disabled || working} />{t('Growth.task_feedback_approve')}</label>
              <button type="button" disabled={disabled || working || !feedbackApproved}
                onClick={() => { void sendSyncedFeedback(); }}>
                {t('Growth.send_feedback')}</button>
            </>}
            {feedbackResult && <GrowthFeedback raw={JSON.stringify(feedbackResult)} />}
          </div>}
        {draftBlocked && <p role="status">{t('Growth.task_conflict_preserved')}</p>}
        <label htmlFor="growth-sync-task-answer">{t('Growth.task_answer_text')}</label>
        <textarea id="growth-sync-task-answer" value={answerText}
          onChange={(event) => setAnswerText(event.target.value)} maxLength={20000}
          disabled={disabled || working || selected.local_draft?.state === 'recovery'
            || Boolean(selected.deleted_at)} />
        <button type="button" disabled={disabled || working
          || Boolean(selected.deleted_at)}
          onClick={() => setAnswerText(selected.local_draft?.content_text
            ?? selected.remote_draft?.content_text ?? '')}>
          {t('Growth.task_load_draft')}
        </button>
        <button type="button" disabled={disabled || working || Boolean(draftBlocked)
          || Boolean(selected.deleted_at)}
          onClick={() => { void queue('draft'); }}>{t('Growth.task_save_draft')}</button>
        {selected.local_draft?.state === 'conflict' && <button type="button"
          disabled={disabled || working || Boolean(selected.deleted_at)
            || selected.pending_operations.length > 0}
          onClick={() => { void queue('rebase'); }}>{t('Growth.task_rebase_draft')}</button>}
        <label htmlFor="growth-sync-task-parent">{t('Growth.task_parent_answer')}</label>
        <select id="growth-sync-task-parent" value={parentAttemptId}
          onChange={(event) => setParentAttemptId(event.target.value)}
          disabled={disabled || working || Boolean(draftBlocked)}>
          <option value="">{t('Growth.task_new_answer')}</option>
          {selected.attempts.map((attempt) => <option key={attempt.attempt_id}
            value={attempt.attempt_id}>{attempt.attempt_id}</option>)}
          {selected.pending_attempts.map((attempt) => <option key={attempt.attempt_id}
            value={attempt.attempt_id}>{attempt.attempt_id}</option>)}
        </select>
        <label htmlFor="growth-sync-task-hint">{t('Growth.task_hint_level')}</label>
        <select id="growth-sync-task-hint" value={hintLevel}
          onChange={(event) => setHintLevel(Number(event.target.value))}
          disabled={disabled || working || Boolean(draftBlocked)}>
          {[0, 1, 2, 3].map((level) => <option key={level} value={level}>{level}</option>)}
        </select>
        <button type="button" disabled={disabled || working || Boolean(draftBlocked)
          || Boolean(selected.deleted_at) || !answerText.trim()}
          onClick={() => { void queue('answer'); }}>{t('Growth.task_submit_answer')}</button>
        <label htmlFor="growth-sync-task-progress">{t('Growth.task_progress')}</label>
        <select id="growth-sync-task-progress" value={progress || selected.task.progress}
          onChange={(event) => setProgress(event.target.value)}
          disabled={disabled || working || selected.pending_operations.length > 0}>
          {(progressChoices[selected.task.progress] ?? [selected.task.progress]).map((value) =>
            <option key={value} value={value}>{t(`Growth.progress_${value}`)}</option>)}
        </select>
        <button type="button" disabled={disabled || working || Boolean(selected.deleted_at)
          || selected.pending_operations.length > 0
          || (progress || selected.task.progress) === selected.task.progress}
          onClick={() => { void queue('progress'); }}>{t('Growth.task_queue_progress')}</button>
      </>}
    </>}
    {message && <p role="status">{message}</p>}
  </div>;
}
