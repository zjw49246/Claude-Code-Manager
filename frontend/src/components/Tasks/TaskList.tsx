import { useState, useMemo, useRef, useEffect, useCallback } from 'react';
import { api } from '../../api/client';
import type { Task, Project, SubAgentSummary } from '../../api/client';
import { Trash2, RotateCcw, XCircle, MessageCircle, Archive, ArchiveRestore, Star, Copy, Check, MoreVertical, Pencil, Mail, MailOpen, Wrench, Users } from 'lucide-react';
import { TAG_COLOR_OPTIONS } from '../TagColors';
import { ExpandableText } from '../ExpandableText';

interface TaskListProps {
  tasks: Task[];
  projects: Project[];
  onRefresh: () => void;
  onOpenChat: (task: Task) => void;
}

const statusColors: Record<string, string> = {
  pending: 'bg-yellow-500',
  in_progress: 'bg-blue-500',
  executing: 'bg-blue-400 animate-pulse',
  plan_review: 'bg-purple-500',
  completed: 'bg-green-500',
  failed: 'bg-red-500',
  cancelled: 'bg-gray-500',
};

export function TaskList({ tasks, projects, onRefresh, onOpenChat }: TaskListProps) {
  const projectMap = useMemo(() => {
    const map: Record<number, { name: string; color: string | null }> = {};
    for (const p of projects) map[p.id] = { name: p.name, color: p.badge_color };
    return map;
  }, [projects]);

  const [copiedId, setCopiedId] = useState<number | null>(null);
  const [menuOpenId, setMenuOpenId] = useState<number | null>(null);
  const [editingTitleId, setEditingTitleId] = useState<number | null>(null);
  const [titleDraft, setTitleDraft] = useState('');
  const [toolsExpandedId, setToolsExpandedId] = useState<number | null>(null);
  const [subAgentsExpandedId, setSubAgentsExpandedId] = useState<number | null>(null);
  const [subAgentSummary, setSubAgentSummary] = useState<SubAgentSummary | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const titleInputRef = useRef<HTMLInputElement>(null);

  // Close dropdowns on outside click
  useEffect(() => {
    if (toolsExpandedId === null && subAgentsExpandedId === null) return;
    const handleDropdownClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (toolsExpandedId !== null && !target.closest('[data-tools-dropdown]')) setToolsExpandedId(null);
      if (subAgentsExpandedId !== null && !target.closest('[data-subagents-dropdown]')) setSubAgentsExpandedId(null);
    };
    document.addEventListener('mousedown', handleDropdownClick);
    return () => document.removeEventListener('mousedown', handleDropdownClick);
  }, [toolsExpandedId, subAgentsExpandedId]);

  // Close overflow menu on outside click
  useEffect(() => {
    if (menuOpenId === null) return;
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpenId(null);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [menuOpenId]);

  // Auto-focus title input
  useEffect(() => {
    if (editingTitleId !== null) titleInputRef.current?.focus();
  }, [editingTitleId]);

  const handleDelete = async (id: number) => {
    await api.deleteTask(id);
    onRefresh();
  };
  const handleCancel = async (id: number) => {
    await api.cancelTask(id);
    onRefresh();
  };
  const handleRetry = async (id: number) => {
    await api.retryTask(id);
    onRefresh();
  };
  const handleStar = async (id: number) => {
    await api.starTask(id);
    onRefresh();
  };
  const handleToggleUnread = async (id: number, currentlyUnread: boolean) => {
    if (currentlyUnread) {
      await api.markTaskRead(id);
    } else {
      await api.markTaskUnread(id);
    }
    onRefresh();
  };
  const handleArchive = async (id: number) => {
    await api.archiveTask(id);
    onRefresh();
  };

  const handleCopy = async (t: Task) => {
    try {
      await navigator.clipboard.writeText(t.description || '');
      setCopiedId(t.id);
      setTimeout(() => setCopiedId(null), 2000);
    } catch { /* clipboard may fail in insecure context */ }
  };

  const handleTitleSave = async (t: Task) => {
    const trimmed = titleDraft.trim();
    setEditingTitleId(null);
    if (trimmed === (t.title || '')) return;
    try {
      await api.updateTask(t.id, { title: trimmed });
      onRefresh();
    } catch { /* ignore */ }
  };

  const handleSubAgentsToggle = useCallback(async (taskId: number) => {
    if (subAgentsExpandedId === taskId) {
      setSubAgentsExpandedId(null);
      setSubAgentSummary(null);
      return;
    }
    setSubAgentSummary(null);
    try {
      const summary = await api.getSubAgentSummary(taskId);
      setSubAgentSummary(summary);
      setSubAgentsExpandedId(taskId);
    } catch {
      setSubAgentSummary({ by_type: {} });
      setSubAgentsExpandedId(taskId);
    }
  }, [subAgentsExpandedId]);

  if (tasks.length === 0) {
    return <p className="text-gray-500 text-sm text-center py-8">No tasks yet</p>;
  }

  return (
    <div className="space-y-2">
      {tasks.map((t) => (
        <div key={t.id} className={`rounded-lg p-3 ${t.has_unread ? 'bg-indigo-900/50 ring-1 ring-indigo-500/50' : 'bg-gray-800'}`}>
          {/* Row 1: status dot + badges (left, wraps) | action buttons (right, no wrap) */}
          <div className="flex items-center gap-2">
            <span className={`w-2.5 h-2.5 rounded-full shrink-0 self-start mt-[9px] ${statusColors[t.status] || 'bg-gray-500'}`} />
            <div className="flex items-center gap-2 flex-wrap flex-1 min-w-0 min-h-[28px]">
              <span className="text-xs text-gray-500">#{t.id}</span>
              {t.project_id && projectMap[t.project_id] && (() => {
                const proj = projectMap[t.project_id!];
                const colorDef = TAG_COLOR_OPTIONS.find((c) => c.key === proj.color);
                const bg = colorDef ? colorDef.bg : 'bg-emerald-600/30';
                const text = colorDef ? colorDef.text : 'text-emerald-300';
                return <span className={`text-xs ${bg} ${text} px-1.5 rounded font-medium whitespace-nowrap`}>{proj.name}</span>;
              })()}
              {t.priority > 0 && (
                <span className="text-xs bg-indigo-600/30 text-indigo-300 px-1.5 rounded">P{t.priority}</span>
              )}
              <span className="hidden sm:inline text-xs text-gray-500 capitalize">{t.status.replace('_', ' ')}</span>
              <span className={`hidden sm:inline text-xs px-1.5 rounded font-medium ${t.provider === 'codex' ? 'bg-green-600/30 text-green-300' : 'bg-blue-600/30 text-blue-300'}`}>
                {t.provider === 'codex' ? 'Codex' : 'Claude'}
              </span>
              {t.model && (
                <span className="hidden sm:inline text-xs bg-gray-700 text-gray-300 px-1.5 rounded">{t.model}</span>
              )}
              <div className="relative" data-tools-dropdown>
                <button
                  onClick={() => setToolsExpandedId(toolsExpandedId === t.id ? null : t.id)}
                  className="text-xs bg-amber-600/30 text-amber-300 px-1.5 rounded cursor-pointer hover:bg-amber-600/40 flex items-center gap-0.5"
                >
                  <Wrench size={12} />
                  {t.enabled_skills ? Object.values(t.enabled_skills).filter(Boolean).length : 0}
                </button>
                {toolsExpandedId === t.id && (
                  <div className="absolute top-full mt-1 left-0 bg-gray-800 border border-gray-600 rounded shadow-lg z-20 min-w-[140px] py-1">
                    {t.enabled_skills && Object.entries(t.enabled_skills).filter(([, v]) => v).length > 0 ? (
                      Object.entries(t.enabled_skills).filter(([, v]) => v).map(([skill]) => (
                        <div key={skill} className="px-3 py-1 text-xs text-gray-300 flex items-center gap-1.5">
                          <span className="text-green-400">✓</span>
                          {skill.charAt(0).toUpperCase() + skill.slice(1)}
                        </div>
                      ))
                    ) : (
                      <div className="px-3 py-1 text-xs text-gray-500">No tools enabled</div>
                    )}
                  </div>
                )}
              </div>
              <div className="relative" data-subagents-dropdown>
                <button
                  onClick={() => handleSubAgentsToggle(t.id)}
                  className={`text-xs bg-teal-600/30 text-teal-300 px-1.5 rounded cursor-pointer hover:bg-teal-600/40 flex items-center gap-0.5${t.active_sub_agents > 0 ? ' animate-pulse' : ''}`}
                >
                  <Users size={12} />
                  {t.active_sub_agents}
                </button>
              </div>
            </div>
            {/* Action buttons — always top-right aligned */}
            <div className="flex gap-1 shrink-0 items-center">
              <button
                onClick={() => handleStar(t.id)}
                className={`p-1.5 transition-colors ${t.starred ? 'text-yellow-400 hover:text-yellow-300' : 'text-gray-600 hover:text-yellow-400'}`}
                title={t.starred ? "Unstar" : "Star"}
              >
                <Star size={16} fill={t.starred ? 'currentColor' : 'none'} />
              </button>
              <button
                onClick={() => handleToggleUnread(t.id, t.has_unread)}
                className={`p-1.5 transition-colors ${t.has_unread ? 'text-indigo-400 hover:text-indigo-300' : 'text-gray-600 hover:text-indigo-400'}`}
                title={t.has_unread ? "Mark as read" : "Mark as unread"}
              >
                {t.has_unread ? <MailOpen size={16} /> : <Mail size={16} />}
              </button>
              {t.session_id && (
                <button
                  onClick={() => onOpenChat(t)}
                  className="flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-indigo-600/20 text-indigo-400 hover:bg-indigo-600/30"
                  title="Chat"
                >
                  <MessageCircle size={14} /><span className="hidden sm:inline"> Chat</span>
                </button>
              )}
              <button
                onClick={() => handleCopy(t)}
                className="p-1.5 text-gray-600 hover:text-gray-300 transition-colors"
                title="Copy prompt"
              >
                {copiedId === t.id ? <Check size={16} className="text-green-400" /> : <Copy size={16} />}
              </button>
              {/* Overflow menu */}
              <div className="relative" ref={menuOpenId === t.id ? menuRef : undefined}>
                <button
                  onClick={() => setMenuOpenId(menuOpenId === t.id ? null : t.id)}
                  className="p-1.5 text-gray-600 hover:text-gray-300 transition-colors"
                  title="More actions"
                >
                  <MoreVertical size={16} />
                </button>
                {menuOpenId === t.id && (
                  <div className="absolute right-0 top-8 z-50 bg-gray-900 border border-gray-700 rounded-lg shadow-xl py-1 min-w-[140px]">
                    <button
                      onClick={() => { setTitleDraft(t.title || ''); setEditingTitleId(t.id); setMenuOpenId(null); }}
                      className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800 text-left"
                    >
                      <Pencil size={14} /> Edit title
                    </button>
                    {['in_progress', 'executing'].includes(t.status) && (
                      <button
                        onClick={() => { handleCancel(t.id); setMenuOpenId(null); }}
                        className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-yellow-400 hover:bg-gray-800 text-left"
                      >
                        <XCircle size={14} /> Cancel
                      </button>
                    )}
                    {t.status === 'failed' && (
                      <button
                        onClick={() => { handleRetry(t.id); setMenuOpenId(null); }}
                        className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-blue-400 hover:bg-gray-800 text-left"
                      >
                        <RotateCcw size={14} /> Retry
                      </button>
                    )}
                    <button
                      onClick={() => { handleArchive(t.id); setMenuOpenId(null); }}
                      className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-amber-400 hover:bg-gray-800 text-left"
                    >
                      {t.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
                      {t.archived ? 'Unarchive' : 'Archive'}
                    </button>
                    {['pending', 'failed', 'cancelled', 'completed'].includes(t.status) && (
                      <button
                        onClick={() => { handleDelete(t.id); setMenuOpenId(null); }}
                        className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-red-400 hover:bg-gray-800 text-left"
                      >
                        <Trash2 size={14} /> Delete
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
          {/* Expandable sub-agents detail */}
          {subAgentsExpandedId === t.id && (
            <div className="mt-1 pl-[1.125rem] flex flex-wrap gap-1" data-subagents-dropdown>
              {subAgentSummary && Object.entries(subAgentSummary.by_type).some(([, c]) => c.running > 0) ? (
                Object.entries(subAgentSummary.by_type).filter(([, c]) => c.running > 0).map(([type, counts]) => (
                  <span key={type} className="text-xs bg-teal-600/20 text-teal-300 px-2 py-0.5 rounded flex items-center gap-1">
                    {type.charAt(0).toUpperCase() + type.slice(1)}:
                    <span className="text-green-400">{counts.running} running</span>
                  </span>
                ))
              ) : (
                <span className="text-xs text-gray-500">No active sub-agents</span>
              )}
            </div>
          )}
          {/* Row 2: title + description (full width) */}
          <div className="mt-1 pl-[1.125rem]">
            {/* Title (editable) */}
            {editingTitleId === t.id ? (
              <input
                ref={titleInputRef}
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onBlur={() => handleTitleSave(t)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleTitleSave(t);
                  if (e.key === 'Escape') setEditingTitleId(null);
                }}
                className="w-full bg-gray-700 text-foreground text-sm rounded px-2 py-0.5 mt-0.5 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                placeholder="Enter title..."
              />
            ) : (
              t.title ? (
                <p className="text-foreground text-sm font-medium mt-0.5 line-clamp-1">{t.title}</p>
              ) : null
            )}
            {/* Description (expandable) */}
            {t.mode === 'loop' && !t.description ? (
              <p className="text-sm mt-0.5 text-gray-500 italic">{t.todo_file_path}</p>
            ) : t.description ? (
              <ExpandableText
                text={t.description}
                collapsedLines={2}
                className={`text-sm mt-0.5 ${t.title ? 'text-gray-400' : 'text-foreground'}`}
              />
            ) : null}
            {t.mode === 'goal' && t.goal_condition && (
              <p className="text-emerald-500/70 text-xs mt-0.5 line-clamp-1">Goal: {t.goal_condition}</p>
            )}
            {t.mode === 'loop' && t.loop_progress && (
              <p className="text-indigo-400 text-xs mt-0.5">⟳ {t.loop_progress}</p>
            )}
            {t.mode === 'goal' && t.goal_turns_used > 0 && (
              <p className="text-emerald-400 text-xs mt-0.5">
                ◎ Turn {t.goal_turns_used}/{t.goal_max_turns}
                {t.goal_last_reason && <span className="text-gray-500 ml-1">— {t.goal_last_reason}</span>}
              </p>
            )}
            {t.target_repo && (
              <p className="text-gray-600 text-xs mt-0.5 truncate">{t.target_repo}</p>
            )}
            {t.error_message && (
              <p className="text-red-400 text-xs mt-1">{t.error_message}</p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
