import { GrowthFeedback } from './GrowthSyncWorkspace';
import type { useGrowthWorkspace } from './useGrowthWorkspace';

export default function GrowthLearningPanel({ workspace }: { workspace: ReturnType<typeof useGrowthWorkspace> }) {
  const { t, busy, providerName, showExpectedAnswer, setShowExpectedAnswer, cloudModules, taskChain, materials, materialsPreview, materialsApproved, setMaterialsApproved, materialsError, quizAnswers, setQuizAnswers, quizReveals, answerText, setAnswerText, parentAttemptId, setParentAttemptId, noteText, setNoteText, feedbackPreview, feedbackApproved, setFeedbackApproved, feedbackTaskId, feedbackMessage, selectedTask, previewMaterials, startMaterials, revealMaterial, submitAnswer, requestHint, submitNote, changeTaskProgress, reviewFeedback, sendFeedback } = workspace;
  return <>{selectedTask && taskChain && <div className="growth-page__model"
            aria-labelledby="growth-card-title">
            <h3 id="growth-card-title">{t('Growth.card_title')}</h3>
            <div className="growth-page__card-intro">
              <p className="growth-page__eyebrow">正式知识卡 · v{selectedTask.card_version ?? 1} · 3–5 分钟 · {taskChain.card?.quiz_kind ?? 'guided'} / {taskChain.card?.exercise_kind ?? 'prediction'}</p>
              <h4>{taskChain.opportunity.title}</h4>
              <p>{taskChain.card?.context ?? taskChain.opportunity.reason}</p>
              <p className="growth-page__card-question">{selectedTask.front_question ?? selectedTask.question}</p>
              <p className="growth-page__hint">先观察真实代码，再自己解释或预测。回答后可获取基于源码的反馈。</p>
              <div className="growth-page__source-list">证据：{taskChain.sources.map((source) =>
                <code key={source.source_ref_id}>{source.relative_path}</code>)}</div>
              {taskChain.source_patch && <details className="growth-page__source-patch">
                <summary>查看本次源码变化</summary>
                <pre className="growth-page__payload">{taskChain.source_patch}</pre>
              </details>}
            </div>
            {selectedTask.module_id && <p>{t('Growth.card_module')}: {
              cloudModules.find((module) => module.module_id === selectedTask.module_id)?.name
              ?? selectedTask.module_id
            } · <code>{selectedTask.module_index_revision?.slice(0, 12)}</code></p>}
            <p>{t('Growth.card_progress')}: {t(`Growth.task_${selectedTask.progress}`)}</p>
            <div className="growth-page__actions">
              {(['ready', 'paused', 'completed', 'dismissed'].includes(selectedTask.progress))
                && <button disabled={busy} onClick={() => changeTaskProgress('in_progress')}>
                  {t('Growth.resume_card')}
                </button>}
              {selectedTask.progress === 'in_progress' && <>
                <button disabled={busy} onClick={() => changeTaskProgress('paused')}>
                  {t('Growth.pause_card')}
                </button>
                <button disabled={busy || !taskChain.attempts.some((attempt) =>
                  attempt.status === 'feedback_ready')} onClick={() => changeTaskProgress('completed')}>
                  {t('Growth.complete_card')}
                </button>
              </>}
            </div>
            <h4>{t('Growth.answers_title')}</h4>
            <div>
              <p>卡住时逐级查看提示，先自己想，再继续回答。</p>
              <button disabled={busy || taskChain.hints.length >= 3
                || taskChain.hints.length >= (taskChain.card?.hints.length ?? 3)
                || selectedTask.progress === 'completed' || selectedTask.progress === 'dismissed'}
                onClick={requestHint}>{t('Growth.request_hint')}</button>
              <ol>{taskChain.hints.map((hint) => <li key={hint.hint_id}>
                {hint.hint_text}
              </li>)}</ol>
            </div>
            <ul>{taskChain.attempts.map((attempt, index) => <li key={attempt.attempt_id}>
              <strong>{index === 0 ? '第一次回答' : '修正后的回答'}</strong>
              <p>{attempt.answer_text}</p>
              <small>{attempt.status} · {t('Growth.hint_level')}: {attempt.hint_level}</small>
              {attempt.feedback_json && <GrowthFeedback raw={attempt.feedback_json} />}
              {attempt.feedback_error && <p className="growth-page__error">
                {t('Growth.feedback_failed')}</p>}
              {(attempt.status === 'accepted' || attempt.status === 'feedback_failed')
                && <button disabled={busy || !!feedbackTaskId}
                  onClick={() => { void reviewFeedback(attempt.attempt_id); }}>
                  {t(attempt.status === 'feedback_failed'
                    ? 'Growth.retry_feedback' : 'Growth.preview_feedback')}
                </button>}
            </li>)}</ul>
            {taskChain.attempts.some((attempt) => attempt.status === 'feedback_ready') &&
              taskChain.card && <div className="growth-page__reference">
                <button type="button" onClick={() => setShowExpectedAnswer((value) => !value)}>
                  {showExpectedAnswer ? '收起参考思路' : '查看参考思路'}</button>
                {showExpectedAnswer && <><p>{selectedTask.back_answer || taskChain.card.expected_answer}</p>{selectedTask.back_explanation && <p>{selectedTask.back_explanation}</p>}</>}
                {taskChain.card.followups.length > 0 && <>
                  <strong>再想一步</strong>
                  <ol>{taskChain.card.followups.map((item) => <li key={item}>{item}</li>)}</ol>
                </>}
              </div>}
            {feedbackMessage && <p role="status" aria-live="polite">{feedbackMessage}</p>}
            {feedbackPreview && <div className="growth-page__model">
              <p>{t('Growth.model_destination')}: <strong>{feedbackPreview.provider_host}</strong>
                {' · '}{feedbackPreview.model_name}</p>
              <p>{t('Growth.feedback_payload')}</p>
              <pre className="growth-page__payload">{feedbackPreview.payload_text}</pre>
              <label className="growth-page__approval">
                <input type="checkbox" checked={feedbackApproved}
                  onChange={(event) => setFeedbackApproved(event.target.checked)} />
                {t('Growth.approve_model_send')}
              </label>
              <button disabled={busy || !!feedbackTaskId || !feedbackApproved}
                onClick={() => { void sendFeedback(); }}>
                {t('Growth.send_feedback')}
              </button>
            </div>}
            {selectedTask.progress !== 'completed' && selectedTask.progress !== 'dismissed'
              && <form onSubmit={submitAnswer} className="growth-page__form">
                <label htmlFor="growth-answer">{t('Growth.answer_label')}</label>
                <textarea id="growth-answer" value={answerText}
                  onChange={(event) => setAnswerText(event.target.value)} />
                {taskChain.attempts.length > 0 && <>
                  <label htmlFor="growth-parent-answer">{t('Growth.corrects_answer')}</label>
                  <select id="growth-parent-answer" value={parentAttemptId}
                    onChange={(event) => setParentAttemptId(event.target.value)}>
                    <option value="">{t('Growth.new_answer')}</option>
                    {taskChain.attempts.map((attempt) => <option key={attempt.attempt_id}
                      value={attempt.attempt_id}>{attempt.answer_text.slice(0, 80)}</option>)}
                  </select>
                </>}
                <button type="submit" disabled={busy || !answerText.trim()}>
                  {t('Growth.save_answer')}
                </button>
              </form>}
            <section className="growth-page__materials" aria-labelledby="growth-materials-title">
              <h4 id="growth-materials-title">AhaDiff 深入学习</h4>
              <p className="growth-page__hint">讲解、提示与迁移追问附属于当前知识卡，不计为额外卡片。</p>
              {(materials?.status === 'none' || materials?.status === 'failed') && <>
                <button type="button" disabled={!providerName || busy}
                  onClick={() => { void previewMaterials(); }}>预览生成范围</button>
                {materialsPreview && <div className="growth-page__model">
                  <p>将用 {materialsPreview.model_name} 生成讲解和练习；所选源码会发送至 {materialsPreview.provider_host}。</p>
                  <pre className="growth-page__payload">{materialsPreview.patch_text}</pre>
                  <label className="growth-page__approval"><input type="checkbox"
                    checked={materialsApproved}
                    onChange={(event) => setMaterialsApproved(event.target.checked)} />
                    我确认用这段变化生成材料</label>
                  <button type="button" disabled={!materialsApproved}
                    onClick={() => { void startMaterials(); }}>生成完整学习材料</button>
                </div>}
              </>}
              {(materials?.status === 'queued' || materials?.status === 'running') &&
                <p role="status">AhaDiff 正在生成讲解与迁移练习…</p>}
              {materials?.status === 'failed' && <>
                <p role="alert">完整材料生成失败：{materials.error}</p>
              </>}
              {materials?.status === 'ready' && <>
                <details><summary>精简讲解</summary>
                  <div className="growth-page__lesson">{materials.lesson?.split(/\n\s*\n/).map((block, index) =>
                    <p key={index}>{block}</p>)}</div>
                </details>
                <h5>迁移练习</h5>
                {materials.questions.map((question, index) => <div key={question.question_id}
                  className="growth-page__practice">
                  <p className="growth-page__eyebrow">练习 {index + 1} · {question.exercise_kind}</p>
                  <p>{question.question}</p>
                  {materials.attempts?.some((attempt) => attempt.question_id === question.question_id)
                    ? <p className="growth-page__hint">已作答并记入本地成长记录。</p> : <>
                      <textarea aria-label={`练习 ${index + 1} 的回答`}
                        value={quizAnswers[question.question_id] ?? ''}
                        onChange={(event) => setQuizAnswers((current) => ({
                          ...current, [question.question_id]: event.target.value,
                        }))} />
                      <button type="button" disabled={!quizAnswers[question.question_id]?.trim()}
                        onClick={() => { void revealMaterial(question.question_id); }}>
                        提交后查看参考答案</button>
                    </>}
                  {(question.expected_answer || quizReveals[question.question_id]) &&
                    <div className="growth-page__reference">
                      <strong>参考答案</strong>
                      <p>{question.expected_answer ?? quizReveals[question.question_id].expected_answer}</p>
                      <p>{question.explanation ?? quizReveals[question.question_id].explanation}</p>
                    </div>}
                  
                </div>)}
                {!!materials.misconceptions?.length && <details>
                  <summary>易错观点（模型候选，请结合源码核对）</summary>
                  <ul>{materials.misconceptions.map((item) => <li key={item.card_id}>
                    <p>{item.misconception}</p>
                    <details><summary>查看模型给出的纠正草稿</summary>
                      <p>{item.correction}</p>
                      <small>请先用本次源码验证；线索：{item.evidence_ref}</small>
                    </details>
                  </li>)}</ul>
                </details>}
              </>}
              {materialsError && <p role="alert" className="growth-page__error">{materialsError}</p>}
            </section>
            <h4>{t('Growth.notes_title')}</h4>
            {selectedTask.progress === 'completed' && <ul>
              <li>{t('Growth.note_prompt_fact')}</li>
              <li>{t('Growth.note_prompt_next')}</li>
            </ul>}
            <ul>{taskChain.notes.map((note) => <li key={note.note_id}>
              <p>{note.content_text}</p>
            </li>)}</ul>
            <form onSubmit={submitNote} className="growth-page__form">
              <label htmlFor="growth-note">{t('Growth.note_label')}</label>
              <textarea id="growth-note" value={noteText}
                onChange={(event) => setNoteText(event.target.value)} />
              <button type="submit" disabled={busy || !noteText.trim()}>
                {t('Growth.save_note')}
              </button>
            </form>
          </div>}</>;
}
