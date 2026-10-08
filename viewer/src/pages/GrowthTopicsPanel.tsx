import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createGrowthCloudTopic,
  createGrowthCloudTopicNote,
  listGrowthCloudTopics,
  mergeGrowthCloudTopics,
  readGrowthCloudTopic,
  splitGrowthCloudTopic,
  updateGrowthCloudTopic,
  type GrowthCloudAccess,
  type GrowthCloudSplitChild,
  type GrowthCloudTopic,
  type GrowthCloudTopicDetail,
} from '../api/growth';
import { useTranslation } from '../i18n/useTranslation';

type Assignment = 'parent' | 'a' | 'b';

export default function GrowthTopicsPanel({ access, disabled, refreshKey }: {
  access: GrowthCloudAccess | null; disabled: boolean; refreshKey: number;
}) {
  const { t } = useTranslation();
  const [topics, setTopics] = useState<GrowthCloudTopic[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<GrowthCloudTopicDetail | null>(null);
  const [title, setTitle] = useState('');
  const [renameTitle, setRenameTitle] = useState('');
  const [noteText, setNoteText] = useState('');
  const [noteTaskId, setNoteTaskId] = useState('');
  const [targetId, setTargetId] = useState('');
  const [mergeApproved, setMergeApproved] = useState(false);
  const [childA, setChildA] = useState('');
  const [childB, setChildB] = useState('');
  const [assignments, setAssignments] = useState<Record<string, Assignment>>({});
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const createIds = useRef<{ operation: string; topic: string } | null>(null);
  const noteIds = useRef<{ operation: string; note: string } | null>(null);
  const updateId = useRef('');
  const mergeId = useRef('');
  const splitIds = useRef<{ operation: string; a: string; b: string } | null>(null);
  const cloudUrl = access?.cloud_url;
  const accessToken = access?.access_token;
  const accountId = access?.account_id;

  const refresh = useCallback(async (id?: string) => {
    if (!cloudUrl || !accessToken || !accountId) return;
    const credentials = { cloud_url: cloudUrl, access_token: accessToken, account_id: accountId };
    const listed = await listGrowthCloudTopics(credentials);
    setTopics(listed.topics);
    const next = id || listed.topics[0]?.topic_id || '';
    setSelectedId(next);
    const loaded = next ? await readGrowthCloudTopic(credentials, next) : null;
    setDetail(loaded);
    setRenameTitle(loaded?.topic.title || '');
  }, [cloudUrl, accessToken, accountId]);

  useEffect(() => {
    void refresh().catch(() => setError(t('Growth.topics_load_failed')));
  }, [refresh, refreshKey, t]);

  async function run(action: () => Promise<void>) {
    setWorking(true); setError(''); setNotice('');
    try { await action(); setNotice(t('Growth.topics_saved')); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setWorking(false); }
  }

  function selectTopic(id: string) {
    setSelectedId(id); setDetail(null); setTargetId('');
    setNoteTaskId('');
    setMergeApproved(false); setAssignments({});
    updateId.current = ''; mergeId.current = ''; splitIds.current = null;
    if (access && id) void readGrowthCloudTopic(access, id).then((loaded) => {
      setDetail(loaded); setRenameTitle(loaded.topic.title);
    })
      .catch(() => setError(t('Growth.topics_load_failed')));
  }

  const topic = detail?.topic;
  const canEdit = Boolean(topic && !detail?.is_alias && topic.status === 'active');
  const selectedNoteTaskId = detail?.tasks.some((task) => task.task_id === noteTaskId)
    ? noteTaskId : (detail?.tasks.length === 1 ? detail.tasks[0].task_id : '');
  const mergeTargets = topics.filter((item) => item.topic_id !== selectedId
    && item.status === 'active' && !item.canonical_topic_id);
  const target = mergeTargets.find((item) => item.topic_id === targetId);
  const selectedTarget = target || mergeTargets[0];

  function setAssignment(key: string, value: Assignment) {
    setAssignments((current) => ({ ...current, [key]: value }));
    splitIds.current = null;
  }

  function assignmentControl(kind: 'note' | 'task', id: string, label: string) {
    const key = `${kind}:${id}`;
    return <div key={key}>
      <p>{label}</p>
      <select aria-label={`${t('Growth.topics_assign')} ${label}`}
        value={assignments[key] || 'parent'}
        onChange={(event) => setAssignment(key, event.target.value as Assignment)}
        disabled={working || disabled}>
        <option value="parent">{t('Growth.topics_keep_parent')}</option>
        <option value="a">{t('Growth.topics_child_a')}</option>
        <option value="b">{t('Growth.topics_child_b')}</option>
      </select>
    </div>;
  }

  return <div className="growth-page__sync-notes">
    <h3>{t('Growth.topics_title')}</h3>
    <p className="growth-page__hint">{t('Growth.topics_hint')}</p>
    {!access ? <p>{t('Growth.chat_connect_first')}</p> : <>
      <label htmlFor="growth-topic-create">{t('Growth.topics_new_title')}</label>
      <input id="growth-topic-create" value={title} maxLength={200}
        onChange={(event) => { setTitle(event.target.value); createIds.current = null; }}
        disabled={working || disabled} />
      <button type="button" disabled={!title.trim() || working || disabled}
        onClick={() => { void run(async () => {
          const ids = createIds.current || {
            operation: crypto.randomUUID(), topic: crypto.randomUUID(),
          };
          createIds.current = ids;
          await createGrowthCloudTopic(access, ids.operation, ids.topic, title.trim());
          createIds.current = null; setTitle(''); await refresh(ids.topic);
        }); }}>{t('Growth.topics_create')}</button>
      <button type="button" disabled={working || disabled}
        onClick={() => { void run(async () => { await refresh(selectedId); }); }}>
        {t('Growth.topics_refresh')}
      </button>
      {topics.length > 0 && <>
        <label htmlFor="growth-topic-select">{t('Growth.topics_select')}</label>
        <select id="growth-topic-select" value={selectedId}
          onChange={(event) => selectTopic(event.target.value)} disabled={working || disabled}>
          {topics.map((item) => <option key={item.topic_id} value={item.topic_id}>
            {item.title} · r{item.revision} · {item.status}
            {item.canonical_topic_id ? ` → ${item.canonical_topic_id.slice(0, 8)}` : ''}
          </option>)}
        </select>
      </>}
      {detail && topic && <>
        <p>{topic.title} · r{topic.revision} · {topic.status}</p>
        {detail.is_alias && <p role="status">{t('Growth.topics_alias')}:
          {' '}<code>{detail.canonical_topic_id}</code></p>}
        <div className="growth-page__originals">
          <div><strong>{t('Growth.topics_notes')}</strong>
            {detail.notes.map((item) => <div key={item.note_id}>
              <code>{item.note_id}</code><pre>{item.content_text}</pre>
            </div>)}</div>
          <div><strong>{t('Growth.topics_tasks')}</strong>
            {detail.tasks.map((item) => <div key={item.task_id}>
              <code>{item.task_id}</code><pre>{item.question}</pre>
            </div>)}</div>
        </div>
        {detail.children.length > 0 && <p>{t('Growth.topics_children')}:
          {' '}{detail.children.map((item) => item.title).join('、')}</p>}
        {detail.moves.length > 0 && <details><summary>{t('Growth.topics_moves')}</summary>
          <ul>{detail.moves.map((item) => <li key={item.move_id}>
            {item.action} · {item.entity_type} · {item.entity_id}
            {' · '}{item.from_topic_id} → {item.to_topic_id}
          </li>)}</ul>
        </details>}
        {canEdit && <>
          <label htmlFor="growth-topic-rename">{t('Growth.topics_rename_title')}</label>
          <input id="growth-topic-rename" value={renameTitle} maxLength={200}
            onChange={(event) => { setRenameTitle(event.target.value);
              updateId.current = ''; }} disabled={working || disabled} />
          <button type="button" disabled={!renameTitle.trim()
            || renameTitle.trim() === topic.title || working || disabled}
            onClick={() => { void run(async () => {
              const operation = updateId.current || crypto.randomUUID();
              updateId.current = operation;
              await updateGrowthCloudTopic(access, selectedId, operation,
                topic.revision, renameTitle.trim(), topic.status);
              updateId.current = ''; await refresh(selectedId);
            }); }}>{t('Growth.topics_rename_submit')}</button>
          <label htmlFor="growth-topic-note">{t('Growth.topics_note_text')}</label>
          <textarea id="growth-topic-note" value={noteText} maxLength={100000}
            onChange={(event) => { setNoteText(event.target.value); noteIds.current = null; }}
            disabled={working || disabled} />
          {detail.tasks.length > 0 && <>
            <label htmlFor="growth-topic-note-task">{t('Growth.topics_note_task')}</label>
            <select id="growth-topic-note-task" value={selectedNoteTaskId}
              onChange={(event) => { setNoteTaskId(event.target.value); noteIds.current = null; }}
              disabled={working || disabled}>
              <option value="">{t('Growth.topics_note_topic_only')}</option>
              {detail.tasks.map((task) => <option key={task.task_id} value={task.task_id}>
                {task.question.slice(0, 80)}
              </option>)}
            </select>
          </>}
          <button type="button" disabled={!noteText.trim() || working || disabled}
            onClick={() => { void run(async () => {
              const ids = noteIds.current || {
                operation: crypto.randomUUID(), note: crypto.randomUUID(),
              };
              noteIds.current = ids;
              await createGrowthCloudTopicNote(
                access, selectedId, ids.operation, ids.note, noteText.trim(),
                selectedNoteTaskId || null,
              );
              noteIds.current = null; setNoteText(''); await refresh(selectedId);
            }); }}>{t('Growth.topics_note_save')}</button>
        </>}
        {!detail.is_alias && <button type="button" disabled={working || disabled}
          onClick={() => { void run(async () => {
            const operation = updateId.current || crypto.randomUUID();
            updateId.current = operation;
            await updateGrowthCloudTopic(
              access, selectedId, operation, topic.revision, topic.title,
              topic.status === 'active' ? 'archived' : 'active',
            );
            updateId.current = ''; await refresh(selectedId);
          }); }}>{topic.status === 'active'
            ? t('Growth.topics_archive') : t('Growth.topics_reactivate')}</button>}
        {canEdit && mergeTargets.length > 0 && <div className="growth-page__sync-notes">
          <h3>{t('Growth.topics_merge')}</h3>
          <p className="growth-page__hint">{t('Growth.topics_merge_hint')}</p>
          <label htmlFor="growth-topic-target">{t('Growth.topics_target')}</label>
          <select id="growth-topic-target" value={selectedTarget?.topic_id || ''}
            onChange={(event) => { setTargetId(event.target.value);
              setMergeApproved(false); mergeId.current = ''; }}>
            {mergeTargets.map((item) => <option key={item.topic_id} value={item.topic_id}>
              {item.title} · r{item.revision}
            </option>)}
          </select>
          <label className="growth-page__approval"><input type="checkbox"
            checked={mergeApproved} onChange={(event) => setMergeApproved(event.target.checked)} />
            {t('Growth.topics_merge_approve')}
          </label>
          <button type="button" disabled={!mergeApproved || !selectedTarget || working || disabled}
            onClick={() => { void run(async () => {
              if (!selectedTarget) return;
              const operation = mergeId.current || crypto.randomUUID();
              mergeId.current = operation;
              await mergeGrowthCloudTopics(access, selectedId, selectedTarget.topic_id,
                operation, topic.revision, selectedTarget.revision);
              mergeId.current = ''; setMergeApproved(false);
              await refresh(selectedId);
            }); }}>{t('Growth.topics_merge_submit')}</button>
        </div>}
        {canEdit && <div className="growth-page__sync-notes">
          <h3>{t('Growth.topics_split')}</h3>
          <p className="growth-page__hint">{t('Growth.topics_split_hint')}</p>
          <label htmlFor="growth-topic-child-a">{t('Growth.topics_child_a')}</label>
          <input id="growth-topic-child-a" value={childA} maxLength={200}
            onChange={(event) => { setChildA(event.target.value); splitIds.current = null; }} />
          <label htmlFor="growth-topic-child-b">{t('Growth.topics_child_b')}</label>
          <input id="growth-topic-child-b" value={childB} maxLength={200}
            onChange={(event) => { setChildB(event.target.value); splitIds.current = null; }} />
          {detail.notes.map((item) => assignmentControl(
            'note', item.note_id, item.content_text.slice(0, 80),
          ))}
          {detail.tasks.map((item) => assignmentControl(
            'task', item.task_id, item.question.slice(0, 80),
          ))}
          <button type="button" disabled={!childA.trim() || !childB.trim()
            || childA.trim() === childB.trim() || working || disabled}
            onClick={() => { void run(async () => {
              const ids = splitIds.current || {
                operation: crypto.randomUUID(), a: crypto.randomUUID(), b: crypto.randomUUID(),
              };
              splitIds.current = ids;
              const children: GrowthCloudSplitChild[] = [
                { topic_id: ids.a, title: childA.trim(), note_ids: [], task_ids: [] },
                { topic_id: ids.b, title: childB.trim(), note_ids: [], task_ids: [] },
              ];
              for (const item of detail.notes) {
                const destination = assignments[`note:${item.note_id}`];
                if (destination === 'a') children[0].note_ids.push(item.note_id);
                if (destination === 'b') children[1].note_ids.push(item.note_id);
              }
              for (const item of detail.tasks) {
                const destination = assignments[`task:${item.task_id}`];
                if (destination === 'a') children[0].task_ids.push(item.task_id);
                if (destination === 'b') children[1].task_ids.push(item.task_id);
              }
              await splitGrowthCloudTopic(access, selectedId, ids.operation,
                topic.revision, children);
              splitIds.current = null; setChildA(''); setChildB('');
              setAssignments({}); await refresh(selectedId);
            }); }}>{t('Growth.topics_split_submit')}</button>
        </div>}
      </>}
    </>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="growth-page__error" role="alert">{error}
      {' '}{t('Growth.topics_retry_hint')}</p>}
  </div>;
}
