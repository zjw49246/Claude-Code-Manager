import { useState, useEffect, useRef } from 'react';
import { api } from '../../api/client';
import type { Project, TagItem, UploadResult } from '../../api/client';
import { Plus, Paperclip, X } from 'lucide-react';
import { ProjectSelect } from '../ProjectSelect';
import { resolveTagColor } from '../TagColors';
import { VoiceButton } from '../Voice/VoiceButton';
import { SecretPicker } from '../Secrets/SecretPicker';

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
  const [model, setModel] = useState('');
  const [defaultModel, setDefaultModel] = useState('opus');
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [todoFilePath, setTodoFilePath] = useState('');
  const [maxIterations, setMaxIterations] = useState(50);
  const [loading, setLoading] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [tagItems, setTagItems] = useState<TagItem[]>([]);
  const [tagFilter, setTagFilter] = useState<string>('');
  const [pendingImages, setPendingImages] = useState<File[]>([]);
  const [imagePreviews, setImagePreviews] = useState<string[]>([]);
  const [selectedSecretIds, setSelectedSecretIds] = useState<number[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadProjects = () => {
    api.listProjects().then(setProjects).catch(() => {});
    api.listTags().then(setTagItems).catch(() => {});
  };

  useEffect(() => {
    loadProjects();
    api.config().then((c) => {
      setDefaultModel(c.default_model);
      setModelOptions(c.model_options.filter((m) => m !== 'default'));
    }).catch(() => {});
  }, []);

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

  const handleImageSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const combined = [...pendingImages, ...files].slice(0, 5);
    setPendingImages(combined);
    setImagePreviews(combined.map((f) => URL.createObjectURL(f)));
    // Reset input so same file can be re-selected after removal
    e.target.value = '';
  };

  const removeImage = (idx: number) => {
    URL.revokeObjectURL(imagePreviews[idx]);
    const imgs = pendingImages.filter((_, i) => i !== idx);
    const prevs = imagePreviews.filter((_, i) => i !== idx);
    setPendingImages(imgs);
    setImagePreviews(prevs);
  };

  const canSubmit =
    (description || mode === 'loop') &&
    (mode !== 'loop' || todoFilePath) &&
    (projectId || (isNewProject && newProjectName));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setLoading(true);
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
      if (pendingImages.length > 0) {
        const results: UploadResult[] = await api.uploadImages(pendingImages);
        uploadedPaths = results.map((r) => r.path);
      }

      await api.createTask({
        description: description || undefined,
        project_id: pid as number,
        priority,
        mode,
        ...(mode === 'loop' ? { todo_file_path: todoFilePath, max_iterations: maxIterations } : {}),
        ...(uploadedPaths.length > 0 ? { image_paths: uploadedPaths } : {}),
        ...(selectedSecretIds.length > 0 ? { secret_ids: selectedSecretIds } : {}),
        model: model || defaultModel,
      });
      setDescription('');
      setPriority(0);
      imagePreviews.forEach((url) => URL.revokeObjectURL(url));
      setPendingImages([]);
      setImagePreviews([]);
      setSelectedSecretIds([]);
      setModel('');
      onCreated();
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="bg-gray-800 rounded-lg p-4 space-y-3">
      <h3 className="text-sm font-semibold text-gray-300">New Task</h3>
      <div className="flex gap-2">
        <textarea
          className="flex-1 bg-gray-700 text-foreground rounded px-3 py-2 text-sm h-24 resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500"
          placeholder={mode === 'loop' ? 'Background / context (optional)' : 'Prompt / Description (this will be sent to Claude Code)'}
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
          accept="image/png,image/jpeg,image/gif,image/webp"
          multiple
          className="hidden"
          onChange={handleImageSelect}
        />
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={pendingImages.length >= 5}
          className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 px-2 py-1 rounded border border-gray-600 hover:border-gray-400 disabled:opacity-40"
        >
          <Paperclip size={13} />
          {pendingImages.length > 0 ? `${pendingImages.length}/5 images` : 'Attach images'}
        </button>
        <SecretPicker selectedIds={selectedSecretIds} onChange={setSelectedSecretIds} />
        {imagePreviews.map((src, idx) => (
          <div key={idx} className="relative w-12 h-12 rounded overflow-hidden border border-gray-600">
            <img src={src} alt="" className="w-full h-full object-cover" />
            <button
              type="button"
              onClick={() => removeImage(idx)}
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
      <div className="flex items-center gap-3 flex-wrap">
        <label className="text-sm text-gray-400">Priority:</label>
        <input
          type="number"
          className="w-20 bg-gray-700 text-foreground rounded px-2 py-1 text-sm"
          value={priority}
          onChange={(e) => setPriority(Number(e.target.value))}
        />
        <label className="text-sm text-gray-400 ml-2">Mode:</label>
        <select
          className="bg-gray-700 text-foreground rounded px-2 py-1 text-sm"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
        >
          <option value="auto">Auto (direct execute)</option>
          <option value="plan">Plan (review first)</option>
          <option value="loop">Loop (todo list)</option>
        </select>
        <label className="text-sm text-gray-400 ml-2">Model:</label>
        <select
          className="w-[130px] bg-gray-700 text-foreground rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        >
          <option value="">{defaultModel} (default)</option>
          {modelOptions.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
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
              type="number"
              min={1}
              className="w-20 bg-gray-700 text-foreground rounded px-2 py-1 text-sm"
              value={maxIterations}
              onChange={(e) => setMaxIterations(Math.max(1, Number(e.target.value)))}
            />
          </>
        )}
        <button
          type="submit"
          disabled={loading || !canSubmit}
          className="ml-auto flex items-center gap-1 bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded text-sm font-medium disabled:opacity-50"
        >
          <Plus size={16} />
          {loading ? 'Creating...' : 'Create Task'}
        </button>
      </div>
    </form>
  );
}
