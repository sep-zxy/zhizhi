import { useCallback, useEffect, useRef, useState } from 'react';
import {
  decideGrowthChatSuggestion,
  generateGrowthChat,
  listGrowthChats,
  previewGrowthChat,
  readGrowthChat,
  sendGrowthChatMessage,
  startGrowthChat,
  type GrowthChat,
  type GrowthChatPreview,
  type GrowthChatSession,
} from '../api/growth';
import { useTranslation } from '../i18n/useTranslation';

interface Access { cloud_url: string; access_token: string; account_id: string }

export default function GrowthChatPanel({ access, projectId, disabled, onTopicConfirmed }: {
  access: Access | null; projectId: string | null; disabled: boolean;
  onTopicConfirmed: () => void;
}) {
  const { t } = useTranslation();
  const [sessions, setSessions] = useState<GrowthChatSession[]>([]);
  const [sessionId, setSessionId] = useState('');
  const [chat, setChat] = useState<GrowthChat | null>(null);
  const [useProject, setUseProject] = useState(false);
  const [message, setMessage] = useState('');
  const [providerName, setProviderName] = useState('');
  const [preview, setPreview] = useState<GrowthChatPreview | null>(null);
  const [approved, setApproved] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const pendingStart = useRef('');
  const pendingMessage = useRef('');
  const pendingGeneration = useRef<{ requestId: string; messageId: string } | null>(null);
  const cloudUrl = access?.cloud_url;
  const accessToken = access?.access_token;
  const accountId = access?.account_id;

  const refresh = useCallback(async (selected?: string) => {
    if (!cloudUrl || !accessToken || !accountId) return;
    const credentials = { cloud_url: cloudUrl, access_token: accessToken, account_id: accountId };
    const listed = await listGrowthChats(credentials);
    setSessions(listed.sessions);
    const next = selected || listed.sessions[0]?.session_id || '';
    setSessionId(next);
    setChat(next ? await readGrowthChat(credentials, next) : null);
  }, [cloudUrl, accessToken, accountId]);

  useEffect(() => {
    void refresh().catch(() => setError(t('Growth.chat_load_failed')));
  }, [refresh, t]);

  async function run(action: () => Promise<void>) {
    setWorking(true); setError(''); setNotice('');
    try { await action(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setWorking(false); }
  }

  function changeSession(next: string) {
    setSessionId(next); setChat(null); setPreview(null); setApproved(false);
    pendingGeneration.current = null;
    if (access && next) void readGrowthChat(access, next).then(setChat)
      .catch(() => setError(t('Growth.chat_load_failed')));
  }

  return <div className="growth-page__sync-notes">
    <h3>{t('Growth.chat_title')}</h3>
    <p className="growth-page__hint">{t('Growth.chat_hint')}</p>
    {!access ? <p>{t('Growth.chat_connect_first')}</p> : <>
      <label className="growth-page__approval"><input type="checkbox"
        checked={useProject} onChange={(event) => setUseProject(event.target.checked)}
        disabled={!projectId || disabled || working} />
        {t('Growth.chat_link_project')}
      </label>
      <button type="button" disabled={disabled || working}
        onClick={() => { void run(async () => {
          const id = pendingStart.current || crypto.randomUUID();
          pendingStart.current = id;
          await startGrowthChat(access, id, useProject ? projectId : null);
          pendingStart.current = '';
          await refresh(id); setPreview(null); setApproved(false);
        }); }}>{t('Growth.chat_new')}</button>
      {sessions.length > 0 && <>
        <label htmlFor="growth-chat-session">{t('Growth.chat_session')}</label>
        <select id="growth-chat-session" value={sessionId}
          onChange={(event) => changeSession(event.target.value)}>
          {sessions.map((item) => <option key={item.session_id} value={item.session_id}>
            {item.session_id.slice(0, 8)} · {item.updated_at}
          </option>)}
        </select>
      </>}
      {chat && <>
        <div aria-label={t('Growth.chat_history')}>
          {chat.messages.map((item) => <div key={item.message_id}>
            <strong>{item.role === 'user' ? t('Growth.chat_user') : t('Growth.chat_assistant')}</strong>
            {' · '}{item.origin}<pre>{item.content_text}</pre>
          </div>)}
        </div>
        <label htmlFor="growth-chat-message">{t('Growth.chat_message')}</label>
        <textarea id="growth-chat-message" maxLength={20000} value={message}
          onChange={(event) => setMessage(event.target.value)} disabled={working || disabled} />
        <button type="button" disabled={!message.trim() || working || disabled}
          onClick={() => { void run(async () => {
            const id = pendingMessage.current || crypto.randomUUID();
            pendingMessage.current = id;
            await sendGrowthChatMessage(access, sessionId, id, message.trim());
            pendingMessage.current = ''; setMessage('');
            setPreview(null); setApproved(false); pendingGeneration.current = null;
            await refresh(sessionId);
          }); }}>{t('Growth.chat_send')}</button>
        <label htmlFor="growth-chat-provider">{t('Growth.chat_provider')}</label>
        <input id="growth-chat-provider" value={providerName} maxLength={100}
          onChange={(event) => { setProviderName(event.target.value);
            setPreview(null); setApproved(false); pendingGeneration.current = null; }}
          disabled={working || disabled} />
        <button type="button" disabled={!providerName.trim() || working || disabled}
          onClick={() => { void run(async () => {
            setPreview(await previewGrowthChat(access, sessionId, providerName.trim()));
            setApproved(false); pendingGeneration.current = null;
          }); }}>{t('Growth.chat_preview')}</button>
        {preview && <>
          <p>{t('Growth.chat_destination')}: {preview.provider_host} / {preview.model_name}</p>
          <pre className="growth-page__payload">{preview.payload_text}</pre>
          <label className="growth-page__approval"><input type="checkbox"
            checked={approved} onChange={(event) => setApproved(event.target.checked)} />
            {t('Growth.chat_approve')}
          </label>
          <div className="growth-page__actions">
            <button type="button" disabled={!approved || working || disabled}
              onClick={() => { void run(async () => {
                const ids = pendingGeneration.current || {
                  requestId: crypto.randomUUID(), messageId: crypto.randomUUID(),
                };
                pendingGeneration.current = ids;
                const result = await generateGrowthChat(
                  access, sessionId, providerName.trim(), ids.requestId,
                  ids.messageId, preview.approval_hash,
                );
                if (result.sync_state === 'acked') {
                  pendingGeneration.current = null;
                  setPreview(null); setApproved(false);
                  await refresh(sessionId);
                } else setNotice(t('Growth.chat_queued'));
              }); }}>{t('Growth.chat_generate')}</button>
            <button type="button" disabled={working} onClick={() => {
              pendingGeneration.current = null; setPreview(null); setApproved(false);
            }}>{t('Growth.chat_reset')}</button>
          </div>
        </>}
        {chat.suggestions.map((item) => <div key={item.suggestion_id}>
          <strong>{item.status === 'pending'
            ? t('Growth.chat_suggestion') : t('Growth.chat_suggestion_record')}</strong>
          <p>{item.title} · {item.reason}</p>
          <p>{item.status}{item.topic_id ? ` · ${item.topic_id}` : ''}</p>
          {item.status === 'pending' && <div className="growth-page__actions">
            {(['confirm', 'reject'] as const).map((decision) => <button key={decision}
              type="button" disabled={working || disabled}
              onClick={() => { void run(async () => {
                const result = await decideGrowthChatSuggestion(
                  access, sessionId, item.suggestion_id, decision,
                );
                await refresh(sessionId);
                if (decision === 'confirm' && result.status === 'confirmed'
                  && result.topic_id) onTopicConfirmed();
              }); }}>{decision === 'confirm'
                ? t('Growth.chat_confirm') : t('Growth.chat_reject')}</button>)}
          </div>}
        </div>)}
      </>}
    </>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="growth-page__error" role="alert">{error}</p>}
  </div>;
}
