import { useState, useEffect, useCallback } from 'react';
import { Wrench, Users, Settings, Server } from 'lucide-react';
import { api } from '../../api/client';
import type { Task, SubAgentSummary, Worker } from '../../api/client';

// workers 列表页级缓存（RunOnBadge 在列表里逐行渲染，避免 N 次请求）
let workersCache: Worker[] | null = null;
let workersPromise: Promise<Worker[]> | null = null;
function fetchWorkersCached(): Promise<Worker[]> {
  if (workersCache) return Promise.resolve(workersCache);
  if (!workersPromise) {
    workersPromise = api.listWorkers().then((ws) => { workersCache = ws; return ws; })
      .catch(() => { workersPromise = null; return []; });
  }
  return workersPromise;
}

/** 任务运行位置徽章：跑在 worker 上时显示 worker 名；本机不显示。 */
export function RunOnBadge({ task }: { task: Task }) {
  const [name, setName] = useState<string | null>(null);
  useEffect(() => {
    if (task.worker_id == null) { setName(null); return; }
    let cancelled = false;
    fetchWorkersCached().then((ws) => {
      if (cancelled) return;
      setName(ws.find((w) => w.id === task.worker_id)?.name || `worker #${task.worker_id}`);
    });
    return () => { cancelled = true; };
  }, [task.worker_id]);
  if (task.worker_id == null) return null;
  return (
    <span
      className="text-xs bg-sky-600/30 text-sky-300 px-1.5 rounded whitespace-nowrap inline-flex items-center gap-0.5"
      title="运行位置（在 Config 里可迁移）"
    >
      <Server size={11} />
      {name ?? `#${task.worker_id}`}
    </span>
  );
}

export const ALL_TOOLS = [
  { key: 'help', label: 'Help' },
  { key: 'workflows', label: 'Workflows' },
  { key: 'monitor', label: 'Monitor' },
];

/** Wrench badge with a dropdown to toggle per-task tools (shared by the
 * task list and the split-mode sidebar). */
export function ToolsBadge({ task, onRefresh }: { task: Task; onRefresh: () => void }) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const handle = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest('[data-tools-dropdown]')) setOpen(false);
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [open]);

  return (
    <div className="relative" data-tools-dropdown>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
        className="text-xs bg-amber-600/30 text-amber-300 px-1.5 rounded cursor-pointer hover:bg-amber-600/40 flex items-center gap-0.5"
        title="Tools"
      >
        <Wrench size={12} />
        {task.enabled_skills ? Object.values(task.enabled_skills).filter(Boolean).length : 0}
      </button>
      {open && (
        <div className="absolute top-full mt-1 left-0 bg-gray-800 border border-gray-600 rounded shadow-lg z-20 min-w-[160px] py-1">
          {ALL_TOOLS.map((tool) => {
            const enabled = !!(task.enabled_skills && task.enabled_skills[tool.key]);
            return (
              <button
                key={tool.key}
                onClick={async (e) => {
                  e.stopPropagation();
                  const newSkills = { ...(task.enabled_skills || {}), [tool.key]: !enabled };
                  try {
                    await api.updateTask(task.id, { enabled_skills: newSkills });
                    onRefresh();
                  } catch { /* keep current state */ }
                }}
                className="w-full px-3 py-1.5 text-xs text-left flex items-center gap-2 hover:bg-gray-700 transition-colors"
              >
                <span className={`w-3.5 h-3.5 rounded border flex items-center justify-center text-[9px] ${
                  enabled ? 'bg-green-600 border-green-500 text-white' : 'border-gray-600'
                }`}>
                  {enabled && '✓'}
                </span>
                <span className={enabled ? 'text-gray-200' : 'text-gray-400'}>{tool.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Sub-agents badge with a summary dropdown (shared by the task list and
 * the split-mode sidebar). */
export function SubAgentsBadge({ task }: { task: Task }) {
  const [open, setOpen] = useState(false);
  const [summary, setSummary] = useState<SubAgentSummary | null>(null);

  useEffect(() => {
    if (!open) return;
    const handle = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest('[data-subagents-dropdown]')) setOpen(false);
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [open]);

  const toggle = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (open) {
      setOpen(false);
      setSummary(null);
      return;
    }
    setSummary(null);
    try {
      setSummary(await api.getSubAgentSummary(task.id));
    } catch {
      setSummary({ by_type: {} });
    }
    setOpen(true);
  }, [open, task.id]);

  return (
    <div className="relative" data-subagents-dropdown>
      <button
        onClick={toggle}
        className={`text-xs bg-teal-600/30 text-teal-300 px-1.5 rounded cursor-pointer hover:bg-teal-600/40 flex items-center gap-0.5${task.active_sub_agents > 0 ? ' animate-pulse' : ''}`}
        title="Sub-agents"
      >
        <Users size={12} />
        {task.active_sub_agents}
      </button>
      {open && (
        <div className="absolute top-full mt-1 left-0 bg-gray-800 border border-gray-600 rounded shadow-lg z-20 min-w-[140px] py-1">
          {summary && Object.keys(summary.by_type).length > 0 ? (
            Object.entries(summary.by_type).map(([type, counts]) => (
              <div key={type} className="px-3 py-1 text-xs text-gray-300 flex items-center justify-between gap-3">
                <span>{type.charAt(0).toUpperCase() + type.slice(1)}</span>
                <span className={counts.running > 0 ? 'text-green-400' : 'text-gray-500'}>{counts.running} running</span>
              </div>
            ))
          ) : (
            <div className="px-3 py-1 text-xs text-gray-500">No sub-agents</div>
          )}
        </div>
      )}
    </div>
  );
}

// Config options cache (fetched once per page load)
interface ConfigOptions {
  claude: string[]; codex: string[];
  effort: string[]; codexEffort: string[];
}
let _configOptionsCache: ConfigOptions | null = null;
async function fetchConfigOptions(): Promise<ConfigOptions> {
  if (_configOptionsCache) return _configOptionsCache;
  const c = await api.config();
  _configOptionsCache = {
    claude: c.model_options.filter((m) => m !== 'default'),
    codex: c.codex_model_options.filter((m) => m !== 'default'),
    effort: c.effort_options,
    codexEffort: c.codex_effort_options,
  };
  return _configOptionsCache;
}
const fetchModelOptions = fetchConfigOptions;

/** Clickable model badge: dropdown to switch the task's model (persisted). */
export function ModelBadge({ task, onRefresh, compact }: { task: Task; onRefresh: () => void; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<string[]>([]);

  useEffect(() => {
    if (!open) return;
    fetchModelOptions().then((o) => setOptions(task.provider === 'codex' ? o.codex : o.claude)).catch(() => {});
    const handle = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest('[data-model-dropdown]')) setOpen(false);
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [open, task.provider]);

  const label = task.model || 'default';

  return (
    <div className="relative" data-model-dropdown>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
        className={`text-xs bg-gray-700 text-gray-300 px-1.5 rounded cursor-pointer hover:bg-gray-600 hover:text-gray-100 ${compact ? 'max-w-[120px] truncate' : ''}`}
        title="切换模型（持久化到该任务）"
      >
        {label}
      </button>
      {open && (
        <div className="absolute top-full mt-1 left-0 bg-gray-800 border border-gray-600 rounded shadow-lg z-20 min-w-[180px] py-1 max-h-60 overflow-y-auto">
          {options.length === 0 && (
            <div className="px-3 py-1.5 text-xs text-gray-500">Loading…</div>
          )}
          {options.map((m) => (
            <button
              key={m}
              onClick={async (e) => {
                e.stopPropagation();
                setOpen(false);
                if (m === task.model) return;
                try {
                  await api.updateTask(task.id, { model: m });
                  onRefresh();
                } catch { /* keep current */ }
              }}
              className={`w-full px-3 py-1.5 text-xs text-left transition-colors hover:bg-gray-700 ${
                m === task.model ? 'text-indigo-300 bg-indigo-600/20' : 'text-gray-300'
              }`}
            >
              {m}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}


const TIMEOUT_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'default' },
  { value: '0.5', label: '30 min' },
  { value: '1', label: '1 hour' },
  { value: '2', label: '2 hours' },
  { value: '4', label: '4 hours' },
  { value: '8', label: '8 hours' },
  { value: '12', label: '12 hours' },
  { value: '24', label: '24 hours' },
  { value: '0', label: 'No limit' },
];

const THINKING_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'default' },
  { value: '4096', label: '4k' },
  { value: '8192', label: '8k' },
  { value: '16384', label: '16k' },
  { value: '32768', label: '32k' },
  { value: '65536', label: '64k' },
  { value: '131072', label: '128k' },
];

/** Per-task Config: gear button opening a panel to edit Model / Effort /
 * Timeout / Thinking in place (each change persists via updateTask).
 * Shared by the task list, the sidebar, and the chat header. */
export function TaskConfigBadge({ task, onRefresh, openUp, align }: { task: Task; onRefresh: () => void; openUp?: boolean; align?: 'left' | 'right' }) {
  const [open, setOpen] = useState(false);
  const [opts, setOpts] = useState<ConfigOptions | null>(null);
  const [workers, setWorkers] = useState<{ id: number; name: string; status: string }[]>([]);
  const [migrating, setMigrating] = useState(false);

  useEffect(() => {
    if (!open) return;
    api.listWorkers().then(setWorkers).catch(() => {});
  }, [open]);

  useEffect(() => {
    if (!open) return;
    fetchConfigOptions().then(setOpts).catch(() => {});
    const handle = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest('[data-task-config]')) setOpen(false);
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [open]);

  const update = async (data: Parameters<typeof api.updateTask>[1]) => {
    try {
      await api.updateTask(task.id, data);
      onRefresh();
    } catch { /* keep current */ }
  };

  const isCodex = task.provider === 'codex';
  const models = opts ? (isCodex ? opts.codex : opts.claude) : [];
  const efforts = opts ? (isCodex ? opts.codexEffort : opts.effort) : [];

  return (
    <div className="relative" data-task-config>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
        className="text-xs bg-gray-700 text-gray-300 px-1.5 rounded cursor-pointer hover:bg-gray-600 hover:text-gray-100 flex items-center gap-0.5"
        title={`Config（model: ${task.model || 'default'}）`}
      >
        <Settings size={12} />
        Config
      </button>
      {open && (
        <div
          className={`absolute ${openUp ? 'bottom-full mb-1' : 'top-full mt-1'} ${align === 'right' ? 'right-0' : 'left-0'} bg-gray-800 border border-gray-600 rounded shadow-lg z-30 p-3 min-w-[250px] max-w-[calc(100vw-1rem)]`}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 items-center text-xs">
            <span className="text-gray-400">Run on</span>
            <select
              className="bg-gray-700 text-foreground rounded px-2 py-1 text-xs disabled:opacity-50"
              value={task.worker_id == null ? '' : String(task.worker_id)}
              disabled={migrating || task.status === 'executing' || task.status === 'migrating'}
              title={task.status === 'executing' ? '运行中不能切换，先 Stop' : '切换执行位置（迁移 session + 工作目录）'}
              onChange={async (e) => {
                const target = e.target.value === '' ? -1 : parseInt(e.target.value);
                if ((target === -1 && task.worker_id == null) || target === task.worker_id) return;
                setMigrating(true);
                try {
                  await api.updateTask(task.id, { worker_id: target });
                  onRefresh();
                } catch (err) {
                  window.alert(String(err));
                } finally {
                  setMigrating(false);
                }
              }}
            >
              <option value="">本机</option>
              {workers.map((w) => (
                <option key={w.id} value={w.id} disabled={w.status !== 'ready'}>
                  {w.name}{w.status !== 'ready' ? ` (${w.status})` : ''}
                </option>
              ))}
            </select>

            <span className="text-gray-400">Model</span>
            <select
              className="bg-gray-700 text-foreground rounded px-2 py-1 text-xs"
              value={task.model || ''}
              onChange={(e) => update({ model: e.target.value })}
            >
              {task.model && !models.includes(task.model) && (
                <option value={task.model}>{task.model}</option>
              )}
              {models.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>

            <span className="text-gray-400">Effort</span>
            <select
              className="bg-gray-700 text-foreground rounded px-2 py-1 text-xs"
              value={task.effort_level || ''}
              onChange={(e) => update({ effort_level: e.target.value })}
            >
              {!task.effort_level && <option value="">default</option>}
              {efforts.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>

            <span className="text-gray-400">Timeout</span>
            <select
              className="bg-gray-700 text-foreground rounded px-2 py-1 text-xs"
              value={task.timeout_hours == null ? '' : String(task.timeout_hours)}
              onChange={(e) => update({ timeout_hours: e.target.value === '' ? null : Number(e.target.value) })}
            >
              {TIMEOUT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>

            <span className="text-gray-400">Thinking</span>
            <select
              className="bg-gray-700 text-foreground rounded px-2 py-1 text-xs"
              value={task.thinking_budget == null ? '' : String(task.thinking_budget)}
              onChange={(e) => update({ thinking_budget: e.target.value === '' ? null : Number(e.target.value) })}
            >
              {THINKING_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          <div className="mt-2 text-[10px] text-gray-500">修改在下一轮对话生效</div>
        </div>
      )}
    </div>
  );
}
