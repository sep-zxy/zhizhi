import { useEffect, useRef, useState, type FormEvent } from 'react';
import {
  listGrowthLocalReviews, listGrowthDueReviews, submitGrowthReview, queueGrowthReview, getGrowthTaskChain, syncGrowthCloud,
  type GrowthCloudAccess, type GrowthDueReview, type GrowthReviewRating,
} from '../api/growth';
import { useTranslation } from '../i18n/useTranslation';

export default function GrowthReviewsPanel({ access, disabled, projectId = '', deviceId }: {
  access: GrowthCloudAccess | null; disabled: boolean; projectId?: string; deviceId?: string;
}) {
  const { t } = useTranslation();
  const [due, setDue] = useState<GrowthDueReview[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [answerText, setAnswerText] = useState('');
  const [rating, setRating] = useState<GrowthReviewRating | ''>('');
  const [hintLevel, setHintLevel] = useState(0);
  const [revealed, setRevealed] = useState(false);
  const [back, setBack] = useState({ answer: '', explanation: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const pending = useRef<{ key: string; operationId: string; reviewId: string } | null>(null);
  const selected = due.find((item) => item.task_id === selectedId) ?? due[0];

  async function load() {
    let result: { due: GrowthDueReview[] };
    try {
      result = access ? await listGrowthDueReviews(access) : await listGrowthLocalReviews();
      if (access) result.due = result.due.map((item) => ({ ...item, account_id: access.account_id }));
    }
    catch (reason) {
      if (!access) throw reason;
      result = await listGrowthLocalReviews(access.account_id);
    }
    setDue(result.due.filter((item) => !projectId || item.project_id === projectId));
  }

  useEffect(() => {
    void load().catch(() => setError(t('Growth.review_load_failed')));
    // The account changes only after a successful cloud sync.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [access?.account_id, projectId, t]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!selected || !(selected.account_id ?? access?.account_id) || !answerText.trim() || !rating || !revealed ||
      (hintLevel > 0 && ['easy', 'good'].includes(rating))) return;
    setBusy(true);
    setError('');
    setNotice('');
    const key = JSON.stringify([
      access?.account_id, selected.task_id, selected.revision,
      answerText.trim(), rating, hintLevel,
    ]);
    if (pending.current?.key !== key) {
      pending.current = {
        key, operationId: crypto.randomUUID(), reviewId: crypto.randomUUID(),
      };
    }
    try {
      const online = access && access.account_id === (selected.account_id ?? access.account_id);
      const result = online ? await submitGrowthReview(access, selected.task_id,
        pending.current.operationId, pending.current.reviewId, selected.revision,
        answerText, rating, hintLevel) : await queueGrowthReview(selected.card_id ?? selected.task_id, {
        account_id: (selected.account_id ?? access?.account_id)!, device_id: selected.device_id ?? deviceId,
        operation_id: pending.current.operationId, review_id: pending.current.reviewId,
        base_revision: selected.revision, answer_text: answerText, answer: rating, hint_level: hintLevel,
      });
      pending.current = null;
      setAnswerText('');
      setRating('');
      setHintLevel(0); setRevealed(false); setBack({ answer: '', explanation: '' });
      setNotice(result.sync_state === 'pending' ? '复习回答已保存在本机，等待同步。' : t('Growth.review_saved'));
      if (result.sync_state === 'pending' && online) {
        try {
          await syncGrowthCloud(access.cloud_url, access.access_token);
          setNotice(t('Growth.review_saved'));
        } catch { setNotice('复习回答已保存在本机。网络恢复后，可在设置中继续同步。'); }
      }
      await load();
    } catch {
      setError(t('Growth.review_submit_failed'));
    } finally {
      setBusy(false);
    }
  }

  async function reveal() {
    if (!selected || !answerText.trim()) return;
    setBusy(true); setError('');
    try {
      if (selected.back_answer) setBack({ answer: selected.back_answer, explanation: selected.back_explanation ?? '' });
      else { const chain = await getGrowthTaskChain(selected.task_id); setBack({ answer: chain.task.back_answer ?? chain.card?.expected_answer ?? '', explanation: chain.task.back_explanation ?? '' }); }
      setRevealed(true);
    } catch { setError('参考答案读取失败，请重试揭示答案。你的回答已保留。'); }
    finally { setBusy(false); }
  }
  return <div className="growth-page__panel growth-review" aria-labelledby="growth-reviews-title">
    <h3 id="growth-reviews-title">{t('Growth.review_title')}</h3>
    <p className="growth-page__hint">{t('Growth.review_hint')}</p>
    <>
      <button type="button" disabled={busy || disabled} onClick={() => {
        setError(''); void load().catch(() => setError(t('Growth.review_load_failed')));
      }}>{t('Growth.review_refresh')}</button>
      {due.length === 0 ? <p>{t('Growth.review_empty')}</p> : <>
        <label htmlFor="growth-review-task">{t('Growth.review_select')}</label>
        <select id="growth-review-task" value={selected?.task_id ?? ''}
          onChange={(event) => {
            setSelectedId(event.target.value); setAnswerText('');
            setRating(''); setHintLevel(0); setRevealed(false); setBack({ answer: '', explanation: '' }); setError(''); pending.current = null;
          }}>
          {due.map((item) => <option key={item.task_id} value={item.task_id}>
            {item.question.slice(0, 90)}
          </option>)}
        </select>
        {selected && <form className="growth-page__form" onSubmit={(event) => {
          void submit(event);
        }}>
<div className="growth-review__card"><span className="growth-page__eyebrow">{revealed ? '背面 · 核对理解' : '正面 · 独立回忆'}</span><p className="growth-review__question">{selected.question}</p></div>
          <p>{t('Growth.review_due_at')}: {new Date(selected.due_at).toLocaleString()}</p>
          <label htmlFor="growth-review-answer">{t('Growth.review_answer')}</label>
          <textarea id="growth-review-answer" value={answerText}
            disabled={busy || revealed} onChange={(event) => setAnswerText(event.target.value)}
            maxLength={20000} rows={4} required />
          {!revealed && <button type="button" disabled={busy || disabled || !answerText.trim()} onClick={() => { void reveal(); }}>揭示答案</button>}
          {revealed && <><div className="growth-page__reference"><strong>参考答案</strong><p>{back.answer || '此卡还没有参考答案，请结合源码反馈核对。'}</p><p>{back.explanation}</p></div>
          <label htmlFor="growth-review-hint">{t('Growth.review_hint_level')}</label>
          <select id="growth-review-hint" value={hintLevel}
            onChange={(event) => {
              const level = Number(event.target.value);
              setHintLevel(level);
              if (level > 0 && (rating === 'easy' || rating === 'good')) setRating('');
            }}>
            {[0, 1, 2, 3].map((level) => <option key={level} value={level}>{level}</option>)}
          </select>
          <label htmlFor="growth-review-rating">{t('Growth.review_rating')}</label>
          <select id="growth-review-rating" value={rating} required
            onChange={(event) => setRating(event.target.value as GrowthReviewRating | '')}>
            <option value="">{t('Growth.review_choose_rating')}</option>
            <option value="wrong">{t('Growth.review_wrong')}</option>
            <option value="hard">{t('Growth.review_hard')}</option>
            {hintLevel === 0 && <>
              <option value="good">{t('Growth.review_good')}</option>
              <option value="easy">{t('Growth.review_easy')}</option>
            </>}
          </select>
          <div className="growth-review__ratings">{([['wrong', '没想起'], ['hard', '费力'], ['good', '记住了'], ['easy', '很轻松']] as const).map(([value, label]) => <button key={value} type="button" className={rating === value ? '' : 'growth-page__secondary'} aria-pressed={rating === value} disabled={busy || (hintLevel > 0 && ['good', 'easy'].includes(value))} onClick={() => setRating(value)}>{label}</button>)}</div>
          <button disabled={busy || disabled || !answerText.trim() || !rating}>
            {busy ? t('Growth.review_saving') : t('Growth.review_submit')}
          </button></>}
        </form>}
      </>}
    </>
    {error && <p className="growth-page__error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </div>;
}
