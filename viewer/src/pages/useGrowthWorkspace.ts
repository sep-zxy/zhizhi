import { useGrowthDraft } from './useGrowthDraft';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { getGrowthSyncWorkspace, queueGrowthTaskDraft, bindGrowthRepository, captureGrowthSnapshot, captureGrowthDevelopmentEvent, createGrowthCloudExport, createGrowthTask, createGrowthFeature, createGrowthProject, confirmGrowthLocalExploreTopic, decideGrowthTopic, getGrowthLocalState, getGrowthCommits, getGrowthDevelopmentEvents, getGrowthModelSources, getGrowthMaterials, getGrowthTaskChain, getGrowthUntracked, indexGrowthSnapshot, importGrowthProject, listGrowthCloudTopics, previewGrowthFeedback, previewGrowthLocalExplore, previewGrowthModelRequest, previewGrowthMaterials, rateGrowthMaterialCard, retryGrowthAnalysis, revealGrowthMaterialAnswer, requestGrowthHint, saveGrowthNote, sendGrowthLocalExplore, setGrowthTaskProgress, startGrowthAnalysis, startGrowthFeedback, startGrowthModelAnalysis, generateGrowthMaterials, submitGrowthAnswer, syncGrowthCloud, previewGrowthCloud, type CapturedGrowthSnapshot, type GrowthLocalState, type GrowthCommit, type GrowthDevelopmentEvent, type GrowthCloudExport, type GrowthCloudModule, type GrowthCloudTopic, type GrowthCodeIndex, type GrowthAnalysisSubmission, type GrowthModelPreview, type GrowthModelSources, type GrowthFeedbackPreview, type GrowthTaskChain, type GrowthTaskSummary, type GrowthMaterials, type GrowthMaterialsPreview, type GrowthSyncWorkspace, type GrowthSyncSummary, type GrowthSyncPreview, type GrowthLocalExplorePreview, type GrowthExploreMessage } from '../api/growth';
import { getTask } from '../api/tasks';
import { useTranslation } from '../i18n/useTranslation';

export function parseCodeSymbols(value: string): string[] {
  return value.split(/\s*(?:→|->|,)\s*|\n/).map((item) => item.trim()).filter(Boolean);
}

export function useGrowthWorkspace() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedView = searchParams.get('view');
  const aliases: Record<string, string> = { overview: 'today', pending: 'learning', discuss: 'topics', review: 'cards' };
  const view = requestedView ? (aliases[requestedView] ?? requestedView) : 'today';
  const pageTitles: Record<string, string> = { today: '今日归纳', learning: '待确认', cards: '知识卡', wiki: '知识 Wiki', review: '复习', topics: '探讨', projects: '项目与提交', settings: '设置', all: '成长工作区' };
  const pageDescriptions: Record<string, string> = { today: '先核对聚合结果，再确认成卡', learning: '核对聚合边界，确认后进入知识卡', cards: '确认后的知识卡，先学习，再检查与沉淀', wiki: '跨日、跨项目积累，同一概念持续补证据', review: '先回忆，再揭示答案。按照这一次的表现安排下次复习。', topics: '从问题出发，把值得学习的发现留下来。', projects: '管理本机仓库、完整提交历史和发送范围。', settings: '同步、备份与跨设备恢复。', all: '从代码变化开始，学习、记录并持续复习。' };
  const showProjectManagement = view === 'all' || view === 'projects';
  const showLearningAnalysis = view === 'all' || view === 'learning' || view === 'projects';
  const [state, setState] = useState<GrowthLocalState | null>(null);
  // Legacy /growth links resume a project; named workspace views keep “all projects”.
  const projectId = searchParams.get('project') ?? (view === 'all' ? state?.projects[0]?.project_id ?? '' : '');
  const setProjectId = (value: string) => setSearchParams((current) => {
    const next = new URLSearchParams(current);
    if (value || view === 'all') next.set('project', value); else next.delete('project');
    next.delete('card');
    return next;
  });
  const [bindingId, setBindingId] = useState('');
  const [featureId, setFeatureId] = useState('');
  const [projectName, setProjectName] = useState('');
  const [quickName, setQuickName] = useState('');
  const [quickPath, setQuickPath] = useState('');
  const [quickModelAllowed, setQuickModelAllowed] = useState(false);
  const [repositoryPath, setRepositoryPath] = useState('');
  const [featureLabel, setFeatureLabel] = useState('');
  const [baseRef, setBaseRef] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [captured, setCaptured] = useState<CapturedGrowthSnapshot | null>(null);
  const [untrackedPaths, setUntrackedPaths] = useState<string[]>([]);
  const [selectedUntracked, setSelectedUntracked] = useState<string[]>([]);
  const [commits, setCommits] = useState<GrowthCommit[]>([]);
  const [selectedCommits, setSelectedCommits] = useState<string[]>([]);
  const [commitError, setCommitError] = useState('');
  const [commitMessage, setCommitMessage] = useState('');
  const [exploreSessionId, setExploreSessionId] = useState<string>(() => crypto.randomUUID());
  const [exploreMessages, setExploreMessages] = useState<GrowthExploreMessage[]>([]);
  const [exploreSuggestion, setExploreSuggestion] = useState<{
    title: string; reason: string;
  } | null>(null);
  const [exploreInput, setExploreInput] = useGrowthDraft('explore', projectId);
  const [exploreProvider, setExploreProvider] = useState('');
  const [explorePreview, setExplorePreview] = useState<GrowthLocalExplorePreview | null>(null);
  const [exploreApproved, setExploreApproved] = useState(false);
  const [exploreBusy, setExploreBusy] = useState(false);
  const [exploreError, setExploreError] = useState('');
  const [snapshotId, setSnapshotId] = useState('');
  const [codeSymbols, setCodeSymbols] = useState('');
  const [codeIndex, setCodeIndex] = useState<GrowthCodeIndex | null>(null);
  const [codeIndexBusy, setCodeIndexBusy] = useState(false);
  const [codeIndexError, setCodeIndexError] = useState('');
  const [analysisTaskId, setAnalysisTaskId] = useState<string | null>(null);
  const [analysisMessage, setAnalysisMessage] = useState('');
  const [analysisError, setAnalysisError] = useState('');
  const [modelSources, setModelSources] = useState<GrowthModelSources | null>(null);
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([]);
  const [providerName, setProviderName] = useState('');
  const [modelPreview, setModelPreview] = useState<GrowthModelPreview | null>(null);
  const [modelApproved, setModelApproved] = useState(false);
  const [modelError, setModelError] = useState('');
  const [selectedTaskId, setSelectedTaskId] = useState('');
  const [showExpectedAnswer, setShowExpectedAnswer] = useState(false);
  const [cloudModules, setCloudModules] = useState<GrowthCloudModule[]>([]);
  const [taskModuleIds, setTaskModuleIds] = useState<Record<string, string>>({});
  const [taskChain, setTaskChain] = useState<GrowthTaskChain | null>(null);
  const [materials, setMaterials] = useState<GrowthMaterials | null>(null);
  const [materialsPreview, setMaterialsPreview] = useState<GrowthMaterialsPreview | null>(null);
  const [materialsApproved, setMaterialsApproved] = useState(false);
  const [materialsJobId, setMaterialsJobId] = useState<string | null>(null);
  const [materialsError, setMaterialsError] = useState('');
  const [quizAnswers, setQuizAnswers] = useState<Record<string, string>>({});
  const [quizReveals, setQuizReveals] = useState<Record<string, {
    expected_answer: string; explanation: string;
  }>>({});
  const [ratingBusy, setRatingBusy] = useState(false);
  const ratingRequestIds = useRef<Record<string, string>>({});
  const [answerText, setAnswerText] = useGrowthDraft('answer', projectId + ':' + selectedTaskId);
  const pendingHintRequestId = useRef<string | null>(null);
  const [parentAttemptId, setParentAttemptId] = useState('');
  const [noteText, setNoteText] = useGrowthDraft('note', projectId + ':' + selectedTaskId);
  const [feedbackAttemptId, setFeedbackAttemptId] = useState('');
  const [feedbackPreview, setFeedbackPreview] = useState<GrowthFeedbackPreview | null>(null);
  const [feedbackApproved, setFeedbackApproved] = useState(false);
  const [feedbackTaskId, setFeedbackTaskId] = useState<string | null>(null);
  const [feedbackMessage, setFeedbackMessage] = useState('');
  const [cloudUrl, setCloudUrl] = useState('');
  const [cloudToken, setCloudToken] = useState('');
  const [syncSummary, setSyncSummary] = useState<GrowthSyncSummary | null>(null);
  const [cachedAccounts, setCachedAccounts] = useState<GrowthSyncWorkspace['accounts']>([]);
  const activeCachedAccount = useRef('');

  function selectCachedAccount(accountId: string, accounts = cachedAccounts) {
    const account = accounts.find((item) => item.account_id === accountId);
    if (!account?.sync_summary) return;
    activeCachedAccount.current = account.account_id;
    setSyncSummary(account.sync_summary);
    setCloudUrl(account.cloud_origin ?? '');
    setCloudToken('');
    try { localStorage.setItem('growth:last-sync-account', account.account_id); } catch { /* Session selection still works. */ }
  }

  useEffect(() => {
    let active = true;
    void getGrowthSyncWorkspace().then((workspace) => {
      if (!active) return;
      setCachedAccounts(workspace.accounts);
      let remembered = '';
      try { remembered = localStorage.getItem('growth:last-sync-account') ?? ''; } catch { /* Use the sole local account when available. */ }
      const account = workspace.accounts.find((item) => item.account_id === remembered)
        ?? (workspace.accounts.length === 1 ? workspace.accounts[0] : undefined);
      if (account) {
        activeCachedAccount.current = account.account_id;
        setSyncSummary((current) => current ?? account.sync_summary ?? null);
        setCloudUrl((current) => current || account.cloud_origin || '');
      }
    }).catch(() => { if (active) setSyncError('本机同步记录读取失败，请重新打开设置重试。'); });
    return () => { active = false; };
  }, []);

  const [syncPreview, setSyncPreview] = useState<GrowthSyncPreview | null>(null);
  const [syncApproved, setSyncApproved] = useState(false);
  const [syncError, setSyncError] = useState('');
  const [syncBusy, setSyncBusy] = useState(false);
  const [cloudExport, setCloudExport] = useState<GrowthCloudExport | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState('');
  const pendingExportId = useRef<string | null>(null);
  const [eventInbox, setEventInbox] = useState<GrowthDevelopmentEvent[]>([]);
  const [eventId, setEventId] = useState('');
  const [eventBindingId, setEventBindingId] = useState('');
  const [eventUntrackedPaths, setEventUntrackedPaths] = useState<string[]>([]);
  const [eventSelectedUntracked, setEventSelectedUntracked] = useState<string[]>([]);
  const [eventBusy, setEventBusy] = useState(false);
  const [eventMessage, setEventMessage] = useState('');
  const [eventError, setEventError] = useState('');
  const [topicRefreshKey, setTopicRefreshKey] = useState(0);
  const [cloudTopics, setCloudTopics] = useState<GrowthCloudTopic[]>([]);
  const [cloudTopicsReady, setCloudTopicsReady] = useState(false);
  const [cloudTopicsError, setCloudTopicsError] = useState(false);
  const [topicReuseIds, setTopicReuseIds] = useState<Record<string, string>>({});
  const [selectedLocalTopicId, setSelectedLocalTopicId] = useState('');
  const [localTopicChains, setLocalTopicChains] = useState<GrowthTaskChain[]>([]);
  const [localTopicError, setLocalTopicError] = useState('');
  const [eventAnalysisContext, setEventAnalysisContext] = useState<
    Pick<GrowthDevelopmentEvent, 'account_id' | 'event_id'> & { snapshot_id: string }
  | null>(null);
  const [refreshHistory, setRefreshHistory] = useState(false);
  const [policyDraft, setPolicyDraft] = useState({
    local_processing: true, model_allowed: false, cloud_allowed: false,
  });

  useEffect(() => {
    setExploreSessionId(crypto.randomUUID()); setExploreMessages([]); setExploreSuggestion(null);
    setExplorePreview(null); setExploreApproved(false); setModelPreview(null); setModelApproved(false);
    setSyncPreview(null); setSyncApproved(false); setSelectedCommits([]); setCaptured(null);
    setCodeIndex(null); setCodeSymbols(''); setEventAnalysisContext(null);
    setFeedbackPreview(null); setFeedbackApproved(false); setSelectedLocalTopicId(''); setLocalTopicChains([]);
  }, [projectId]);

  const refresh = useCallback(async () => {
    const next = await getGrowthLocalState();
    setState(next);
    setError('');
  }, []);

  useEffect(() => {
    void refresh().catch(() => setError(t('Growth.load_failed')));
  }, [refresh, t]);

  useEffect(() => {
    let active = true;
    setCloudTopics([]);
    setCloudTopicsReady(false);
    setCloudTopicsError(false);
    if (!syncSummary || !cloudUrl.trim() || !cloudToken.trim()) return;
    void listGrowthCloudTopics({
      cloud_url: cloudUrl.trim(), access_token: cloudToken.trim(),
      account_id: syncSummary.account_id,
    }).then((result) => {
      if (!active) return;
      setCloudTopics(result.topics);
      setCloudTopicsReady(true);
    }).catch(() => { if (active) setCloudTopicsError(true); });
    return () => { active = false; };
  }, [syncSummary, cloudUrl, cloudToken, topicRefreshKey]);

  useEffect(() => {
    let active = true;
    void getGrowthDevelopmentEvents().then((result) => {
      if (active) setEventInbox(result.events);
    }).catch(() => { if (active) setEventError(t('Growth.event_inbox_failed')); });
    return () => { active = false; };
  }, [syncSummary, t]);

  const selectedEvent = eventInbox.find((item) => item.event_id === eventId);
  const selectedEventBindingId = selectedEvent?.bindings.some(
    (item) => item.binding_id === eventBindingId,
  ) ? eventBindingId : (selectedEvent?.bindings[0]?.binding_id ?? '');

  useEffect(() => {
    setEventUntrackedPaths([]);
    setEventSelectedUntracked([]);
    if (!selectedEventBindingId) return;
    let active = true;
    void getGrowthUntracked(selectedEventBindingId).then((result) => {
      if (active) setEventUntrackedPaths(result.paths);
    }).catch(() => { if (active) setEventError(t('Growth.event_untracked_failed')); });
    return () => { active = false; };
  }, [selectedEventBindingId, t]);

  const bindings = state?.bindings.filter((item) => item.project_id === projectId) ?? [];
  const selectedProject = state?.projects.find((item) => item.project_id === projectId);
  useEffect(() => {
    if (!selectedProject) return;
    setPolicyDraft({
      local_processing: selectedProject.local_processing,
      model_allowed: selectedProject.model_allowed,
      cloud_allowed: selectedProject.cloud_allowed,
    });
  }, [selectedProject?.project_id, selectedProject?.local_processing,
    selectedProject?.model_allowed, selectedProject?.cloud_allowed]);
  const features = state?.features.filter((item) => item.project_id === projectId) ?? [];
  const selectedBindingId = bindings.some((item) => item.binding_id === bindingId)
    ? bindingId : (bindings[0]?.binding_id ?? '');
  const selectedFeatureId = features.some((item) => item.feature_id === featureId)
    ? featureId : (features[0]?.feature_id ?? '');
  const selectedBinding = bindings.find((item) => item.binding_id === selectedBindingId);
  const selectedExploreProvider = selectedBinding?.provider_names.includes(exploreProvider)
    ? exploreProvider : (selectedBinding?.provider_names[0] ?? '');
  useEffect(() => {
    setCommits([]);
    setSelectedCommits([]);
    setCommitError('');
    if (!selectedBindingId || !selectedFeatureId) return;
    let active = true;
    void getGrowthCommits(selectedBindingId, selectedFeatureId).then((result) => {
      if (active) setCommits(result.commits);
    }).catch(() => {
      if (active) setCommitError('无法读取当前功能分支的提交');
    });
    return () => { active = false; };
  }, [selectedBindingId, selectedFeatureId]);
  const featureSnapshots = state?.snapshots.filter(
    (item) => item.feature_id === selectedFeatureId
      && item.binding_ids.includes(selectedBindingId),
  ) ?? [];
  const selectedSnapshotId = featureSnapshots.some((item) => item.snapshot_id === snapshotId)
    ? snapshotId : (featureSnapshots[0]?.snapshot_id ?? '');
  const selectedSnapshotIdRef = useRef(selectedSnapshotId);
  selectedSnapshotIdRef.current = selectedSnapshotId;
  const snapshotAnalyses = state?.analysis_runs.filter(
    (item) => item.snapshot_id === selectedSnapshotId,
  ) ?? [];
  const snapshotAnalysisIds = new Set(snapshotAnalyses.map((item) => item.analysis_id));
  const snapshotOpportunities = state?.opportunities.filter(
    (item) => snapshotAnalysisIds.has(item.analysis_id),
  ) ?? [];
  const snapshotOpportunityIds = new Set(
    snapshotOpportunities.map((item) => item.opportunity_id),
  );
  const snapshotTasks = state?.growth_tasks.filter(
    (item) => snapshotOpportunityIds.has(item.opportunity_id),
  ) ?? [];
  const selectedTask = state?.growth_tasks.find((item) => item.task_id === selectedTaskId);

  useEffect(() => {
    setCodeIndex(null);
    setCodeIndexError('');
    setSelectedTaskId(searchParams.get('card') ?? '');
    setTaskChain(null);
    pendingHintRequestId.current = null;
  }, [selectedSnapshotId, searchParams.get('card')]);

  useEffect(() => {
    setTaskChain(null);
    setMaterials(null); setMaterialsPreview(null); setMaterialsApproved(false);
    setMaterialsError(''); setQuizAnswers({}); setQuizReveals({});
    setShowExpectedAnswer(false);
    pendingHintRequestId.current = null;
    setFeedbackAttemptId('');
    setFeedbackPreview(null);
    setFeedbackApproved(false);
    if (!selectedTaskId) return;
    let active = true;
    void getGrowthTaskChain(selectedTaskId).then((result) => {
      if (active) setTaskChain(result);
    }).catch(() => {
      if (active) setError(t('Growth.task_load_failed'));
    });
    void getGrowthMaterials(selectedTaskId).then((result) => {
      if (active) setMaterials(result);
    }).catch(() => { if (active) setMaterialsError('无法读取完整学习材料'); });
    return () => { active = false; };
  }, [selectedTaskId, t]);

  useEffect(() => {
    if (!materialsJobId || !selectedTaskId) return;
    let active = true;
    const poll = () => {
      void getTask(materialsJobId).then((job) => {
        if (!active) return;
        if (job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled') {
          setMaterialsJobId(null);
          void getGrowthMaterials(selectedTaskId).then(setMaterials)
            .catch(() => setMaterialsError('完整学习材料读取失败'));
        }
      }).catch(() => { if (active) setMaterialsError('材料生成状态查询失败'); });
    };
    poll();
    const timer = window.setInterval(poll, 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, [materialsJobId, selectedTaskId]);

  useEffect(() => {
    if (!feedbackTaskId) return;
    let active = true;
    const poll = () => {
      void getTask(feedbackTaskId).then((task) => {
        if (!active) return;
        if (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
          setFeedbackTaskId(null);
          setFeedbackMessage(t(task.status === 'completed'
            ? 'Growth.feedback_done' : 'Growth.feedback_failed'));
          if (selectedTaskId) {
            void getGrowthTaskChain(selectedTaskId).then(setTaskChain)
              .catch(() => setError(t('Growth.task_load_failed')));
          }
        }
      }).catch(() => {
        if (active) setFeedbackMessage(t('Growth.analysis_poll_failed'));
      });
    };
    poll();
    const timer = window.setInterval(poll, 1000);
    return () => { active = false; window.clearInterval(timer); };
  }, [feedbackTaskId, selectedTaskId, t]);
  const hasPendingAnalysis = snapshotAnalyses.some(
    (item) => item.status === 'queued' || item.status === 'running',
  );

  useEffect(() => {
    if (!hasPendingAnalysis || analysisTaskId) return;
    const timer = window.setInterval(() => {
      void refresh().catch(() => setAnalysisError(t('Growth.analysis_poll_failed')));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [hasPendingAnalysis, analysisTaskId, refresh, t]);

  useEffect(() => {
    setModelSources(null);
    setSelectedSourceIds([]);
    setProviderName('');
    setModelPreview(null);
    setModelApproved(false);
    setModelError('');
    if (!selectedSnapshotId || !selectedBindingId) return;
    let active = true;
    void getGrowthModelSources(selectedSnapshotId, selectedBindingId).then((result) => {
      if (active) {
        setModelSources(result);
        setProviderName(result.provider_names.length === 1 ? result.provider_names[0] : '');
      }
    }).catch(() => {
      if (active) setModelError(t('Growth.model_sources_failed'));
    });
    return () => { active = false; };
  }, [selectedSnapshotId, selectedBindingId, t]);

  useEffect(() => {
    if (!analysisTaskId) return;
    let active = true;
    const poll = () => {
      void getTask(analysisTaskId).then((task) => {
        if (!active) return;
        if (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
          setAnalysisTaskId(null);
          setAnalysisError('');
          setAnalysisMessage(t(task.status === 'completed'
            ? 'Growth.analysis_done' : 'Growth.analysis_failed'));
          void refresh().catch(() => setAnalysisError(t('Growth.load_failed')));
        }
      }).catch(() => {
        if (active) setAnalysisError(t('Growth.analysis_poll_failed'));
      });
    };
    poll();
    const timer = window.setInterval(poll, 1000);
    return () => { active = false; window.clearInterval(timer); };
  }, [analysisTaskId, refresh, t]);

  useEffect(() => {
    setUntrackedPaths([]);
    setSelectedUntracked([]);
    if (!selectedBindingId) return;
    let active = true;
    void getGrowthUntracked(selectedBindingId).then((result) => {
      if (active) setUntrackedPaths(result.paths);
    }).catch(() => {
      if (active) setError(t('Growth.untracked_failed'));
    });
    return () => { active = false; };
  }, [selectedBindingId, t]);

  async function act(operation: () => Promise<void>) {
    setBusy(true);
    setError('');
    try {
      await operation();
      await refresh();
    } catch {
      setError(t('Growth.action_failed'));
    } finally {
      setBusy(false);
    }
  }

  function submitProject(event: FormEvent) {
    event.preventDefault();
    if (!projectName.trim()) return;
    void act(async () => {
      const result = await createGrowthProject(projectName.trim());
      setProjectId(result.project_id);
      setBindingId('');
      setFeatureId('');
      setProjectName('');
      setCaptured(null);
    });
  }

  function submitQuickImport(event: FormEvent) {
    event.preventDefault();
    if (!quickName.trim() || !quickPath.trim()) return;
    void act(async () => {
      const result = await importGrowthProject(
        quickName.trim(), quickPath.trim(), quickModelAllowed);
      setProjectId(result.project_id);
      setBindingId(result.binding_id);
      setFeatureId(result.feature_id);
      setSnapshotId(''); setQuickName(''); setQuickPath('');
      navigate('/growth?view=pending&project=' + result.project_id);
    });
  }

  function submitBinding(event: FormEvent) {
    event.preventDefault();
    if (!projectId || !repositoryPath.trim()) return;
    void act(async () => {
      const result = await bindGrowthRepository(projectId, repositoryPath.trim());
      setBindingId(result.binding_id);
      setRepositoryPath('');
    });
  }

  function submitFeature(event: FormEvent) {
    event.preventDefault();
    if (!projectId || !selectedBindingId || !featureLabel.trim() || !baseRef.trim()) return;
    void act(async () => {
      const result = await createGrowthFeature(
        projectId, selectedBindingId, featureLabel.trim(), baseRef.trim(),
      );
      setFeatureId(result.feature_id);
      setFeatureLabel('');
      setCaptured(null);
    });
  }

  function capture() {
    if (!selectedFeatureId || !selectedBindingId) return;
    void act(async () => {
      const result = await captureGrowthSnapshot(
        selectedFeatureId, selectedBindingId, selectedUntracked,
      );
      setCaptured(result);
      setSnapshotId(result.snapshot_id);
    });
  }

  async function captureSelectedCommits() {
    if (!selectedFeatureId || !selectedBindingId || selectedCommits.length === 0) return;
    setBusy(true);
    setCommitError('');
    setCommitMessage('');
    try {
      let last: CapturedGrowthSnapshot | null = null;
      for (const sha of selectedCommits) {
        last = await captureGrowthSnapshot(selectedFeatureId, selectedBindingId, [], sha);
      }
      await refresh();
      if (last) { setCaptured(last); setSnapshotId(last.snapshot_id); }
      setSelectedCommits([]);
      setCommitMessage('已为所选提交分别保存源码快照。下面选择一项，预览代码后生成学习机会。');
    } catch {
      setCommitError('提交快照保存失败。请确认这些提交仍在当前功能分支中。');
    } finally {
      setBusy(false);
    }
  }

  async function previewExplore() {
    if (!projectId || !selectedBindingId || !selectedExploreProvider || !exploreInput.trim()) return;
    setExploreBusy(true); setExploreError(''); setExploreApproved(false);
    try {
      setExplorePreview(await previewGrowthLocalExplore(
        projectId, exploreSessionId, selectedBindingId, selectedExploreProvider,
        exploreInput.trim(),
      ));
    } catch { setExploreError('对话预览失败，请检查项目的模型权限和 provider。'); }
    finally { setExploreBusy(false); }
  }

  async function sendExplore() {
    if (!projectId || !selectedBindingId || !selectedExploreProvider ||
      !explorePreview || !exploreApproved) return;
    setExploreBusy(true); setExploreError('');
    try {
      const result = await sendGrowthLocalExplore(
        projectId, exploreSessionId, selectedBindingId, selectedExploreProvider,
        exploreInput.trim(), explorePreview.approval_hash,
      );
      setExploreMessages(result.messages);
      setExploreSuggestion(result.suggestion);
      setExploreInput(''); setExplorePreview(null); setExploreApproved(false);
      await refresh();
    } catch { setExploreError('对话生成失败，请检查模型连接后重试。'); }
    finally { setExploreBusy(false); }
  }

  async function confirmExploreTopic() {
    if (!projectId || !exploreSuggestion) return;
    setExploreBusy(true); setExploreError('');
    try {
      await confirmGrowthLocalExploreTopic(projectId, exploreSessionId);
      setExploreSuggestion(null);
      await refresh();
    } catch { setExploreError('主题确认失败，请重试。'); }
    finally { setExploreBusy(false); }
  }

  async function openLocalTopic(topicId: string) {
    setSelectedLocalTopicId(topicId); setLocalTopicChains([]); setLocalTopicError('');
    if (!state) return;
    const tasks = state.growth_tasks.filter((task) => {
      if (task.topic_id !== topicId) return false;
      const opportunity = state.opportunities.find(
        (item) => item.opportunity_id === task.opportunity_id);
      const analysis = state.analysis_runs.find(
        (item) => item.analysis_id === opportunity?.analysis_id);
      const snapshot = state.snapshots.find(
        (item) => item.snapshot_id === analysis?.snapshot_id);
      return state.features.some((item) => item.feature_id === snapshot?.feature_id
        && item.project_id === projectId);
    });
    try { setLocalTopicChains(await Promise.all(tasks.map((task) =>
      getGrowthTaskChain(task.task_id)))); }
    catch { setLocalTopicError('主题证据读取失败，请重试。'); }
  }

  function openTopicTask(task: GrowthTaskSummary) {
    if (!state) return;
    const opportunity = state.opportunities.find((item) =>
      item.opportunity_id === task.opportunity_id);
    const analysis = state.analysis_runs.find((item) =>
      item.analysis_id === opportunity?.analysis_id);
    const snapshot = state.snapshots.find((item) =>
      item.snapshot_id === analysis?.snapshot_id);
    if (snapshot) { setFeatureId(snapshot.feature_id); setSnapshotId(snapshot.snapshot_id); }
    setSelectedTaskId(task.task_id);
    navigate('/growth?view=cards&project=' + (task.project_id ?? projectId) + '&card=' + task.task_id);
  }

  async function previewMaterials() {
    if (!selectedTaskId || !selectedBindingId || !providerName) return;
    setMaterialsError(''); setMaterialsApproved(false);
    try { setMaterialsPreview(await previewGrowthMaterials(
      selectedTaskId, selectedBindingId, providerName)); }
    catch { setMaterialsError('无法预览 AhaDiff 完整学习材料的源码范围'); }
  }

  async function startMaterials() {
    if (!selectedTaskId || !selectedBindingId || !providerName ||
      !materialsPreview || !materialsApproved) return;
    setMaterialsError('');
    try {
      const result = await generateGrowthMaterials(
        selectedTaskId, selectedBindingId, providerName, materialsPreview.approval_hash);
      setMaterialsPreview(null); setMaterialsApproved(false);
      if (result.task_id) setMaterialsJobId(result.task_id);
      setMaterials(await getGrowthMaterials(selectedTaskId));
    } catch { setMaterialsError('AhaDiff 材料生成未能启动'); }
  }

  async function revealMaterial(questionId: string) {
    if (!selectedTaskId || !quizAnswers[questionId]?.trim()) return;
    try {
      const result = await revealGrowthMaterialAnswer(
        selectedTaskId, questionId, quizAnswers[questionId].trim());
      setQuizReveals((current) => ({ ...current, [questionId]: result }));
      setMaterials(await getGrowthMaterials(selectedTaskId));
    } catch { setMaterialsError('练习答案提交失败'); }
  }

  async function rateMaterial(questionId: string, answer: 'wrong' | 'hard' | 'good' | 'easy') {
    if (!selectedTaskId || ratingBusy) return;
    setRatingBusy(true); setMaterialsError('');
    const requestKey = `${questionId}:${answer}`;
    ratingRequestIds.current[requestKey] ??= crypto.randomUUID();
    try {
      await rateGrowthMaterialCard(selectedTaskId, questionId, answer,
        ratingRequestIds.current[requestKey]);
      delete ratingRequestIds.current[requestKey];
      setMaterials(await getGrowthMaterials(selectedTaskId));
    } catch { setMaterialsError('复习状态保存失败，请重试。'); }
    finally { setRatingBusy(false); }
  }

  async function indexCode() {
    const symbols = parseCodeSymbols(codeSymbols);
    if (!selectedSnapshotId || !selectedBindingId || symbols.length < 2) return;
    const requestedSnapshotId = selectedSnapshotId;
    setCodeIndexBusy(true);
    setCodeIndexError('');
    setCodeIndex(null);
    try {
      const result = await indexGrowthSnapshot(requestedSnapshotId, selectedBindingId, symbols);
      if (selectedSnapshotIdRef.current === requestedSnapshotId) setCodeIndex(result);
    } catch {
      if (selectedSnapshotIdRef.current === requestedSnapshotId) {
        setCodeIndexError(t('Growth.code_index_failed'));
      }
    } finally {
      setCodeIndexBusy(false);
    }
  }

  async function submitAnalysis(operation: () => Promise<GrowthAnalysisSubmission>) {
    if (!selectedSnapshotId || !selectedBindingId || analysisTaskId) return;
    setBusy(true);
    setAnalysisError('');
    setAnalysisMessage('');
    try {
      const result = await operation();
      if (result.task_id) setAnalysisTaskId(result.task_id);
      setAnalysisMessage(t(result.reused
        ? result.status === 'succeeded' ? 'Growth.analysis_reused'
          : result.status === 'failed' ? 'Growth.analysis_failed'
            : 'Growth.analysis_running'
        : 'Growth.analysis_running'));
      await refresh();
    } catch {
      setAnalysisError(t('Growth.analysis_start_failed'));
    } finally {
      setBusy(false);
    }
  }

  function analyze(analysisId?: string) {
    void submitAnalysis(() => analysisId
      ? retryGrowthAnalysis(analysisId, selectedBindingId)
      : startGrowthAnalysis(selectedSnapshotId, selectedBindingId, crypto.randomUUID()));
  }

  async function previewModelRequest() {
    if (!selectedSnapshotId || !selectedBindingId || !providerName || !selectedSourceIds.length) return;
    setBusy(true);
    setModelError('');
    setModelPreview(null);
    setModelApproved(false);
    try {
      setModelPreview(await previewGrowthModelRequest(
        selectedSnapshotId, selectedBindingId, providerName, selectedSourceIds,
      ));
    } catch {
      setModelError(t('Growth.model_preview_failed'));
    } finally {
      setBusy(false);
    }
  }

  function analyzeLive(reanalysis = false) {
    if (!modelPreview || !modelApproved) return;
    const event = eventAnalysisContext?.snapshot_id === selectedSnapshotId
      ? eventAnalysisContext : undefined;
    void submitAnalysis(() => startGrowthModelAnalysis(
      selectedSnapshotId, selectedBindingId, providerName,
      modelPreview, crypto.randomUUID(), reanalysis, event,
    ));
  }

  async function captureEvent() {
    if (!selectedEvent || !selectedEventBindingId) return;
    setEventBusy(true);
    setEventError('');
    setEventMessage('');
    try {
      const result = await captureGrowthDevelopmentEvent(
        selectedEvent, selectedEventBindingId, eventSelectedUntracked,
      );
      await refresh();
      setProjectId(selectedEvent.project_id);
      setBindingId(selectedEventBindingId);
      setFeatureId(selectedEvent.feature_id ?? '');
      setSnapshotId(result.snapshot_id);
      setCaptured(null);
      setEventAnalysisContext({
        account_id: selectedEvent.account_id,
        event_id: selectedEvent.event_id,
        snapshot_id: result.snapshot_id,
      });
      setEventMessage(t(result.reused ? 'Growth.event_capture_reused'
        : 'Growth.event_capture_ready'));
      setEventInbox((current) => current.map((item) => item.event_id === selectedEvent.event_id
        ? { ...item, capture: {
          binding_id: selectedEventBindingId, snapshot_id: result.snapshot_id,
          analysis_id: result.analysis_id,
        } } : item));
    } catch {
      setEventError(t('Growth.event_capture_failed'));
    } finally {
      setEventBusy(false);
    }
  }

  function decideTopic(proposalId: string, decision: 'confirm' | 'reject',
    existingTopicId?: string) {
    void act(async () => {
      await decideGrowthTopic(proposalId, decision,
        existingTopicId && syncSummary ? {
          account_id: syncSummary.account_id, existing_topic_id: existingTopicId,
        } : undefined);
      setModelPreview(null);
      setModelApproved(false);
    });
  }

  function createTask(opportunityId: string, topicId: string) {
    void act(async () => {
      const moduleId = taskModuleIds[opportunityId];
      const module = moduleId ? cloudModules.find((item) => item.module_id === moduleId) : undefined;
      if (moduleId && !module) throw new Error('选定模块已变化');
      const result = await createGrowthTask(opportunityId, topicId, module);
      setSelectedTaskId(result.task_id);
    });
  }

  function submitAnswer(event: FormEvent) {
    event.preventDefault();
    if (!selectedTaskId || !answerText.trim()) return;
    void act(async () => {
      const hintLevel = Math.max(0, ...(taskChain?.hints.map((item) => item.level) ?? []));
      const previous = taskChain?.attempts.filter((item) => item.status === 'feedback_ready').at(-1);
      await submitGrowthAnswer(selectedTaskId, answerText,
        parentAttemptId || previous?.attempt_id || null, hintLevel);
      setAnswerText('');
      setParentAttemptId('');
      setTaskChain(await getGrowthTaskChain(selectedTaskId));
      setModelPreview(null);
      setModelApproved(false);
    });
  }

  function requestHint() {
    if (!selectedTaskId) return;
    pendingHintRequestId.current ??= crypto.randomUUID();
    void act(async () => {
      await requestGrowthHint(selectedTaskId, pendingHintRequestId.current!);
      pendingHintRequestId.current = null;
      setTaskChain(await getGrowthTaskChain(selectedTaskId));
    });
  }

  function submitNote(event: FormEvent) {
    event.preventDefault();
    if (!selectedTaskId || !noteText.trim()) return;
    void act(async () => {
      await saveGrowthNote(selectedTaskId, noteText);
      setNoteText('');
      setTaskChain(await getGrowthTaskChain(selectedTaskId));
    });
  }

  function changeTaskProgress(target: 'in_progress' | 'paused' | 'completed' | 'dismissed') {
    if (!selectedTaskId) return;
    void act(async () => {
      let pendingDraft = false;
      if (target === 'paused' && answerText.trim()) {
        const workspace = await getGrowthSyncWorkspace();
        const syncedTask = workspace.tasks.find((task) => task.task_id === selectedTaskId
          && (!syncSummary || task.account_id === syncSummary.account_id));
        if (syncedTask) {
          await queueGrowthTaskDraft(syncedTask, answerText,
            syncedTask.local_draft?.submitted_attempt_id ?? syncedTask.remote_draft?.submitted_attempt_id ?? null);
        }
        pendingDraft = Boolean(syncedTask);
        setFeedbackMessage(syncedTask ? '回答草稿已保存在本机，等待同步。' : '回答草稿已保存在本机；卡片首次同步后可在另一设备继续。');
      }
      await setGrowthTaskProgress(selectedTaskId, target);
      setTaskChain(await getGrowthTaskChain(selectedTaskId));
      if (pendingDraft && syncSummary && cloudUrl.trim() && cloudToken.trim()) {
        try {
          const summary = await syncGrowthCloud(cloudUrl.trim(), cloudToken.trim());
          setSyncSummary(summary);
          setFeedbackMessage(summary.outbox.pending || summary.outbox.conflict
            ? '已暂停，回答草稿仍待同步。请在设置中查看同步状态。' : '已暂停，回答草稿已同步，可在另一设备继续。');
        } catch { setFeedbackMessage('已暂停，回答草稿已保存在本机，网络恢复后继续同步。'); }
      }
    });
  }

  async function reviewFeedback(attemptId: string) {
    if (!selectedBindingId) return;
    setBusy(true);
    setFeedbackMessage('');
    setFeedbackAttemptId(attemptId);
    setFeedbackPreview(null);
    setFeedbackApproved(false);
    try {
      setFeedbackPreview(await previewGrowthFeedback(attemptId, selectedBindingId));
    } catch {
      setFeedbackMessage(t('Growth.feedback_preview_failed'));
    } finally {
      setBusy(false);
    }
  }

  async function sendFeedback() {
    if (!feedbackAttemptId || !feedbackPreview || !feedbackApproved || feedbackTaskId) return;
    setBusy(true);
    setFeedbackMessage('');
    try {
      const attempt = taskChain?.attempts.find((item) => item.attempt_id === feedbackAttemptId);
      const result = await startGrowthFeedback(
        feedbackAttemptId, selectedBindingId, feedbackPreview,
        attempt?.status === 'feedback_failed',
      );
      if (result.task_id) setFeedbackTaskId(result.task_id);
      else if (selectedTaskId) setTaskChain(await getGrowthTaskChain(selectedTaskId));
      setFeedbackPreview(null);
      setFeedbackApproved(false);
      setFeedbackMessage(t(result.reused && result.status === 'succeeded'
        ? 'Growth.feedback_done' : 'Growth.feedback_running'));
    } catch {
      setFeedbackMessage(t('Growth.feedback_start_failed'));
    } finally {
      setBusy(false);
    }
  }

  async function submitSync(event: FormEvent) {
    event.preventDefault();
    if (!cloudUrl.trim() || !cloudToken.trim()) return;
    setBusy(true);
    setSyncBusy(true);
    setSyncError('');
    setSyncSummary(null);
    setCloudExport(null);
    pendingExportId.current = null;
    try {
      const summary = await syncGrowthCloud(
        cloudUrl.trim(), cloudToken.trim(), syncApproved && syncPreview?.project_id === projectId
          ? syncPreview : undefined, refreshHistory,
      );
      setSyncSummary(summary);
      activeCachedAccount.current = summary.account_id;
      try { localStorage.setItem('growth:last-sync-account', summary.account_id); } catch { /* Keep the active account in memory. */ }
      setSyncApproved(false);
      setSyncPreview(null);
      setRefreshHistory(false);
      try {
        await refresh();
        setError('');
      } catch {
        setError(t('Growth.load_failed'));
      }
    } catch {
      setSyncError(t('Growth.sync_failed'));
      const cached = cachedAccounts.find((account) => account.account_id === activeCachedAccount.current
        && account.cloud_origin?.replace(/\/$/, '') === cloudUrl.trim().replace(/\/$/, ''));
      if (cached?.sync_summary) setSyncSummary(cached.sync_summary);
    } finally {
      setBusy(false);
      setSyncBusy(false);
    }
  }

  async function prepareCloudExport() {
    if (!syncSummary || !cloudUrl.trim() || !cloudToken.trim()) return;
    setExportBusy(true);
    setExportError('');
    setCloudExport(null);
    try {
      pendingExportId.current ??= crypto.randomUUID();
      setCloudExport(await createGrowthCloudExport(
        cloudUrl.trim(), cloudToken.trim(), syncSummary.account_id, pendingExportId.current,
      ));
      pendingExportId.current = null;
    } catch {
      setExportError(t('Growth.export_failed'));
    } finally {
      setExportBusy(false);
    }
  }

  function downloadCloudExport(format: 'json' | 'markdown') {
    if (!cloudExport) return;
    const isJson = format === 'json';
    const content = isJson ? cloudExport.json_text : cloudExport.markdown_text;
    const blob = new Blob([content], { type: isJson
      ? 'application/json;charset=utf-8' : 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `growth-${cloudExport.export_id}.${isJson ? 'json' : 'md'}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function previewSync() {
    if (!projectId) return;
    setSyncBusy(true);
    setSyncError('');
    setSyncApproved(false);
    try {
      setSyncPreview(await previewGrowthCloud(projectId));
    } catch {
      setSyncPreview(null);
      setSyncError(t('Growth.sync_preview_failed'));
    } finally {
      setSyncBusy(false);
    }
  }


  return {
    pageTitles, pageDescriptions, legacyMode: requestedView === 'learning' || view === 'all',
    t,
    view,
    showProjectManagement,
    showLearningAnalysis,
    state,
    projectId,
    setProjectId,
    bindingId,
    setBindingId,
    setFeatureId,
    projectName,
    setProjectName,
    quickName,
    setQuickName,
    quickPath,
    setQuickPath,
    quickModelAllowed,
    setQuickModelAllowed,
    repositoryPath,
    setRepositoryPath,
    featureLabel,
    setFeatureLabel,
    baseRef,
    setBaseRef,
    busy,
    error,
    captured,
    setCaptured,
    untrackedPaths,
    selectedUntracked,
    setSelectedUntracked,
    commits,
    selectedCommits,
    setSelectedCommits,
    commitError,
    commitMessage,
    exploreSessionId, setExploreSessionId,
    exploreMessages,
    setExploreMessages,
    exploreSuggestion,
    setExploreSuggestion,
    exploreInput,
    setExploreInput,
    setExploreProvider,
    explorePreview,
    setExplorePreview,
    exploreApproved,
    setExploreApproved,
    exploreBusy,
    exploreError,
    snapshotId,
    setSnapshotId,
    codeSymbols,
    setCodeSymbols,
    codeIndex,
    setCodeIndex,
    codeIndexBusy,
    codeIndexError,
    analysisTaskId,
    analysisMessage,
    analysisError,
    modelSources,
    selectedSourceIds,
    setSelectedSourceIds,
    providerName,
    setProviderName,
    modelPreview,
    setModelPreview,
    modelApproved,
    setModelApproved,
    modelError,
    setSelectedTaskId,
    showExpectedAnswer,
    setShowExpectedAnswer,
    cloudModules,
    setCloudModules,
    taskModuleIds,
    setTaskModuleIds,
    taskChain,
    materials,
    materialsPreview,
    materialsApproved,
    setMaterialsApproved,
    materialsError,
    quizAnswers,
    setQuizAnswers,
    quizReveals,
    ratingBusy,
    answerText,
    setAnswerText,
    parentAttemptId,
    setParentAttemptId,
    noteText,
    setNoteText,
    feedbackPreview,
    feedbackApproved,
    setFeedbackApproved,
    feedbackTaskId,
    feedbackMessage,
    cloudUrl,
    setCloudUrl,
    cloudToken,
    setCloudToken,
    syncSummary, cachedAccounts, selectCachedAccount,
    setSyncSummary,
    syncPreview,
    setSyncPreview,
    syncApproved,
    setSyncApproved,
    syncError,
    syncBusy,
    cloudExport,
    setCloudExport,
    exportBusy,
    exportError,
    pendingExportId,
    eventInbox,
    eventId,
    setEventId,
    setEventBindingId,
    eventUntrackedPaths,
    eventSelectedUntracked,
    setEventSelectedUntracked,
    eventBusy,
    eventMessage,
    setEventMessage,
    eventError,
    setEventError,
    topicRefreshKey,
    setTopicRefreshKey,
    cloudTopics,
    cloudTopicsReady,
    cloudTopicsError,
    topicReuseIds,
    setTopicReuseIds,
    selectedLocalTopicId,
    setSelectedLocalTopicId,
    localTopicChains,
    setLocalTopicChains,
    localTopicError,
    eventAnalysisContext,
    refreshHistory,
    setRefreshHistory,
    policyDraft,
    setPolicyDraft,
    refresh,
    selectedEvent,
    selectedEventBindingId,
    bindings,
    selectedProject,
    features,
    selectedBindingId,
    selectedFeatureId,
    selectedBinding,
    selectedExploreProvider,
    featureSnapshots,
    selectedSnapshotId,
    snapshotAnalyses,
    snapshotOpportunities,
    snapshotTasks,
    selectedTask,
    act,
    submitProject,
    submitQuickImport,
    submitBinding,
    submitFeature,
    capture,
    captureSelectedCommits,
    previewExplore,
    sendExplore,
    confirmExploreTopic,
    openLocalTopic,
    openTopicTask,
    previewMaterials,
    startMaterials,
    revealMaterial,
    rateMaterial,
    indexCode,
    analyze,
    previewModelRequest,
    analyzeLive,
    captureEvent,
    decideTopic,
    createTask,
    submitAnswer,
    requestHint,
    submitNote,
    changeTaskProgress,
    reviewFeedback,
    sendFeedback,
    submitSync,
    prepareCloudExport,
    downloadCloudExport,
    previewSync
  };
}
