import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';
import type { SharedTaskReceived } from '../api/client';
import { RefreshCw, ExternalLink, X } from 'lucide-react';

export default function SharesPage() {
  const [tasks, setTasks] = useState<SharedTaskReceived[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
    } catch (e) {
      setError(String(e));
    }
  };

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

      {error && <p className="text-red-400 text-sm">{error}</p>}

      {!loading && tasks.length === 0 && (
        <div className="text-center py-16 text-gray-500">
          <p className="text-lg">No shared tasks yet</p>
          <p className="text-sm mt-2">When someone shares a task with you, it will appear here.</p>
        </div>
      )}

      <div className="grid gap-3">
        {tasks.map(task => (
          <div key={task.id} className="bg-gray-800 rounded-xl border border-gray-700 p-4">
            <div className="flex items-start justify-between">
              <div className="flex-1 min-w-0">
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
                <a
                  href={`${task.owner_ccm_url}/#/tasks/chat/${task.remote_task_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1 px-3 py-1.5 text-sm bg-blue-600 hover:bg-blue-500 text-white rounded-lg"
                >
                  <ExternalLink size={14} /> View
                </a>
                <button
                  onClick={() => handleLeave(task.id)}
                  className="p-1.5 text-gray-400 hover:text-red-400 rounded-lg hover:bg-gray-700"
                  title="Leave this shared task"
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
