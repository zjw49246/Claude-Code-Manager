import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../api/client';
import type { Task, Project, TagItem } from '../api/client';
import { useWebSocket } from '../hooks/useWebSocket';
import { TaskForm } from '../components/Tasks/TaskForm';
import { TaskList } from '../components/Tasks/TaskList';
import { PlanPanel } from '../components/PlanReview/PlanPanel';
import { ChatView } from '../components/Chat/ChatView';
import { LoopChatView } from '../components/Chat/LoopChatView';
import { ProjectSelect } from '../components/ProjectSelect';
import { resolveTagColor } from '../components/TagColors';
import { ChevronLeft, ChevronRight, ChevronDown, Filter, PanelLeftClose, PanelLeftOpen, Search, X, Star, Archive, ArchiveRestore, Share2 } from 'lucide-react';
import { PluginsBadge, SubAgentsBadge } from '../components/Tasks/TaskBadges';
import { TAG_COLOR_OPTIONS } from '../components/TagColors';
import { useTaskReorder } from '../hooks/useTaskReorder';
import { ShareModal } from '../components/ShareModal';

const PAGE_SIZE = 20;

interface TasksPageProps {
  chatTaskId: number | null;
  onChatTaskChange: (id: number | null) => void;
}

export function TasksPage({ chatTaskId, onChatTaskChange }: TasksPageProps) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [allTasks, setAllTasks] = useState<Task[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [page, setPage] = useState(1);
  const [projects, setProjects] = useState<Project[]>([]);
  const [statusFilters, setStatusFilters] = useState<string[]>([]);
  const [tagFilters, setTagFilters] = useState<string[]>([]);
  const [projectFilter, setProjectFilter] = useState<number | undefined>(undefined);
  const [starredFilter, setStarredFilter] = useState(false);
  const [unreadFilter, setUnreadFilter] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [tagItems, setTagItems] = useState<TagItem[]>([]);
  const [chatTask, setChatTask] = useState<Task | null>(null);
  const [sharingTask, setSharingTask] = useState<Task | null>(null);
  const chatTaskRef = useRef<Task | null>(null);
  chatTaskRef.current = chatTask;
  const chatTaskIdRef = useRef(chatTaskId);
  chatTaskIdRef.current = chatTaskId;
  const skipFreezeOnce = useRef(false);

  const setChatTaskWrapped = useCallback((t: Task | null) => {
    setChatTask(t);
    onChatTaskChange(t?.id ?? null);
  }, [onChatTaskChange]);

  // Listen for share-task events from TaskList menu
  useEffect(() => {
    const handler = (e: Event) => {
      const task = (e as CustomEvent).detail?.task;
      if (task) setSharingTask(task);
    };
    window.addEventListener('ccm-share-task', handler);
    return () => window.removeEventListener('ccm-share-task', handler);
  }, []);

  const [autoSortOnAccess, setAutoSortOnAccess] = useState(true);
  useEffect(() => {
    api.getRuntimeSettings().then((s) => setAutoSortOnAccess(s.auto_sort_on_access)).catch(() => {});
  }, []);
  const handleSystemWs = useCallback((raw: Record<string, unknown>) => {
    const msg = raw as { channel?: string; data?: Record<string, unknown> };
    if (msg.channel === 'system' && msg.data?.event === 'runtime_settings_changed') {
      setAutoSortOnAccess(Boolean(msg.data.auto_sort_on_access));
    }
  }, []);
  useWebSocket(['system'], handleSystemWs);

  const [isWide, setIsWide] = useState(() => window.innerWidth >= 1280);
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1280px)');
    const handler = (e: MediaQueryListEvent) => setIsWide(e.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);

  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const statusFilterParam = statusFilters.length > 0 ? statusFilters.join(',') : undefined;

  const refresh = useCallback(async () => {
    try {
      const offset = (page - 1) * PAGE_SIZE;
      const [filtered, count, all, projs, tags] = await Promise.all([
        api.listTasks(statusFilterParam, false, projectFilter, starredFilter || undefined, PAGE_SIZE, offset, showArchived, unreadFilter || undefined),
        api.countTasks(statusFilterParam, false, projectFilter, starredFilter || undefined, showArchived, unreadFilter || undefined),
        api.listTasks(undefined, false, undefined, undefined, PAGE_SIZE, 0, showArchived),
        api.listProjects(),
        api.listTags(),
      ]);
      // When chat is open, freeze sidebar order: update task data in place and
      // append genuinely-new tasks at the end, but NEVER drop a task just because
      // it fell off page-1. With 20/page over hundreds of tasks, an active task
      // (loop/executing) bumping last_accessed_at constantly crosses the page-1
      // boundary; the old code dropped the task that fell off and re-appended the
      // returning one at the end, so the list churned/flickered every few seconds.
      // Keeping fallen-off tasks in place (with last-known data until they return)
      // is what "freeze" should mean — the full server order is restored on close.
      if (chatTaskRef.current && !skipFreezeOnce.current) {
        setTasks(prev => {
          const byId = new Map(filtered.map(t => [t.id, t]));
          const updated = prev.map(t => byId.get(t.id) ?? t);
          const prevIds = new Set(prev.map(t => t.id));
          const added = filtered.filter(t => !prevIds.has(t.id));
          return [...updated, ...added];
        });
      } else {
        skipFreezeOnce.current = false;
        setTasks(filtered);
      }
      setTotalCount(count.total);
      setAllTasks(all);
      setProjects(projs);
      setTagItems(tags);
      // Resolve chatTaskId from URL on first load, or update open chatTask.
      // chatTaskId comes from URL hash — null after onBack.
      // chatTaskRef tracks the currently open chat — used to refresh its data.
      const currentChatTaskId = chatTaskIdRef.current;
      if (currentChatTaskId) {
        const pool = [...filtered, ...all];
        let found = pool.find((t) => t.id === currentChatTaskId);
        if (!found) {
          try { found = await api.getTask(currentChatTaskId); } catch { /* task may not exist */ }
        }
        if (found) setChatTaskWrapped(found);
      } else if (chatTaskRef.current) {
        // Chat is open but not from URL (e.g. clicked from list) — refresh its data
        const found = [...filtered, ...all].find((t) => t.id === chatTaskRef.current!.id);
        if (found) setChatTask(found);
      }
    } catch (e) {
      console.error('Failed to load tasks:', e);
    }
  }, [statusFilterParam, showArchived, projectFilter, starredFilter, unreadFilter, page, setChatTaskWrapped]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  // Reset to page 1 when filters change
  const prevFilter = useRef({ statusFilterParam, showArchived, projectFilter, starredFilter, unreadFilter });
  useEffect(() => {
    const prev = prevFilter.current;
    if (prev.statusFilterParam !== statusFilterParam || prev.showArchived !== showArchived || prev.projectFilter !== projectFilter || prev.starredFilter !== starredFilter || prev.unreadFilter !== unreadFilter) {
      setPage(1);
      prevFilter.current = { statusFilterParam, showArchived, projectFilter, starredFilter, unreadFilter };
    }
  }, [statusFilterParam, showArchived, projectFilter, starredFilter, unreadFilter]);

  // Regex search over task titles (falls back to plain substring on invalid regex)
  const [showSearch, setShowSearch] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<Task[] | null>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const q = searchQuery.trim();
    if (!q) {
      setSearchResults(null);
      return;
    }
    const handle = setTimeout(async () => {
      try {
        // Fetch across all pages so the search isn't limited to the current page
        const all = await api.listTasks(undefined, false, undefined, undefined, 1000, 0, showArchived);
        let re: RegExp | null = null;
        try { re = new RegExp(q, 'i'); } catch { re = null; }
        const matches = all.filter((t) => {
          const title = t.title || t.description || '';
          return re ? re.test(title) : title.toLowerCase().includes(q.toLowerCase());
        });
        setSearchResults(matches);
      } catch {
        setSearchResults([]);
      }
    }, 300);
    return () => clearTimeout(handle);
  }, [searchQuery, showArchived]);

  const statusOptions = ['pending', 'in_progress', 'executing', 'plan_review', 'completed', 'failed'];
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
  const filterDropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!showFilterDropdown) return;
    const handleClick = (e: MouseEvent) => {
      if (filterDropdownRef.current && !filterDropdownRef.current.contains(e.target as Node)) {
        setShowFilterDropdown(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showFilterDropdown]);

  const statusLabels: Record<string, string> = {
    pending: 'Pending',
    in_progress: 'In Progress',
    executing: 'Executing',
    plan_review: 'Plan Review',
    completed: 'Completed',
    failed: 'Failed',
  };

  const statusDotColors: Record<string, string> = {
    pending: 'bg-yellow-500',
    in_progress: 'bg-blue-500',
    executing: 'bg-blue-400',
    plan_review: 'bg-purple-500',
    completed: 'bg-green-500',
    failed: 'bg-red-500',
  };

  const activeFilterCount = statusFilters.length + (starredFilter ? 1 : 0) + (unreadFilter ? 1 : 0) + (showArchived ? 1 : 0) + tagFilters.length;

  const visibleProjects = projects.filter((p) => p.show_in_selector);

  // Collect all unique tags from visible projects + tag registry
  const allProjectTags = Array.from(new Set([...visibleProjects.flatMap((p) => p.tags), ...tagItems.map((t) => t.name)])).sort();

  // Build tag color map
  const tagColorMap: Record<string, string> = {};
  for (const t of tagItems) tagColorMap[t.name] = t.color;

  // Projects filtered by tag (for the project dropdown)
  const tagFilteredProjects = tagFilters.length > 0
    ? visibleProjects.filter((p) => tagFilters.some((t) => p.tags.includes(t)))
    : visibleProjects;

  const splitMode = isWide && chatTask;

  const filteredTasks = tagFilters.length > 0
    ? tasks.filter((t) => {
        if (!t.project_id) return false;
        const proj = projects.find((p) => p.id === t.project_id);
        return proj ? tagFilters.some((tag) => proj.tags.includes(tag)) : false;
      })
    : tasks;

  // 侧边栏拖拽排序（与主列表同一套逻辑）
  const sidebarTasks = searchResults ?? filteredTasks;
  const reorderRefresh = useCallback((optimistic?: Task[]) => {
    if (optimistic) {
      setTasks(optimistic);
    }
    skipFreezeOnce.current = true;
    refresh();
  }, [refresh]);
  const sidebarReorder = useTaskReorder(sidebarTasks, reorderRefresh, autoSortOnAccess);

  const handleOpenChat = useCallback((t: Task) => {
    setChatTaskWrapped(t);
    if (t.has_unread) {
      api.markTaskRead(t.id).catch(() => {});
    }
  }, [setChatTaskWrapped]);

  // Filter / Projects / Search controls — shared between the full task list
  // and the split-mode sidebar
  const filterControls = (
      <div className="flex gap-2 flex-wrap items-center">
        <div className="relative" ref={filterDropdownRef}>
          <button
            onClick={() => setShowFilterDropdown(!showFilterDropdown)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium transition-colors ${
              activeFilterCount > 0
                ? 'bg-indigo-600 text-white'
                : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
            }`}
          >
            <Filter size={12} />
            Filter
            {activeFilterCount > 0 && (
              <span className="bg-white/20 text-white px-1.5 rounded-full text-[10px]">{activeFilterCount}</span>
            )}
            <ChevronDown size={12} className={`transition-transform ${showFilterDropdown ? 'rotate-180' : ''}`} />
          </button>
          {showFilterDropdown && (
            <div className="absolute top-full mt-1 left-0 bg-gray-900 border border-gray-700 rounded-lg shadow-xl z-30 min-w-[180px] py-1 max-h-72 overflow-y-auto">
              {/* Status section */}
              <div className="px-3 py-1 text-[10px] text-gray-500 uppercase tracking-wider">Status</div>
              {statusOptions.map((f) => {
                const checked = statusFilters.includes(f);
                return (
                  <button
                    key={f}
                    onClick={() => setStatusFilters(checked ? statusFilters.filter(s => s !== f) : [...statusFilters, f])}
                    className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left transition-colors ${
                      checked ? 'bg-indigo-600/20 text-indigo-300' : 'text-gray-300 hover:bg-gray-800'
                    }`}
                  >
                    <span className={`w-3 h-3 rounded border flex items-center justify-center text-[8px] ${checked ? 'bg-indigo-500 border-indigo-500 text-white' : 'border-gray-600'}`}>
                      {checked && '✓'}
                    </span>
                    <span className={`w-2 h-2 rounded-full ${statusDotColors[f] || ''}`} />
                    {statusLabels[f]}
                  </button>
                );
              })}

              <div className="border-t border-gray-700 my-1" />

              {/* Toggle filters */}
              <div className="px-3 py-1 text-[10px] text-gray-500 uppercase tracking-wider">Filters</div>
              <button
                onClick={() => setStarredFilter(!starredFilter)}
                className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left transition-colors ${
                  starredFilter ? 'bg-yellow-600/20 text-yellow-300' : 'text-gray-300 hover:bg-gray-800'
                }`}
              >
                <span className={`w-3 h-3 rounded border flex items-center justify-center text-[8px] ${starredFilter ? 'bg-yellow-500 border-yellow-500 text-white' : 'border-gray-600'}`}>
                  {starredFilter && '✓'}
                </span>
                ★ Starred
              </button>
              <button
                onClick={() => setUnreadFilter(!unreadFilter)}
                className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left transition-colors ${
                  unreadFilter ? 'bg-indigo-600/20 text-indigo-300' : 'text-gray-300 hover:bg-gray-800'
                }`}
              >
                <span className={`w-3 h-3 rounded border flex items-center justify-center text-[8px] ${unreadFilter ? 'bg-indigo-500 border-indigo-500 text-white' : 'border-gray-600'}`}>
                  {unreadFilter && '✓'}
                </span>
                Unread
              </button>
              <button
                onClick={() => setShowArchived(!showArchived)}
                className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left transition-colors ${
                  showArchived ? 'bg-amber-600/20 text-amber-300' : 'text-gray-300 hover:bg-gray-800'
                }`}
              >
                <span className={`w-3 h-3 rounded border flex items-center justify-center text-[8px] ${showArchived ? 'bg-amber-500 border-amber-500 text-white' : 'border-gray-600'}`}>
                  {showArchived && '✓'}
                </span>
                Archived
              </button>

              {/* Tags section */}
              {allProjectTags.length > 0 && (
                <>
                  <div className="border-t border-gray-700 my-1" />
                  <div className="px-3 py-1 text-[10px] text-gray-500 uppercase tracking-wider">Tags</div>
                  {allProjectTags.map((tag) => {
                    const c = resolveTagColor(tag, tagColorMap[tag]);
                    const active = tagFilters.includes(tag);
                    return (
                      <button
                        key={tag}
                        onClick={() => {
                          const next = active ? tagFilters.filter((t) => t !== tag) : [...tagFilters, tag];
                          setTagFilters(next);
                          if (next.length > 0 && projectFilter !== undefined) {
                            const filtered = visibleProjects.filter((p) => next.some((t) => p.tags.includes(t)));
                            if (!filtered.some((p) => p.id === projectFilter)) {
                              setProjectFilter(undefined);
                            }
                          }
                        }}
                        className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left transition-colors ${
                          active ? `${c.bg} ${c.text}` : 'text-gray-300 hover:bg-gray-800'
                        }`}
                      >
                        <span className={`w-3 h-3 rounded border flex items-center justify-center text-[8px] ${active ? `${c.dot.replace('bg-', 'bg-')} border-current text-white` : 'border-gray-600'}`}>
                          {active && '✓'}
                        </span>
                        <span className={`w-2 h-2 rounded-full ${c.dot} ${active ? '' : 'opacity-60'}`} />
                        {tag}
                      </button>
                    );
                  })}
                </>
              )}

              {/* Clear all */}
              {activeFilterCount > 0 && (
                <>
                  <div className="border-t border-gray-700 my-1" />
                  <button
                    onClick={() => { setStatusFilters([]); setStarredFilter(false); setUnreadFilter(false); setShowArchived(false); setTagFilters([]); }}
                    className="w-full px-3 py-1.5 text-xs text-red-400 hover:bg-gray-800 text-left"
                  >
                    Clear all filters
                  </button>
                </>
              )}
            </div>
          )}
        </div>

        <ProjectSelect
          projects={tagFilteredProjects}
          value={projectFilter}
          onChange={(v) => setProjectFilter(v ? Number(v) : undefined)}
          placeholder="Projects"
          tagColorMap={tagColorMap}
        />

        <button
          onClick={() => {
            setShowSearch((s) => {
              if (s) setSearchQuery('');
              return !s;
            });
            setTimeout(() => searchInputRef.current?.focus(), 0);
          }}
          className={`flex items-center gap-1 text-xs px-2 py-1.5 rounded border transition-colors ${
            showSearch
              ? 'bg-indigo-600/30 text-indigo-300 border-indigo-500/50 hover:bg-indigo-600/40'
              : 'bg-gray-700 text-gray-400 border-gray-600 hover:bg-gray-600 hover:text-gray-300'
          }`}
          title="Search task titles (regex)"
        >
          <Search size={13} />
        </button>
        {showSearch && (
          <div className="relative flex-1 max-w-xs">
            <input
              ref={searchInputRef}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') { setSearchQuery(''); setShowSearch(false); } }}
              placeholder="Regex search titles..."
              className="w-full px-3 py-2 pr-8 rounded-lg bg-gray-900 border border-gray-700 text-sm text-gray-200 placeholder-gray-600 focus:border-indigo-500 focus:outline-none"
            />
            {searchQuery && (
              <button
                onClick={() => setSearchQuery('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
              >
                <X size={14} />
              </button>
            )}
          </div>
        )}
        {searchResults !== null && (
          <span className="text-xs text-gray-500 whitespace-nowrap">{searchResults.length} match{searchResults.length === 1 ? '' : 'es'}</span>
        )}
      </div>
  );

  const taskListContent = (
    <>
      <TaskForm onCreated={refresh} />

      <PlanPanel tasks={allTasks} onRefresh={refresh} />

      {filterControls}

      <TaskList
        tasks={searchResults ?? filteredTasks}
        projects={projects}
        onRefresh={refresh}
        onOpenChat={handleOpenChat}
        activeTaskId={chatTask?.id ?? null}
        autoSortOnAccess={autoSortOnAccess}
      />

      {totalPages > 1 && searchResults === null && (
        <div className="flex items-center justify-center gap-3 py-2">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="p-1.5 rounded text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronLeft size={18} />
          </button>
          <span className="text-xs text-gray-400">
            {page} / {totalPages}
            <span className="ml-2 text-gray-600">({totalCount} tasks)</span>
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="p-1.5 rounded text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronRight size={18} />
          </button>
        </div>
      )}
    </>
  );

  const chatPanel = chatTask && (
    chatTask.mode === 'loop'
      ? <LoopChatView key={chatTask.id} task={chatTask} onBack={() => setChatTaskWrapped(null)} inline={isWide} />
      : <ChatView key={chatTask.id} task={chatTask} projects={projects} onBack={() => setChatTaskWrapped(null)} onTaskUpdated={refresh} inline={isWide} />
  );

  const [sidebarOpen, setSidebarOpen] = useState(true);

  if (splitMode) {
    const sidebarStatusColors: Record<string, string> = {
      pending: 'bg-yellow-500',
      in_progress: 'bg-blue-500',
      executing: 'bg-blue-400 animate-pulse',
      plan_review: 'bg-purple-500',
      completed: 'bg-green-500',
      failed: 'bg-red-500',
      cancelled: 'bg-gray-500',
    };
    return (
      <div className="flex h-[calc(100vh-64px)] -mt-4 -mx-4">
        {sidebarOpen && (
          <div className="w-[260px] shrink-0 flex flex-col border-r border-gray-800 bg-gray-900/50">
            <div className="px-3 py-2 border-b border-gray-800 flex items-center justify-between shrink-0">
              <span className="text-xs font-medium text-gray-400">Tasks</span>
              <button
                onClick={() => setSidebarOpen(false)}
                className="p-1 text-gray-500 hover:text-gray-300 transition-colors"
                title="Collapse sidebar"
              >
                <PanelLeftClose size={14} />
              </button>
            </div>
            <div className="px-2 py-1.5 border-b border-gray-800 shrink-0">
              {filterControls}
            </div>
            <div className="flex-1 overflow-y-auto min-h-0">
              {sidebarTasks
                .map((t, idx) => {
                const proj = t.project_id ? projects.find((p) => p.id === t.project_id) : undefined;
                const colorDef = proj ? TAG_COLOR_OPTIONS.find((c) => c.key === proj.badge_color) : undefined;
                return (
                <div
                  key={t.id}
                  {...sidebarReorder.itemProps(t, idx)}
                  onClick={() => handleOpenChat(t)}
                  className={`w-full text-left px-3 py-2.5 transition-colors border-b border-gray-800/50 cursor-pointer ${
                    sidebarReorder.draggingId === t.id ? 'opacity-40' : ''
                  } ${sidebarReorder.overIndex === idx && sidebarReorder.draggingId !== null && sidebarReorder.draggingId !== t.id ? 'ring-2 ring-inset ring-indigo-400' : ''} ${
                    chatTask?.id === t.id
                      ? 'bg-indigo-900/40 border-l-2 border-l-indigo-400'
                      : 'hover:bg-gray-800/50 border-l-2 border-l-transparent'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    {/* 状态只靠圆点颜色表达（绿=完成 红=失败 蓝=运行 黄=等待） */}
                    <span className={`w-2 h-2 rounded-full shrink-0 ${sidebarStatusColors[t.status] || 'bg-gray-500'}`} title={t.status} />
                    <span className={`text-xs truncate flex-1 ${chatTask?.id === t.id ? 'text-foreground font-medium' : 'text-gray-300'}`}>
                      {t.title || t.description?.slice(0, 50) || `Task #${t.id}`}
                    </span>
                    {t.has_unread && <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 shrink-0" />}
                  </div>
                  <div className="flex items-center gap-1.5 mt-1 ml-4 flex-wrap">
                    <span className="text-[10px] text-gray-500">#{t.id}</span>
                    {proj && (
                      <span className={`text-[10px] px-1 rounded font-medium whitespace-nowrap ${colorDef ? `${colorDef.bg} ${colorDef.text}` : 'bg-emerald-600/30 text-emerald-300'}`}>
                        {proj.name}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 mt-1 ml-4" onClick={(e) => e.stopPropagation()}>
                    <PluginsBadge task={t} onRefresh={refresh} />
                    <SubAgentsBadge task={t} />
                    <span className="flex-1" />
                    <button
                      onClick={async () => { await api.starTask(t.id); refresh(); }}
                      className={`p-1 transition-colors ${t.starred ? 'text-yellow-400 hover:text-yellow-300' : 'text-gray-600 hover:text-yellow-400'}`}
                      title={t.starred ? 'Unstar' : 'Star'}
                    >
                      <Star size={13} fill={t.starred ? 'currentColor' : 'none'} />
                    </button>
                    <button
                      onClick={() => window.dispatchEvent(new CustomEvent('ccm-share-task', { detail: { task: t } }))}
                      className="p-1 text-gray-600 hover:text-blue-400 transition-colors"
                      title="Share"
                    >
                      <Share2 size={13} />
                    </button>
                    <button
                      onClick={async () => { await api.archiveTask(t.id); refresh(); }}
                      className="p-1 text-gray-600 hover:text-amber-400 transition-colors"
                      title={t.archived ? 'Unarchive' : 'Archive'}
                    >
                      {t.archived ? <ArchiveRestore size={13} /> : <Archive size={13} />}
                    </button>
                  </div>
                </div>
              );})}
            </div>
            {totalPages > 1 && (
              <div className="flex items-center justify-center gap-2 py-1.5 border-t border-gray-800 shrink-0">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="p-1 rounded text-gray-400 hover:text-white disabled:opacity-30"
                >
                  <ChevronLeft size={14} />
                </button>
                <span className="text-[10px] text-gray-500">{page}/{totalPages}</span>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="p-1 rounded text-gray-400 hover:text-white disabled:opacity-30"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            )}
          </div>
        )}
        {!sidebarOpen && (
          <div className="shrink-0 border-r border-gray-800 bg-gray-900/50 flex flex-col items-center pt-2">
            <button
              onClick={() => setSidebarOpen(true)}
              className="p-1.5 text-gray-500 hover:text-gray-300 transition-colors"
              title="Expand sidebar"
            >
              <PanelLeftOpen size={16} />
            </button>
          </div>
        )}
        <div className="flex-1 min-w-0">
          {chatPanel}
        </div>
        {sharingTask && (
          <ShareModal
            type="task"
            itemId={sharingTask.id}
            itemTitle={sharingTask.title || `Task #${sharingTask.id}`}
            onClose={() => setSharingTask(null)}
          />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {taskListContent}
      {chatPanel}
      {sharingTask && (
        <ShareModal
          type="task"
          itemId={sharingTask.id}
          itemTitle={sharingTask.title || `Task #${sharingTask.id}`}
          onClose={() => setSharingTask(null)}
        />
      )}
    </div>
  );
}
