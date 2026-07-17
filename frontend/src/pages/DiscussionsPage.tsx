import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';
import type { DiscussionListItem, Project } from '../api/client';
import { DiscussionView } from '../components/Discussion/DiscussionView';
import { Plus, MessageSquare, Trash2 } from '../components/icons';

export function DiscussionsPage() {
  const [discussions, setDiscussions] = useState<DiscussionListItem[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | undefined>();
  const [showForm, setShowForm] = useState(false);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [facilitatorModel, setFacilitatorModel] = useState('claude-opus-4-6');
  const [agentModel, setAgentModel] = useState('claude-opus-4-6');
  const [maxAgents, setMaxAgents] = useState(5);

  const loadDiscussions = useCallback(async () => {
    try {
      const list = await api.listDiscussions();
      setDiscussions(list);
    } catch (e) {
      console.error('Failed to load discussions:', e);
    }
  }, []);

  useEffect(() => {
    loadDiscussions();
    api.listProjects().then(setProjects).catch(() => {});
    api.config().then((c) => setModelOptions(c.model_options)).catch(() => {});
  }, [loadDiscussions]);

  const handleCreate = async () => {
    if (!title.trim()) return;
    setCreating(true);
    try {
      const d = await api.createDiscussion({
        title: title.trim(),
        project_id: projectId,
        facilitator_model: facilitatorModel,
        agent_model: agentModel,
        max_agents: maxAgents,
      });
      setDiscussions((prev) => [d, ...prev]);
      setActiveId(d.id);
      setTitle('');
      setShowForm(false);
    } catch (e) {
      console.error('Failed to create discussion:', e);
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: number, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Delete this discussion?')) return;
    try {
      await api.deleteDiscussion(id);
      setDiscussions((prev) => prev.filter((d) => d.id !== id));
      if (activeId === id) setActiveId(null);
    } catch (err) {
      console.error('Failed to delete:', err);
    }
  };

  if (activeId) {
    return (
      <DiscussionView
        discussionId={activeId}
        onBack={() => {
          setActiveId(null);
          loadDiscussions();
        }}
        onDeleted={loadDiscussions}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold text-foreground">Discussions</h2>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-1.5 px-3 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-500 transition-colors"
        >
          <Plus size={16} />
          New Discussion
        </button>
      </div>

      {showForm && (
        <div className="bg-gray-800 rounded-lg p-4 space-y-3 border border-gray-700">
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Discussion topic, e.g. SaaS 架构设计方案讨论"
            className="w-full bg-gray-900 text-foreground rounded px-3 py-2 text-sm border border-gray-700 focus:border-indigo-500 focus:outline-none"
            onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
            autoFocus
          />
          <div className="flex items-center gap-3 flex-wrap">
            <select
              value={projectId ?? ''}
              onChange={(e) =>
                setProjectId(e.target.value ? Number(e.target.value) : undefined)
              }
              className="bg-gray-900 text-foreground text-sm rounded px-3 py-2 border border-gray-700"
            >
              <option value="">No project</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
            <div className="flex items-center gap-1.5">
              <label className="text-xs text-gray-400">Facilitator:</label>
              <select
                value={facilitatorModel}
                onChange={(e) => setFacilitatorModel(e.target.value)}
                className="bg-gray-900 text-foreground text-xs rounded px-2 py-2 border border-gray-700"
              >
                {modelOptions.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-1.5">
              <label className="text-xs text-gray-400">Agents:</label>
              <select
                value={agentModel}
                onChange={(e) => setAgentModel(e.target.value)}
                className="bg-gray-900 text-foreground text-xs rounded px-2 py-2 border border-gray-700"
              >
                {modelOptions.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-1.5">
              <label className="text-xs text-gray-400">Max agents:</label>
              <select
                value={maxAgents}
                onChange={(e) => setMaxAgents(Number(e.target.value))}
                className="bg-gray-900 text-foreground text-xs rounded px-2 py-2 border border-gray-700"
              >
                {[1, 2, 3, 4, 5].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </div>
            <button
              onClick={handleCreate}
              disabled={!title.trim() || creating}
              className="px-4 py-2 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-500 disabled:opacity-50 transition-colors"
            >
              {creating ? 'Creating...' : 'Create'}
            </button>
            <button
              onClick={() => setShowForm(false)}
              className="px-4 py-2 text-gray-400 text-sm hover:text-foreground transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {discussions.length === 0 && !showForm ? (
        <div className="text-center py-16 text-gray-500">
          <MessageSquare size={48} className="mx-auto mb-4 opacity-50" />
          <p className="text-lg">No discussions yet</p>
          <p className="text-sm mt-1">
            Create a discussion to brainstorm with multiple AI perspectives
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {discussions.map((d) => (
            <div
              key={d.id}
              onClick={() => setActiveId(d.id)}
              className="bg-gray-800 rounded-lg p-4 cursor-pointer hover:bg-gray-750 border border-gray-700 hover:border-gray-600 transition-colors flex items-center gap-3"
            >
              <MessageSquare size={20} className="text-indigo-400 shrink-0" />
              <div className="flex-1 min-w-0">
                <h3 className="font-medium text-foreground truncate">
                  {d.title}
                </h3>
                <p className="text-xs text-gray-400 mt-0.5">
                  {d.agent_count} agents · {d.message_count} messages · {new Date(d.created_at).toLocaleDateString()}
                </p>
              </div>
              <button
                onClick={(e) => handleDelete(d.id, e)}
                className="p-1.5 text-gray-500 hover:text-red-400 transition-colors"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
