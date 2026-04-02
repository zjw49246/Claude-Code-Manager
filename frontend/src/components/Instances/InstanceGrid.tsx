import { useState, useEffect } from 'react';
import { api } from '../../api/client';
import type { Instance } from '../../api/client';
import { Square, Trash2, Plus, Zap, ZapOff } from 'lucide-react';

interface InstanceGridProps {
  instances: Instance[];
  onRefresh: () => void;
  onViewLogs: (id: number) => void;
}

const statusColors: Record<string, string> = {
  idle: 'bg-gray-500',
  running: 'bg-green-500 animate-pulse',
  error: 'bg-red-500',
  stopped: 'bg-yellow-500',
};

export function InstanceGrid({ instances, onRefresh, onViewLogs }: InstanceGridProps) {
  const [newName, setNewName] = useState('');
  const [newModel, setNewModel] = useState('');
  const [dispatcherRunning, setDispatcherRunning] = useState(false);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState('');

  useEffect(() => {
    api.dispatcherStatus()
      .then((s) => setDispatcherRunning(s.running))
      .catch(() => {});
    api.config()
      .then((c) => { setModelOptions(c.model_options); setDefaultModel(c.default_model); })
      .catch(() => {});
  }, []);

  const handleCreate = async () => {
    const name = newName || `worker-${instances.length + 1}`;
    await api.createInstance({ name, model: newModel || 'default' });
    setNewName('');
    setNewModel('');
    onRefresh();
  };

  const handleDelete = async (id: number) => {
    await api.deleteInstance(id);
    onRefresh();
  };

  const handleStop = async (id: number) => {
    await api.stopInstance(id);
    onRefresh();
  };

  const toggleDispatcher = async () => {
    if (dispatcherRunning) {
      await api.stopDispatcher();
    } else {
      await api.startDispatcher();
    }
    setDispatcherRunning(!dispatcherRunning);
    onRefresh();
  };

  return (
    <div className="space-y-3">
      <div className="flex gap-2 flex-wrap">
        <input
          className="flex-1 min-w-[120px] bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          placeholder="Instance name (optional)"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
        />
        <select
          className="w-[180px] bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          value={newModel}
          onChange={(e) => setNewModel(e.target.value)}
        >
          <option value="">{defaultModel ? `Model (default: ${defaultModel})` : 'Model (default)'}</option>
          {modelOptions.map((m) => (
            <option key={m} value={m}>{m === 'default' && defaultModel ? `default (${defaultModel})` : m}</option>
          ))}
        </select>
        <button
          onClick={handleCreate}
          className="flex items-center gap-1 bg-indigo-600 hover:bg-indigo-700 text-white px-3 py-2 rounded text-sm font-medium whitespace-nowrap"
        >
          <Plus size={16} /> Add
        </button>
        <button
          onClick={toggleDispatcher}
          className={`flex items-center gap-1 px-3 py-2 rounded text-sm font-medium whitespace-nowrap ${
            dispatcherRunning
              ? 'bg-yellow-600/20 text-yellow-400 hover:bg-yellow-600/30'
              : 'bg-green-600/20 text-green-400 hover:bg-green-600/30'
          }`}
        >
          {dispatcherRunning ? <ZapOff size={16} /> : <Zap size={16} />}
          {dispatcherRunning ? 'Stop' : 'Start'} Dispatcher
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {instances.map((inst) => (
          <div key={inst.id} className="bg-gray-800 rounded-lg p-4 space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className={`w-2.5 h-2.5 rounded-full ${statusColors[inst.status] || 'bg-gray-500'}`} />
                <span className="text-foreground font-medium text-sm">{inst.name}</span>
              </div>
              <span className="text-xs text-gray-500">#{inst.id}</span>
            </div>

            <div className="text-xs text-gray-400 space-y-0.5">
              <p>Status: <span className="text-gray-300">{inst.status}</span></p>
              <p>Model: <span className="text-gray-300">{inst.model}{inst.model === 'default' && defaultModel ? ` (${defaultModel})` : ''}</span></p>
              <p>Completed: <span className="text-gray-300">{inst.total_tasks_completed}</span></p>
              {inst.current_task_id && <p>Task: <span className="text-indigo-400">#{inst.current_task_id}</span></p>}
              {inst.pid && <p>PID: <span className="text-gray-300">{inst.pid}</span></p>}
            </div>

            <div className="flex gap-1 pt-1">
              <button
                onClick={() => onViewLogs(inst.id)}
                className="px-2 py-1 rounded text-xs font-medium bg-gray-700 text-gray-300 hover:bg-gray-600"
              >
                Logs
              </button>
              {inst.status === 'running' && (
                <button onClick={() => handleStop(inst.id)} className="p-1 text-gray-400 hover:text-yellow-400" title="Stop">
                  <Square size={14} />
                </button>
              )}
              <button onClick={() => handleDelete(inst.id)} className="p-1 text-gray-400 hover:text-red-400 ml-auto" title="Delete">
                <Trash2 size={14} />
              </button>
            </div>
          </div>
        ))}
      </div>

      {instances.length === 0 && (
        <p className="text-gray-500 text-sm text-center py-8">No instances. Dispatcher will auto-create workers on start.</p>
      )}
    </div>
  );
}
