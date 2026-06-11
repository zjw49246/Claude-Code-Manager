import { useState, useEffect, useCallback } from 'react';
import { Wrench, Users } from 'lucide-react';
import { api } from '../../api/client';
import type { Task, SubAgentSummary } from '../../api/client';

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

// Model options cache (fetched once per page load)
let _modelOptionsCache: { claude: string[]; codex: string[] } | null = null;
async function fetchModelOptions(): Promise<{ claude: string[]; codex: string[] }> {
  if (_modelOptionsCache) return _modelOptionsCache;
  const c = await api.config();
  _modelOptionsCache = {
    claude: c.model_options.filter((m) => m !== 'default'),
    codex: c.codex_model_options.filter((m) => m !== 'default'),
  };
  return _modelOptionsCache;
}

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
