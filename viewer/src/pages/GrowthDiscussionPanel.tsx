
import type { useGrowthWorkspace } from './useGrowthWorkspace';

export default function GrowthDiscussionPanel({ workspace }: { workspace: ReturnType<typeof useGrowthWorkspace> }) {
  const { view, state, projectId, setProjectId, setBindingId, setExploreSessionId, exploreMessages, setExploreMessages, exploreSuggestion, setExploreSuggestion, exploreInput, setExploreInput, setExploreProvider, explorePreview, setExplorePreview, exploreApproved, setExploreApproved, exploreBusy, exploreError, selectedLocalTopicId, setSelectedLocalTopicId, localTopicChains, setLocalTopicChains, localTopicError, selectedBinding, selectedExploreProvider, previewExplore, sendExplore, confirmExploreTopic, openLocalTopic, openTopicTask } = workspace;
  return <>{view === 'topics' && <section
          className="growth-page__panel growth-page__explore" aria-labelledby="growth-local-explore-title">
          <p className="growth-page__eyebrow">从一个问题开始</p>
          <h2 id="growth-local-explore-title">自由探索</h2>
          <p className="growth-page__hint">直接提问。聊出值得长期追踪的方向时，再由你确认成长主题。</p>
          {state?.projects.length && <>
            <label htmlFor="growth-explore-project">当前项目</label>
            <select id="growth-explore-project" value={projectId}
              onChange={(event) => { setProjectId(event.target.value); setBindingId('');
                setExploreSessionId(crypto.randomUUID()); setExploreMessages([]);
                setExploreSuggestion(null); setExplorePreview(null);
                setSelectedLocalTopicId(''); setLocalTopicChains([]); }}>
              {state.projects.map((item) => <option key={item.project_id} value={item.project_id}>
                {item.name}</option>)}
            </select>
          </>}
          {state?.local_explorations.filter((item) => item.project_id === projectId).length ?
            <div className="growth-page__actions">
              {state.local_explorations.filter((item) => item.project_id === projectId)
                .slice(0, 3).map((item) => <button key={item.session_id} type="button"
                  className="growth-page__secondary"
                  onClick={() => {
                    setExploreSessionId(item.session_id);
                    setExploreMessages(JSON.parse(item.messages_json));
                    setExploreSuggestion(item.topic_id ? null : item.suggestion_json
                      ? JSON.parse(item.suggestion_json) : null);
                    setExplorePreview(null); setExploreApproved(false); setExploreInput('');
                  }}>继续 {JSON.parse(item.messages_json)[0]?.content_text?.slice(0, 20) ?? '对话'}</button>)}
              <button type="button" className="growth-page__secondary" onClick={() => {
                setExploreSessionId(crypto.randomUUID()); setExploreMessages([]);
                setExploreSuggestion(null); setExplorePreview(null); setExploreInput('');
              }}>新话题</button>
            </div> : null}
          {exploreMessages.length > 0 && <div className="growth-page__conversation">
            {exploreMessages.map((item, index) => <div key={index}
              className={item.role === 'user' ? 'growth-page__conversation-user' : ''}>
              <strong>{item.role === 'user' ? '你' : '成长伴侣'}</strong>
              <p>{item.content_text}</p>
            </div>)}
          </div>}
          {exploreSuggestion && <div className="growth-page__topic-suggestion">
            <strong>建议成长主题：{exploreSuggestion.title}</strong>
            <p>{exploreSuggestion.reason}</p>
            <button type="button" disabled={exploreBusy}
              onClick={() => { void confirmExploreTopic(); }}>确认创建主题</button>
          </div>}
          {selectedBinding?.provider_names.length ? <>
            <label htmlFor="growth-explore-provider">模型</label>
            <select id="growth-explore-provider" value={selectedExploreProvider}
              onChange={(event) => { setExploreProvider(event.target.value);
                setExplorePreview(null); setExploreApproved(false); }}>
              {selectedBinding.provider_names.map((name) => <option key={name} value={name}>
                {name}</option>)}
            </select>
            <label htmlFor="growth-explore-input">你想弄懂什么？</label>
            <textarea id="growth-explore-input" value={exploreInput}
              onChange={(event) => { setExploreInput(event.target.value);
                setExplorePreview(null); setExploreApproved(false); }}
              placeholder="例如：这个项目的提交为什么要默认选择模型？" />
            <button type="button" disabled={exploreBusy || !exploreInput.trim()}
              onClick={() => { void previewExplore(); }}>预览发送内容</button>
            {explorePreview && <div className="growth-page__model">
              <p>将发送至 {explorePreview.provider_host} · {explorePreview.model_name}</p>
              <pre className="growth-page__payload">{explorePreview.payload_text}</pre>
              <label className="growth-page__approval"><input type="checkbox"
                checked={exploreApproved}
                onChange={(event) => setExploreApproved(event.target.checked)} />
                我确认发送这些内容</label>
              <button type="button" disabled={exploreBusy || !exploreApproved}
                onClick={() => { void sendExplore(); }}>发送并继续对话</button>
            </div>}
          </> : <p>请先为当前项目配置模型，再开始自由探索。</p>}
          {exploreError && <p role="alert" className="growth-page__error">{exploreError}</p>}
          {view === 'topics' && state?.local_topics.length ? <div className="growth-page__topic-history">
            <h3>我的成长主题</h3>
            <ul>{state.local_topics.filter((topic) =>
              state.local_topic_projects?.some((link) => link.topic_id === topic.topic_id
                && link.project_id === projectId)).map((topic) =>
              <li key={topic.topic_id}><button type="button"
                className="growth-page__secondary"
                onClick={() => { void openLocalTopic(topic.topic_id); }}>
                {topic.title}</button></li>)}</ul>
            {localTopicError && <p role="alert" className="growth-page__error">{localTopicError}</p>}
            {selectedLocalTopicId && <div>
              <h4>{state.local_topics.find((item) =>
                item.topic_id === selectedLocalTopicId)?.title}</h4>
              {localTopicChains.length === 0 && !localTopicError &&
                <p className="growth-page__hint">这个主题还没有关联学习卡；可继续上面的讨论。</p>}
              {localTopicChains.map((chain) => <article key={chain.task.task_id}
                className="growth-page__practice">
                <strong>{chain.opportunity.title}</strong>
                <p>{chain.task.question}</p>
                <p className="growth-page__hint">状态：{chain.task.progress} · 来源：{
                  chain.sources.map((source) => source.relative_path).join('、')}</p>
                {chain.attempts.length > 0 && <p>最近回答：{chain.attempts.at(-1)?.answer_text}</p>}
                {chain.notes.map((note) => <p key={note.note_id}>亲写笔记：{note.content_text}</p>)}
                <button type="button" onClick={() => openTopicTask(chain.task)}>
                  打开卡片与完整证据</button>
              </article>)}
            </div>}
          </div> : null}
        </section>}</>;
}
