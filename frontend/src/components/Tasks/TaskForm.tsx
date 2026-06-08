import { useState, useEffect, useRef } from 'react';
import { api } from '../../api/client';
import type { Project, TagItem, Task, UploadResult } from '../../api/client';
import { Plus, Paperclip, X, Star } from 'lucide-react';
import { ProjectSelect } from '../ProjectSelect';
import { resolveTagColor } from '../TagColors';
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [tagItems, setTagItems] = useState<TagItem[]>([]);
  const [tagFilter, setTagFilter] = useState<string>('');
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [filePreviews, setFilePreviews] = useState<string[]>([]);
  const [selectedSecretIds, setSelectedSecretIds] = useState<number[]>([]);
  const [dropError, setDropError] = useState('');
  const [starOnCreate, setStarOnCreate] = useState(false);
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
        provider,
        model: model || activeDefaultModel,
        ...(effort ? { effort_level: effort } : {}),
        ...(thinkingBudget ? { thinking_budget: parseInt(thinkingBudget) || null } : {}),
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
      setCloneFromTaskId('');
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create task');
    } finally {
      setLoading(false);
    }
  };

  return (
    <form ref={formRef} onSubmit={handleSubmit} className="bg-gray-800 rounded-lg p-4 space-y-3">
      <h3 className="text-sm font-semibold text-gray-300">New Task</h3>
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
          className="flex-1 bg-gray-700 text-foreground rounded px-3 py-2 text-sm h-24 resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500"
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
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={pendingFiles.length >= 10}
          className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 px-2 py-1 rounded border border-gray-600 hover:border-gray-400 disabled:opacity-40"
        >
          <Paperclip size={13} />
          {pendingFiles.length > 0 ? `${pendingFiles.length}/10 files` : 'Attach files'}
        </button>
        <SecretPicker selectedIds={selectedSecretIds} onChange={setSelectedSecretIds} />
        {pendingFiles.map((file, idx) => (
          <div key={idx} className="relative rounded overflow-hidden border border-gray-600">
            {filePreviews[idx] ? (
              <div className="w-12 h-12">
                <img src={filePreviews[idx]} alt="" className="w-full h-full object-cover" />
              </div>
            ) : (
              <div className="flex items-center gap-1 px-2 py-1.5 bg-gray-700 text-xs text-gray-300 max-w-[120px]">
                <Paperclip size={11} className="shrink-0" />
                <span className="truncate">{file.name}</span>
              </div>
            )}
            <button
              type="button"
              onClick={() => removeFile(idx)}
              className="absolute top-0 right-0 bg-gray-900/80 rounded-bl p-0.5 text-gray-300 hover:text-white"
            >
              <X size={10} />
            </button>
          </div>
        ))}
      </div>
      <div className="space-y-2">
        {/* Tag filter pills */}
        {(() => {
          const allTags = Array.from(new Set(projects.filter((p) => p.show_in_selector).flatMap((p) => p.tags))).sort();
          if (allTags.length === 0) return null;
          const tcMap: Record<string, string> = {};
          for (const t of tagItems) tcMap[t.name] = t.color;
          return (
            <div className="flex gap-1.5 flex-wrap">
              <button
                type="button"
                onClick={() => setTagFilter('')}
                className={`px-2 py-0.5 rounded text-xs transition-colors ${
                  tagFilter === '' ? 'bg-indigo-600 text-white' : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
                }`}
              >
                All
              </button>
              {allTags.map((tag) => {
                const c = resolveTagColor(tag, tcMap[tag]);
                const active = tagFilter === tag;
                return (
                  <button
                    key={tag}
                    type="button"
                    onClick={() => {
                      setTagFilter(tagFilter === tag ? '' : tag);
                      if (tagFilter !== tag && projectId) {
                        const proj = projects.find((p) => p.id === Number(projectId));
                        if (proj && !proj.tags.includes(tag)) {
                          setProjectId('');
                          setIsNewProject(false);
                        }
                      }
                    }}
                    className={`px-2 py-0.5 rounded text-xs transition-colors border ${c.bg} ${c.text} ${c.border} ${
                      active ? 'opacity-100' : 'opacity-50 hover:opacity-80'
                    }`}
                  >
                    {tag}
                  </button>
                );
              })}
            </div>
          );
        })()}
        <ProjectSelect
          projects={projects.filter((p) => p.show_in_selector && (!tagFilter || p.tags.includes(tagFilter)))}
          value={isNewProject ? NEW_PROJECT_VALUE : projectId || undefined}
          onChange={handleProjectChange}
          placeholder="Select project..."
          extraOptions={[{ value: NEW_PROJECT_VALUE, label: '+ New project' }]}
          className="w-full"
          showStatus
          tagColorMap={Object.fromEntries(tagItems.map((t) => [t.name, t.color]))}
        />
        {isNewProject && (
          <div className="flex gap-2">
            <input
              className="flex-1 bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Project name (required)"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              required
            />
            <input
              className="flex-1 bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Remote repo URL (optional)"
              value={newProjectUrl}
              onChange={(e) => setNewProjectUrl(e.target.value)}
            />
          </div>
        )}
      </div>
      {contextTasks.length > 0 && (
        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-400 whitespace-nowrap">Copy context from:</label>
          <select
            className="flex-1 bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
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
      <div className="flex items-center gap-3 flex-wrap">
        <label className="text-sm text-gray-400">Priority:</label>
        <select
          className="w-[52px] bg-gray-700 text-foreground rounded px-1 py-1.5 text-sm"
          value={priority}
          onChange={(e) => setPriority(Number(e.target.value))}
        >
          {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
        <label className="text-sm text-gray-400 ml-1">Mode:</label>
        <select
          className="w-[70px] bg-gray-700 text-foreground rounded px-1 py-1.5 text-sm"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
        >
          <option value="auto">Auto</option>
          <option value="plan">Plan</option>
          <option value="loop">Loop</option>
          <option value="goal">Goal</option>
        </select>
        <label className="text-sm text-gray-400 ml-1">CLI:</label>
        <select
          className="w-[75px] bg-gray-700 text-foreground rounded px-1 py-1.5 text-sm"
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
        <label className="text-sm text-gray-400 ml-1">Model:</label>
        <select
          className="w-[140px] bg-gray-700 text-foreground rounded px-1 py-1.5 text-sm"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        >
          <option value="">{activeDefaultModel} (default)</option>
          {activeModelOptions.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
        <label className="text-sm text-gray-400 ml-1">Effort:</label>
        <select
          className="w-[80px] bg-gray-700 text-foreground rounded px-1 py-1.5 text-sm"
          value={effort}
          onChange={(e) => setEffort(e.target.value)}
        >
          <option value="">{defaultEffort} (default)</option>
          {activeEffortOptions.filter((e) => e !== defaultEffort).map((e) => (
            <option key={e} value={e}>{e}</option>
          ))}
        </select>
        <label className="text-sm text-gray-400 ml-1">Thinking:</label>
        <select
          className="w-[80px] bg-gray-700 text-foreground rounded px-1 py-1.5 text-sm"
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
        {mode === 'loop' && (
          <>
            <input
              className="flex-1 min-w-0 bg-gray-700 text-foreground rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Todo file path (e.g. TODO.md)"
              value={todoFilePath}
              onChange={(e) => setTodoFilePath(e.target.value)}
              required
            />
            <label className="text-sm text-gray-400 ml-1 whitespace-nowrap">Max iter:</label>
            <input
              type="text"
              inputMode="numeric"
              className="w-20 bg-gray-700 text-foreground rounded px-2 py-1 text-sm"
              value={maxIterations}
              onChange={(e) => setMaxIterations(e.target.value.replace(/[^0-9]/g, ''))}
              onBlur={() => {
                const n = parseInt(maxIterations);
                setMaxIterations(String((!n || n < 1) ? 1 : n));
              }}
            />
            <label className="flex items-center gap-1 text-sm text-gray-400 ml-1 whitespace-nowrap cursor-pointer">
              <input
                type="checkbox"
                checked={mustComplete}
                onChange={(e) => setMustComplete(e.target.checked)}
                className="accent-indigo-500"
              />
              Must complete
            </label>
          </>
        )}
        {mode === 'goal' && (
          <>
            <input
              className="flex-1 min-w-0 bg-gray-700 text-foreground rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Goal condition (e.g. all tests pass and lint is clean)"
              value={goalCondition}
              onChange={(e) => setGoalCondition(e.target.value)}
              required
            />
            <label className="text-sm text-gray-400 ml-1 whitespace-nowrap">Max turns:</label>
            <input
              type="text"
              inputMode="numeric"
              className="w-20 bg-gray-700 text-foreground rounded px-2 py-1 text-sm"
              value={goalMaxTurns}
              onChange={(e) => setGoalMaxTurns(e.target.value.replace(/[^0-9]/g, ''))}
              onBlur={() => {
                const n = parseInt(goalMaxTurns);
                setGoalMaxTurns(String((!n || n < 1) ? 1 : n));
              }}
            />
          </>
        )}
        <label className="flex items-center gap-1.5 text-sm text-gray-400 ml-auto whitespace-nowrap cursor-pointer">
          <Star size={14} className={starOnCreate ? 'text-yellow-400' : 'text-gray-600'} fill={starOnCreate ? 'currentColor' : 'none'} />
          <input
            type="checkbox"
            checked={starOnCreate}
            onChange={(e) => setStarOnCreate(e.target.checked)}
            className="accent-yellow-500 hidden"
          />
          Star
        </label>
        <button
          type="submit"
          disabled={loading || !canSubmit}
          className="flex items-center gap-1 bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded text-sm font-medium disabled:opacity-50"
        >
          <Plus size={16} />
          {loading ? 'Creating...' : 'Create Task'}
        </button>
      </div>
    </form>
  );
}
