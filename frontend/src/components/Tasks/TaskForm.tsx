import { useState, useEffect, useRef } from 'react';
import { api } from '../../api/client';
import type { Project, TagItem, Task, UploadResult } from '../../api/client';
import { Plus, Paperclip, X, Star, Wrench, Settings } from 'lucide-react';
import { ProjectSelect } from '../ProjectSelect';
import { VoiceButton } from '../Voice/VoiceButton';
import { SecretPicker } from '../Secrets/SecretPicker';
import { useFileDrop } from '../../hooks/useFileDrop';

interface TaskFormProps {
  onCreated: () => void;
}

const NEW_PROJECT_VALUE = '__new__';

export function TaskForm({ onCreated }: TaskFormProps) {
  const [description, setDescription] = useState('');
  const [projectId, setProjectId] = useState<number | ''>('');
  const [isNewProject, setIsNewProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [newProjectUrl, setNewProjectUrl] = useState('');
  const [priority, setPriority] = useState(0);
  const [mode, setMode] = useState('auto');
  const [provider, setProvider] = useState('claude');
  // 分布式 Worker：执行位置（'' = 本机）
  const [workerId, setWorkerId] = useState('');
  const [workers, setWorkers] = useState<{ id: number; name: string; status: string }[]>([]);
  const [model, setModel] = useState('');
  const [providerOptions, setProviderOptions] = useState<string[]>(['claude', 'codex']);
  const [effort, setEffort] = useState('');
  const [defaultModel, setDefaultModel] = useState('claude-opus-4-6');
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [defaultCodexModel, setDefaultCodexModel] = useState('gpt-5.1-codex-max');
  const [codexModelOptions, setCodexModelOptions] = useState<string[]>([]);
  const [effortOptions, setEffortOptions] = useState<string[]>([]);
  const [codexEffortOptions, setCodexEffortOptions] = useState<string[]>([]);
  const [defaultEffort, setDefaultEffort] = useState('medium');
  const [todoFilePath, setTodoFilePath] = useState('');
  const [maxIterations, setMaxIterations] = useState('50');
  const [mustComplete, setMustComplete] = useState(false);
  const [goalCondition, setGoalCondition] = useState('');
  const [goalMaxTurns, setGoalMaxTurns] = useState('30');
  const [thinkingBudget, setThinkingBudget] = useState('');
  const [timeoutHours, setTimeoutHours] = useState('');
  const [systemPromptMode, setSystemPromptMode] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [tagItems, setTagItems] = useState<TagItem[]>([]);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [filePreviews, setFilePreviews] = useState<string[]>([]);
  const [selectedSecretIds, setSelectedSecretIds] = useState<number[]>([]);
  const [dropError, setDropError] = useState('');
  const [enabledTools, setEnabledTools] = useState<Record<string, boolean>>({});
  const [showToolsDropdown, setShowToolsDropdown] = useState(false);
  const [showConfigPanel, setShowConfigPanel] = useState(false);
  const [starOnCreate, setStarOnCreate] = useState(false);
  const toolsRef = useRef<HTMLDivElement>(null);
  const configRef = useRef<HTMLDivElement>(null);
  const [cloneFromTaskId, setCloneFromTaskId] = useState<number | ''>('');
  const [contextTasks, setContextTasks] = useState<Task[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLFormElement>(null);

  const loadProjects = () => {
    api.listProjects().then(setProjects).catch(() => {});
    api.listTags().then(setTagItems).catch(() => {});
  };

  useEffect(() => {
    loadProjects();
    api.config().then((c) => {
      setProvider(c.default_provider || 'claude');
      setProviderOptions(c.provider_options.length ? c.provider_options : ['claude', 'codex']);
      setDefaultModel(c.default_model);
      setModelOptions(c.model_options.filter((m) => m !== 'default'));
      setDefaultCodexModel(c.default_codex_model);
      setCodexModelOptions(c.codex_model_options.filter((m) => m !== 'default'));
      setDefaultEffort(c.default_effort);
      setEffortOptions(c.effort_options);
      setCodexEffortOptions(c.codex_effort_options || ['low', 'medium', 'high', 'xhigh']);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!projectId) {
      setContextTasks([]);
      setCloneFromTaskId('');
      return;
    }
    api.listTasks(undefined, true, projectId as number, undefined, 100)
      .then((tasks) => setContextTasks(tasks.filter((t) => t.session_id)))
      .catch(() => setContextTasks([]));
  }, [projectId]);

  const [availableSkills, setAvailableSkills] = useState<{ key: string; label: string; description: string }[]>([]);
  useEffect(() => {
    if (provider !== 'claude') { setAvailableSkills([]); return; }
    api.listSkills()
      .then((skills) => setAvailableSkills(skills.map((s) => ({ key: s.key, label: s.label, description: s.description }))))
      .catch(() => setAvailableSkills([{ key: 'monitor', label: 'Monitor', description: 'Background monitoring sub-agents' }]));
  }, [provider]);
  const AVAILABLE_TOOLS = availableSkills;
  const enabledToolCount = Object.values(enabledTools).filter(Boolean).length;

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (toolsRef.current && !toolsRef.current.contains(e.target as Node)) {
        setShowToolsDropdown(false);
      }
      if (configRef.current && !configRef.current.contains(e.target as Node)) {
        setShowConfigPanel(false);
      }
    };
    if (showToolsDropdown || showConfigPanel) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showToolsDropdown, showConfigPanel]);

  const hasNonDefaultConfig = priority !== 0 || mode !== 'auto' || provider !== (providerOptions[0] || 'claude') || model !== '' || effort !== '' || thinkingBudget !== '' || timeoutHours !== '';

  const activeDefaultModel = provider === 'codex' ? defaultCodexModel : defaultModel;
  const activeModelOptions = provider === 'codex' ? codexModelOptions : modelOptions;
  const activeEffortOptions = provider === 'codex' ? codexEffortOptions : effortOptions;

  const handleProjectChange = (val: string) => {
    if (val === NEW_PROJECT_VALUE) {
      setIsNewProject(true);
      setProjectId('');
    } else {
      setIsNewProject(false);
      setNewProjectName('');
      setNewProjectUrl('');
      setProjectId(val ? Number(val) : '');
    }
  };

  const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
  const isImageFile = (f: File) => IMAGE_EXTS.some((ext) => f.name.toLowerCase().endsWith(ext));

  useFileDrop({
    targetRef: formRef,
    pendingFiles,
    setPendingFiles,
    setFilePreviews,
    onError: (msg) => setDropError(msg),
  });

  useEffect(() => {
    if (dropError) {
      const t = setTimeout(() => setDropError(''), 2000);
      return () => clearTimeout(t);
    }
  }, [dropError]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const combined = [...pendingFiles, ...files].slice(0, 10);
    setPendingFiles(combined);
    setFilePreviews(combined.map((f) => isImageFile(f) ? URL.createObjectURL(f) : ''));
    e.target.value = '';
  };

  const removeFile = (idx: number) => {
    if (filePreviews[idx]) URL.revokeObjectURL(filePreviews[idx]);
    setPendingFiles((prev) => prev.filter((_, i) => i !== idx));
    setFilePreviews((prev) => prev.filter((_, i) => i !== idx));
  };

  const canSubmit =
    (description || mode === 'loop') &&
    (mode !== 'loop' || todoFilePath) &&
    (mode !== 'goal' || goalCondition) &&
    (projectId || (isNewProject && newProjectName));

  useEffect(() => {
    api.listWorkers().then(setWorkers).catch(() => {});
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setLoading(true);
    setError('');
    try {
      let pid = projectId || undefined;

      // Create new project first if needed
      if (isNewProject && newProjectName) {
        const project = await api.createProject({
          name: newProjectName,
          git_url: newProjectUrl || undefined,
        });
        pid = project.id;
        // Refresh project list and reset new project fields
        loadProjects();
        setIsNewProject(false);
        setNewProjectName('');
        setNewProjectUrl('');
        setProjectId(project.id);
      }

      let uploadedPaths: string[] = [];
      let attachments: { url: string; name: string; is_image: boolean }[] = [];
      if (pendingFiles.length > 0) {
        const results: UploadResult[] = await api.uploadImages(pendingFiles);
        uploadedPaths = results.map((r) => r.path);
        attachments = results.map((r) => ({
          url: r.url,
          name: r.filename || r.url.split('/').pop() || 'file',
          is_image: r.is_image,
        }));
      }

      await api.createTask({
        description: description || undefined,
        project_id: pid as number,
        priority,
        mode,
        ...(mode === 'loop' ? { todo_file_path: todoFilePath, max_iterations: parseInt(maxIterations) || 50, must_complete: mustComplete } : {}),
        ...(mode === 'goal' ? { goal_condition: goalCondition, goal_max_turns: parseInt(goalMaxTurns) || 30 } : {}),
        ...(uploadedPaths.length > 0 ? { file_paths: uploadedPaths } : {}),
        ...(attachments.length > 0 ? { attachments } : {}),
        ...(selectedSecretIds.length > 0 ? { secret_ids: selectedSecretIds } : {}),
        ...(workerId ? { worker_id: parseInt(workerId) } : {}),
        provider,
        model: model || activeDefaultModel,
        ...(effort ? { effort_level: effort } : {}),
        ...(thinkingBudget ? { thinking_budget: parseInt(thinkingBudget) || null } : {}),
        ...(systemPromptMode ? { system_prompt_mode: systemPromptMode } : {}),
        ...(timeoutHours !== '' ? { timeout_hours: Number(timeoutHours) } : {}),
        enabled_skills: (() => {
          const skills = Object.entries(enabledTools)
            .filter(([, v]) => v)
            .reduce((acc, [k]) => ({ ...acc, [k]: true }), {} as Record<string, boolean>);
          return Object.keys(skills).length > 0 ? skills : undefined;
        })(),
        ...(starOnCreate ? { starred: true } : {}),
        ...(cloneFromTaskId ? { clone_from_task_id: cloneFromTaskId as number } : {}),
      });
      setDescription('');
      setPriority(0);
      filePreviews.forEach((url) => URL.revokeObjectURL(url));
      setPendingFiles([]);
      setFilePreviews([]);
      setSelectedSecretIds([]);
      setModel('');
      setEffort('');
      setThinkingBudget('');
      setSystemPromptMode('');
      setTimeoutHours('');
      setCloneFromTaskId('');
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create task');
    } finally {
      setLoading(false);
    }
  };

  return (
    <form ref={formRef} onSubmit={handleSubmit} className="bg-surface rounded-lg p-4 space-y-3 overflow-visible">
      <h3 className="text-sm font-semibold text-muted">New Task</h3>
      {dropError && (
        <div className="bg-yellow-900/50 border border-yellow-700 text-yellow-300 text-xs rounded px-3 py-2 flex items-center justify-between">
          <span>{dropError}</span>
          <button type="button" onClick={() => setDropError('')} className="text-yellow-400 hover:text-yellow-200 ml-2">
            <X size={14} />
          </button>
        </div>
      )}
      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-300 text-xs rounded px-3 py-2 flex items-center justify-between">
          <span>{error}</span>
          <button type="button" onClick={() => setError('')} className="text-red-400 hover:text-red-200 ml-2">
            <X size={14} />
          </button>
        </div>
      )}
      <div className="flex gap-2">
        <textarea
          className="flex-1 bg-input text-foreground rounded px-3 py-2 text-sm h-24 resize-none focus:outline-none focus:ring-2 focus:ring-focus"
          placeholder={mode === 'loop' ? 'Background / context (optional)' : `Prompt / Description (this will be sent to ${provider === 'codex' ? 'Codex' : 'Claude Code'})`}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          required={mode !== 'loop'}
        />
        <VoiceButton onTranscribed={(text) => setDescription((prev) => prev ? prev + ' ' + text : text)} />
      </div>
      {/* Image attachments */}
      <div className="flex items-center gap-2 flex-wrap">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFileSelect}
        />
        <SecretPicker selectedIds={selectedSecretIds} onChange={setSelectedSecretIds} />
        {pendingFiles.map((file, idx) => (
          <div key={idx} className="relative rounded overflow-hidden border border-input-border">
            {filePreviews[idx] ? (
              <div className="w-12 h-12">
                <img src={filePreviews[idx]} alt="" className="w-full h-full object-cover" />
              </div>
            ) : (
              <div className="flex items-center gap-1 px-2 py-1.5 bg-input text-xs text-muted max-w-[120px]">
                <Paperclip size={11} className="shrink-0" />
                <span className="truncate">{file.name}</span>
              </div>
            )}
            <button
              type="button"
              onClick={() => removeFile(idx)}
              className="absolute top-0 right-0 bg-surface-raised/80 rounded-bl p-0.5 text-muted hover:text-foreground"
            >
              <X size={10} />
            </button>
          </div>
        ))}
      </div>
      <div className="space-y-2">
        <ProjectSelect
          projects={projects.filter((p) => p.show_in_selector)}
          value={isNewProject ? NEW_PROJECT_VALUE : projectId || undefined}
          onChange={handleProjectChange}
          placeholder="Select project..."
          extraOptions={[{ value: NEW_PROJECT_VALUE, label: '+ New project' }]}
          className="w-full"
          showStatus
          tagColorMap={Object.fromEntries(tagItems.map((t) => [t.name, t.color]))}
        />
        {isNewProject && (
          <div className="flex flex-col sm:flex-row gap-2">
            <input
              className="flex-1 bg-input text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-focus"
              placeholder="Project name (required)"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              required
            />
            <input
              className="flex-1 min-w-0 bg-input text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-focus"
              placeholder="Remote repo URL (optional)"
              value={newProjectUrl}
              onChange={(e) => setNewProjectUrl(e.target.value)}
            />
          </div>
        )}
      </div>
      {contextTasks.length > 0 && (
        <div className="flex items-center gap-2 min-w-0">
          <label className="text-sm text-subtle whitespace-nowrap shrink-0">Copy context from:</label>
          <select
            className="flex-1 min-w-0 bg-input text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-focus"
            value={cloneFromTaskId}
            onChange={(e) => setCloneFromTaskId(e.target.value ? Number(e.target.value) : '')}
          >
            <option value="">None (start fresh)</option>
            {contextTasks.map((t) => (
              <option key={t.id} value={t.id}>
                #{t.id} {t.description ? t.description.slice(0, 60) : t.title || '(no description)'}
                {t.description && t.description.length > 60 ? '…' : ''}
              </option>
            ))}
          </select>
        </div>
      )}
      {/* Mode-specific inputs (loop/goal) */}
      {mode === 'loop' && (
        <div className="flex items-center gap-2 flex-wrap">
          <input
            className="flex-1 min-w-0 bg-input text-foreground rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-focus"
            placeholder="Todo file path (e.g. TODO.md)"
            value={todoFilePath}
            onChange={(e) => setTodoFilePath(e.target.value)}
            required
          />
          <label className="text-xs text-subtle whitespace-nowrap">Max iter:</label>
          <input
            type="text"
            inputMode="numeric"
            className="w-16 bg-input text-foreground rounded px-2 py-1.5 text-sm"
            value={maxIterations}
            onChange={(e) => setMaxIterations(e.target.value.replace(/[^0-9]/g, ''))}
            onBlur={() => {
              const n = parseInt(maxIterations);
              setMaxIterations(String((!n || n < 1) ? 1 : n));
            }}
          />
          <label className="flex items-center gap-1 text-xs text-subtle whitespace-nowrap cursor-pointer">
            <input
              type="checkbox"
              checked={mustComplete}
              onChange={(e) => setMustComplete(e.target.checked)}
              className="accent-accent"
            />
            Must complete
          </label>
        </div>
      )}
      {mode === 'goal' && (
        <div className="flex items-center gap-2 flex-wrap">
          <input
              className="flex-1 min-w-0 bg-input text-foreground rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-focus"
            placeholder="Goal condition (e.g. all tests pass and lint is clean)"
            value={goalCondition}
            onChange={(e) => setGoalCondition(e.target.value)}
            required
          />
          <label className="text-xs text-subtle whitespace-nowrap">Max turns:</label>
          <input
            type="text"
            inputMode="numeric"
            className="w-16 bg-input text-foreground rounded px-2 py-1.5 text-sm"
            value={goalMaxTurns}
            onChange={(e) => setGoalMaxTurns(e.target.value.replace(/[^0-9]/g, ''))}
            onBlur={() => {
              const n = parseInt(goalMaxTurns);
              setGoalMaxTurns(String((!n || n < 1) ? 1 : n));
            }}
          />
        </div>
      )}
      {/* Bottom action row */}
      <div className="flex items-center gap-2 flex-wrap">
        {/* Attach files */}
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={pendingFiles.length >= 10}
          className="flex items-center gap-1 text-xs px-2 py-1.5 rounded border transition-colors bg-input text-subtle border-input-border hover:bg-surface-hover hover:text-muted disabled:opacity-40"
        >
          <Paperclip size={13} />
          <span className="hidden sm:inline">{pendingFiles.length > 0 ? `${pendingFiles.length}/10 files` : 'Attach files'}</span>
          {pendingFiles.length > 0 && <span className="sm:hidden">{pendingFiles.length}</span>}
        </button>
        {/* Config dropdown */}
        <div ref={configRef} className="relative">
          <button
            type="button"
            onClick={() => setShowConfigPanel(!showConfigPanel)}
            className={`flex items-center gap-1 text-xs px-2 py-1.5 rounded border transition-colors ${
              hasNonDefaultConfig
                ? 'bg-amber-600/30 text-amber-300 border-amber-500/50 hover:bg-amber-600/40'
                : 'bg-input text-subtle border-input-border hover:bg-surface-hover hover:text-muted'
            }`}
          >
            <Settings size={13} />
            <span className="hidden sm:inline">Config</span>
          </button>
          {showConfigPanel && (
            <div className="absolute bottom-full mb-1 left-0 bg-surface border border-input-border rounded shadow-lg z-20 p-3 min-w-[280px]">
              <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 items-center text-xs">
                <span className="text-subtle">Priority</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={priority}
                  onChange={(e) => setPriority(Number(e.target.value))}
                >
                  {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>

                <span className="text-subtle">Mode</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={mode}
                  onChange={(e) => setMode(e.target.value)}
                >
                  <option value="auto">Auto</option>
                  <option value="plan">Plan</option>
                  <option value="loop">Loop</option>
                  <option value="goal">Goal</option>
                </select>

                <span className="text-subtle">Run on</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={workerId}
                  onChange={(e) => setWorkerId(e.target.value)}
                >
                  <option value="">本机</option>
                  {workers.map((w) => (
                    <option key={w.id} value={w.id} disabled={w.status !== 'ready'}>
                      {w.name}{w.status !== 'ready' ? ` (${w.status})` : ''}
                    </option>
                  ))}
                </select>

                <span className="text-subtle">CLI</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={provider}
                  onChange={(e) => {
                    setProvider(e.target.value);
                    setModel('');
                    setEffort('');
                  }}
                >
                  {providerOptions.map((p) => (
                    <option key={p} value={p}>{p === 'claude' ? 'Claude' : p === 'codex' ? 'Codex' : p}</option>
                  ))}
                </select>

                <span className="text-subtle">Model</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                >
                  <option value="">{activeDefaultModel} (default)</option>
                  {activeModelOptions.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>

                <span className="text-subtle">Effort</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={effort}
                  onChange={(e) => setEffort(e.target.value)}
                >
                  <option value="">{defaultEffort} (default)</option>
                  {activeEffortOptions.filter((e) => e !== defaultEffort).map((e) => (
                    <option key={e} value={e}>{e}</option>
                  ))}
                </select>

                <span className="text-subtle">Thinking</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={thinkingBudget}
                  onChange={(e) => setThinkingBudget(e.target.value)}
                >
                  <option value="">default</option>
                  <option value="4096">4k</option>
                  <option value="8192">8k</option>
                  <option value="16384">16k</option>
                  <option value="32768">32k</option>
                  <option value="65536">64k</option>
                  <option value="131072">128k</option>
                </select>

                <span className="text-subtle">Timeout</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={timeoutHours}
                  onChange={(e) => setTimeoutHours(e.target.value)}
                >
                  <option value="">default</option>
                  <option value="0.5">30 min</option>
                  <option value="1">1 hour</option>
                  <option value="2">2 hours</option>
                  <option value="4">4 hours</option>
                  <option value="8">8 hours</option>
                  <option value="12">12 hours</option>
                  <option value="24">24 hours</option>
                  <option value="0">No limit</option>
                </select>

                <span className="text-subtle">System Prompt</span>
                <select
                  className="bg-input text-foreground rounded px-2 py-1 text-xs"
                  value={systemPromptMode}
                  onChange={(e) => setSystemPromptMode(e.target.value)}
                >
                  <option value="">Off</option>
                  <option value="append">Fable 5 (Append)</option>
                  <option value="replace">Fable 5 (Replace)</option>
                </select>
              </div>
            </div>
          )}
        </div>
        {/* Star */}
        <button
          type="button"
          onClick={() => setStarOnCreate(!starOnCreate)}
          className={`flex items-center gap-1 text-xs px-2 py-1.5 rounded border transition-colors ${
            starOnCreate
              ? 'bg-yellow-600/30 text-yellow-300 border-yellow-500/50 hover:bg-yellow-600/40'
              : 'bg-input text-subtle border-input-border hover:bg-surface-hover hover:text-muted'
          }`}
        >
          <Star size={13} fill={starOnCreate ? 'currentColor' : 'none'} />
        </button>
        {/* Tools dropdown */}
        {AVAILABLE_TOOLS.length > 0 && (
          <div ref={toolsRef} className="relative">
            <button
              type="button"
              onClick={() => setShowToolsDropdown(!showToolsDropdown)}
              className={`flex items-center gap-1 text-xs px-2 py-1.5 rounded border transition-colors ${
                enabledToolCount > 0
                  ? 'bg-accent-muted text-accent-muted-foreground border-focus hover:bg-accent hover:text-accent-foreground'
                  : 'bg-input text-subtle border-input-border hover:bg-surface-hover hover:text-muted'
              }`}
            >
              <Wrench size={13} />
              <span className="hidden sm:inline">Tools{enabledToolCount > 0 ? ` (${enabledToolCount})` : ''}</span>
              {enabledToolCount > 0 && <span className="sm:hidden">{enabledToolCount}</span>}
            </button>
            {showToolsDropdown && (
              <div className="absolute top-full mt-1 left-0 bg-surface border border-input-border rounded shadow-lg z-20 min-w-[180px]">
                {AVAILABLE_TOOLS.map((tool) => {
                  const locked = false;
                  return (
                    <label
                      key={tool.key}
                      className={`flex items-center gap-2 px-3 py-2 text-xs transition-colors ${locked ? 'text-subtle cursor-default' : 'text-muted hover:bg-surface-hover cursor-pointer'}`}
                      title={tool.description}
                    >
                      <input
                        type="checkbox"
                        checked={!!enabledTools[tool.key]}
                        onChange={(e) => !locked && setEnabledTools((prev) => ({ ...prev, [tool.key]: e.target.checked }))}
                        disabled={locked}
                        className="accent-accent"
                      />
                      {tool.label}
                    </label>
                  );
                })}
              </div>
            )}
          </div>
        )}
        {/* Create */}
        <button
          type="submit"
          disabled={loading || !canSubmit}
          className="flex items-center gap-1 bg-accent hover:bg-accent-hover text-accent-foreground px-4 py-1.5 rounded text-xs font-medium disabled:opacity-50 ml-auto"
        >
          <Plus size={14} />
          <span className="hidden sm:inline">{loading ? 'Creating...' : 'Create'}</span>
        </button>
      </div>
    </form>
  );
}
