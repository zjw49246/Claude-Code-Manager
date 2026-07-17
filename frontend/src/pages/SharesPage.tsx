import { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from '../api/client';
import type { SharedTaskReceived } from '../api/client';
import { RefreshCw, MessageCircle, X, Search } from '../components/icons';
import { SharedChatView } from '../components/Chat/SharedChatView';

export default function SharesPage() {
  const [tasks, setTasks] = useState<SharedTaskReceived[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openTask, setOpenTask] = useState<SharedTaskReceived | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [ownerFilter, setOwnerFilter] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getSharedTasks();
      setTasks(data.tasks);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleLeave = async (id: number) => {
    if (!confirm('Leave this shared task?')) return;
    try {
      await api.leaveSharedTask(id);
      setTasks(prev => prev.filter(t => t.id !== id));
      if (openTask?.id === id) setOpenTask(null);
    } catch (e) {
      setError(String(e));
    }
  };

  const owners = useMemo(() => {
    const set = new Set<string>();
    tasks.forEach(t => { if (t.owner_name) set.add(t.owner_name); });
    return Array.from(set).sort();
  }, [tasks]);

  const filtered = useMemo(() => {
    let result = tasks;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      result = result.filter(t =>
        (t.task_title || '').toLowerCase().includes(q) ||
        (t.task_description || '').toLowerCase().includes(q) ||
        (t.project_name || '').toLowerCase().includes(q)
      );
    }
    if (ownerFilter) {
      result = result.filter(t => t.owner_name === ownerFilter);
    }
    return result;
  }, [tasks, searchQuery, ownerFilter]);

  if (openTask) {
    return (
      <div className="h-[calc(100vh-8rem)]">
        <SharedChatView shared={openTask} onBack={() => setOpenTask(null)} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-foreground">Shared with me</h1>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-2 px-3 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 text-gray-200 rounded-lg disabled:opacity-50"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      {tasks.length > 0 && (
        <div className="flex items-center gap-3">
          <div className="relative flex-1">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search tasks..."
              className="w-full bg-gray-800 text-foreground rounded-lg pl-9 pr-3 py-2 text-sm border border-gray-700 focus:outline-none focus:border-blue-500"
            />
          </div>
          {owners.length > 1 && (
            <select
              value={ownerFilter}
              onChange={(e) => setOwnerFilter(e.target.value)}
              className="bg-gray-800 text-foreground rounded-lg px-3 py-2 text-sm border border-gray-700 focus:outline-none focus:border-blue-500"
            >
              <option value="">All owners</option>
              {owners.map(o => <option key={o} value={o}>{o}</option>)}
            </select>
          )}
        </div>
      )}

      {error && <p className="text-red-400 text-sm">{error}</p>}

      {!loading && tasks.length === 0 && (
        <div className="text-center py-16 text-gray-500">
          <p className="text-lg">No shared tasks yet</p>
          <p className="text-sm mt-2">When someone shares a task with you, it will appear here.</p>
        </div>
      )}

      {tasks.length > 0 && filtered.length === 0 && (
        <p className="text-gray-500 text-sm text-center py-8">No tasks match your search.</p>
      )}

      <div className="grid gap-3">
        {filtered.map(task => (
          <div key={task.id} className="bg-gray-800 rounded-xl border border-gray-700 p-4 hover:border-gray-600 transition-colors">
            <div className="flex items-start justify-between">
              <div className="flex-1 min-w-0 cursor-pointer" onClick={() => setOpenTask(task)}>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs text-gray-500 bg-gray-700 px-2 py-0.5 rounded">
                    from {task.owner_name || 'Unknown'}
                  </span>
                  {task.project_name && (
                    <span className="text-xs text-blue-400 bg-blue-900/30 px-2 py-0.5 rounded">
                      {task.project_name}
                    </span>
                  )}
                </div>
                <h3 className="text-foreground font-medium truncate">
                  {task.task_title || `Task #${task.remote_task_id}`}
                </h3>
                {task.task_description && (
                  <p className="text-gray-400 text-sm mt-1 line-clamp-2">{task.task_description}</p>
                )}
                {task.received_at && (
                  <p className="text-gray-500 text-xs mt-2">
                    Received {new Date(task.received_at).toLocaleString()}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2 ml-4 flex-shrink-0">
                <button
                  onClick={() => setOpenTask(task)}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm bg-blue-600 hover:bg-blue-500 text-white rounded-lg"
                >
                  <MessageCircle size={14} /> Open
                </button>
                <button
                  onClick={() => handleLeave(task.id)}
                  className="p-1.5 text-gray-400 hover:text-red-400 rounded-lg hover:bg-gray-700"
                >
                  <X size={16} />
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
