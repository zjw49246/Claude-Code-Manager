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
  const [newProvider, setNewProvider] = useState('claude');
  const [newModel, setNewModel] = useState('');
  const [newEffort, setNewEffort] = useState('');
  const [newThinkingBudget, setNewThinkingBudget] = useState('');
  const [dispatcherRunning, setDispatcherRunning] = useState(false);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState('');
  const [providerOptions, setProviderOptions] = useState<string[]>(['claude', 'codex']);
  const [codexModelOptions, setCodexModelOptions] = useState<string[]>([]);
  const [defaultCodexModel, setDefaultCodexModel] = useState('');
  const [effortOptions, setEffortOptions] = useState<string[]>([]);
  const [defaultEffort, setDefaultEffort] = useState('');

  useEffect(() => {
    api.dispatcherStatus()
      .then((s) => setDispatcherRunning(s.running))
      .catch(() => {});
    api.config()
      .then((c) => {
        setNewProvider(c.default_provider || 'claude');
        setProviderOptions(c.provider_options.length ? c.provider_options : ['claude', 'codex']);
        setModelOptions(c.model_options);
        setDefaultModel(c.default_model);
        setCodexModelOptions(c.codex_model_options);
        setDefaultCodexModel(c.default_codex_model);
        setEffortOptions(c.effort_options);
        setDefaultEffort(c.default_effort);
      })
      .catch(() => {});
  }, []);

  const activeDefaultModel = newProvider === 'codex' ? defaultCodexModel : defaultModel;
  const activeModelOptions = newProvider === 'codex' ? codexModelOptions : modelOptions;

  const handleCreate = async () => {
    const name = newName || `worker-${instances.length + 1}`;
    const parsedBudget = newThinkingBudget.trim() === '' ? null : Number(newThinkingBudget);
    const thinking_budget = parsedBudget !== null && Number.isFinite(parsedBudget) && parsedBudget > 0 ? parsedBudget : null;
    await api.createInstance({ name, provider: newProvider, model: newModel || 'default', effort_level: newEffort || null, thinking_budget });
    setNewName('');
    setNewProvider(newProvider);
    setNewModel('');
    setNewEffort('');
    setNewThinkingBudget('');
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
          className="w-[120px] bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          value={newProvider}
          onChange={(e) => {
            setNewProvider(e.target.value);
            setNewModel('');
          }}
        >
          {providerOptions.map((p) => (
            <option key={p} value={p}>{p === 'claude' ? 'Claude' : p === 'codex' ? 'Codex' : p}</option>
          ))}
        </select>
        <select
          className="w-[180px] bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          value={newModel}
          onChange={(e) => setNewModel(e.target.value)}
        >
          <option value="">{activeDefaultModel ? `Model (default: ${activeDefaultModel})` : 'Model (default)'}</option>
          {activeModelOptions.map((m) => (
            <option key={m} value={m}>{m === 'default' && activeDefaultModel ? `default (${activeDefaultModel})` : m}</option>
          ))}
        </select>
        <select
          className="w-[140px] bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          value={newEffort}
          onChange={(e) => setNewEffort(e.target.value)}
        >
          <option value="">{defaultEffort ? `Effort (${defaultEffort})` : 'Effort'}</option>
          {effortOptions.map((e) => (
            <option key={e} value={e}>{e}</option>
          ))}
        </select>
        <input
          type="number"
          inputMode="numeric"
          min={0}
          className="w-[140px] bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          placeholder="Thinking tokens"
          title="Optional Extended Thinking max tokens (MAX_THINKING_TOKENS). Leave empty for CLI default."
          value={newThinkingBudget}
          onChange={(e) => setNewThinkingBudget(e.target.value)}
        />
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
              <p>CLI: <span className="text-gray-300">{inst.provider === 'codex' ? 'Codex' : 'Claude'}</span></p>
              <p>Model: <span className="text-gray-300">{inst.model}{inst.model === 'default' ? ` (${inst.provider === 'codex' ? defaultCodexModel : defaultModel})` : ''}</span></p>
              <p>Effort: <span className="text-gray-300">{inst.effort_level || defaultEffort || 'medium'}</span></p>
              {inst.thinking_budget && inst.thinking_budget > 0 && (
                <p>Thinking: <span className="text-gray-300">{inst.thinking_budget.toLocaleString()} tok</span></p>
              )}
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
