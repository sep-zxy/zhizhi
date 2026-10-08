import { apiFetch, apiFetchResponse, ApiError } from './client';
export type Stage = 'front' | 'back' | 'understanding' | 'understanding_explanation' | 'prediction' | 'prediction_explanation' | 'completed';
export type Mark = 'none' | 'confused' | 'revisit';
export interface KnowledgeSource { source_ref_id: string; relative_path: string; commit_sha?: string; project_id?: string; project_name?: string; excerpt?: string; commit_title?: string }
export interface KnowledgeCandidate { summary?:string; date_span_days?:number; discussions?: {session_id:string;topic_id:string|null;suggestion:{title:string;reason:string}|null}[]; candidate_id: string; opportunity_ids?:string[]; title: string; reason: string; source_commit_shas: string[]; project_ids: string[]; evidence_count: number; suggested_card_id?: string; status: string; sources?: KnowledgeSource[]; source_refs?: KnowledgeSource[] }
export interface KnowledgeCard { wiki_draft_status?: 'draft'|'published'|null; wiki_updated_at?: string|null; card_id: string; concept_id: string; title: string; front_question: string; back_answer: string; back_explanation: string; card_version: number; followup_mark: Mark; learning_stage?: Stage; progress?: string; project_ids?: string[]; source_commit_shas?: string[]; material?: {front_context?:string;front_question:string;back_summary:string;back_mechanism:string;back_boundary:string;misconceptions:string[];source_refs:string[]} | null }
export interface KnowledgeQuestion { conclusion?: string; correct_option_id?: string; question_id: string; kind: 'understanding' | 'prediction'; version: number; stem: string; scenario: string; options: {id: string; text: string}[]; explanations: Record<string,string>; reasoning: string; boundary: string; source_refs: string[] }
export interface KnowledgeAnswer { option_id: string; correct_option_id: string; correct: boolean; explanation_confirmed: boolean; question_id: string; version: number; first_option_id?: string }
export interface WikiDraft { draft_id: string; card_id: string; title?: string; markdown: string; status?: string; original_markdown?: string; target_relative_path?: string; relative_path?: string; path?: string; published_at?: string }
export interface KnowledgeMessage { created_at?: string; role: string; content?: string; content_text?: string; stage?: string; question_id?: string; card_version?: number }
export interface KnowledgeDetail { card: KnowledgeCard; learning: {stage: Stage; answers: Partial<Record<'understanding'|'prediction', KnowledgeAnswer>>; option_drafts?: Record<string,string>; note_text: string; completed_at?: string}; questions: KnowledgeQuestion[]; sources: KnowledgeSource[]; wiki_draft: WikiDraft | null; conversation: KnowledgeMessage[] }
export interface KnowledgeIndex { candidates: KnowledgeCandidate[]; cards: KnowledgeCard[]; wiki_articles: {concept_id:string;article_id:string;relative_path:string;published_at:string;title:string;markdown?:string;version:number}[]; vault_path: string }
const root = '/api/growth/local/knowledge';
export function knowledgeGet<T>(path = '') { return apiFetch<T>(root + path); }
export function knowledgeSend<T>(path: string, body: unknown = {}, method = 'POST') { return apiFetch<T>(root + path, { method, body: JSON.stringify(body) }); }
export const getKnowledge = () => knowledgeGet<KnowledgeIndex>();
export async function getKnowledgeCard(id: string): Promise<KnowledgeDetail> {
  const detail = await knowledgeGet<KnowledgeDetail>('/cards/' + encodeURIComponent(id));
  detail.questions.forEach(question => { const answer = detail.learning.answers[question.kind]; if (answer && question.correct_option_id) answer.correct_option_id = question.correct_option_id; });
  detail.conversation = normalizeConversation(detail.conversation);
  return detail;
}
export function normalizeConversation(messages: unknown[]): KnowledgeMessage[] {
  return messages.flatMap(raw => {
    const item = raw as KnowledgeMessage & {user_text?:string;reply_text?:string;context?:{stage?:string;question_id?:string;card_version?:number}};
    if (item.user_text !== undefined) return [{role:'user',content_text:item.user_text,created_at:item.created_at,...item.context}, {role:'assistant',content_text:item.reply_text,created_at:item.created_at,...item.context}];
    return [item];
  });
}

export async function streamKnowledgeChat(
  cardId: string, body: Record<string, unknown>, signal: AbortSignal,
  onDelta: (text: string) => void,
): Promise<{conversation: KnowledgeMessage[]}> {
  const path = root + '/cards/' + encodeURIComponent(cardId) + '/chat';
  let response: Response;
  try {
    response = await apiFetchResponse(path + '/stream', {
      method: 'POST', body: JSON.stringify(body), signal, headers: {Accept: 'text/event-stream'},
    });
  } catch (error) {
    if (error instanceof ApiError && [404, 405].includes(error.status)) {
      return apiFetch(path, {method: 'POST', body: JSON.stringify(body), signal});
    }
    throw error;
  }
  if (!response.body) throw new Error('模型回复没有可读取的内容。');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, {stream: !chunk.done});
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let event = 'message'; const data: string[] = [];
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
        }
        if (!data.length) continue;
        const payload: unknown = JSON.parse(data.join('\n'));
        if (event === 'error') throw new Error(typeof payload === 'string' ? payload : '模型回复失败。');
        if (event === 'delta' && typeof payload === 'string') onDelta(payload);
        if (event === 'done') {
          const result = payload as {conversation?: KnowledgeMessage[]};
          if (!Array.isArray(result.conversation)) throw new Error('模型回复缺少完整对话。');
          await reader.cancel();
          return {conversation: result.conversation};
        }
      }
      if (chunk.done) throw new Error('回复连接提前结束，可使用相同请求重试。');
    }
  } finally {
    reader.releaseLock();
  }
}
