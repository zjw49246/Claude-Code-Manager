import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Sun, Moon, Globe, Menu, X, Settings } from 'lucide-react';
import { api } from '../../api/client';
import type { RuntimeSettings } from '../../api/client';
import { isCapacitor } from '../../config/server';
import { getTheme, toggleTheme } from '../../config/theme';
import { getTimezone, setTimezone, TIMEZONE_OPTIONS } from '../../config/timezone';
import { PoolDrawer } from './PoolDrawer';

interface HeaderProps {
  currentPage: string;
  onNavigate: (page: string) => void;
}

export function Header({ currentPage, onNavigate }: HeaderProps) {
  const [theme, setTheme] = useState(getTheme());
  const [tz, setTz] = useState(getTimezone());
  const [menuOpen, setMenuOpen] = useState(false);
  const [prefsOpen, setPrefsOpen] = useState(false);
  const prefsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!prefsOpen) return;
    const handle = (e: MouseEvent) => {
      if (prefsRef.current && !prefsRef.current.contains(e.target as Node)) setPrefsOpen(false);
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [prefsOpen]);
  const [runtime, setRuntime] = useState<RuntimeSettings | null>(null);
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    api.getRuntimeSettings().then(setRuntime).catch(() => setRuntime(null));
  }, []);

  const togglePtyMode = useCallback(async () => {
    if (!runtime || switching || !runtime.pty_available) return;
    setSwitching(true);
    try {
      const updated = await api.updateRuntimeSettings({ use_pty_mode: !runtime.use_pty_mode });
      setRuntime(updated);
    } catch {
      // keep previous state on failure
    } finally {
      setSwitching(false);
    }
  }, [runtime, switching]);

  const toggleAutoSort = useCallback(async () => {
    if (!runtime || switching) return;
    setSwitching(true);
    try {
      const updated = await api.updateRuntimeSettings({ auto_sort_on_access: !runtime.auto_sort_on_access });
      setRuntime(updated);
    } catch { /* keep previous state */ } finally {
      setSwitching(false);
    }
  }, [runtime, switching]);

  const pages = [
    { key: 'dashboard', label: 'Dashboard' },
    { key: 'tasks', label: 'Tasks' },
    { key: 'projects', label: 'Projects' },
    { key: 'secrets', label: 'Secrets' },
    { key: 'files', label: 'Files' },
    { key: 'discussions', label: 'Discussions' },
    { key: 'pr-monitor', label: 'PR Monitor' },
    { key: 'workers', label: 'Workers' },
    ...(isCapacitor() ? [{ key: 'server', label: 'Server' }] : []),
  ];

  const handleToggleTheme = () => {
    const next = toggleTheme();
    setTheme(next);
  };

  // 导航收纳规则：不按固定断点，而是按实际宽度——完整导航一行放不下
  // （会换行/溢出）就收进汉堡。用一条隐藏的测量 nav 算出所需宽度，
  // 与「行宽 - 标题 - 右侧控件」比较，窗口尺寸变化时实时重算。
  const rowRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const rightRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLElement>(null);
  const [collapsed, setCollapsed] = useState(false);

  useLayoutEffect(() => {
    const update = () => {
      const row = rowRef.current, t = titleRef.current,
        r = rightRef.current, m = measureRef.current;
      if (!row || !t || !r || !m) return;
      const gaps = 12 * 3; // 行内 gap-3 三处间隙
      const available = row.clientWidth - t.offsetWidth - r.offsetWidth - gaps;
      setCollapsed(m.scrollWidth > available);
    };
    update();
    const ro = new ResizeObserver(update);
    if (rowRef.current) ro.observe(rowRef.current);
    if (rightRef.current) ro.observe(rightRef.current);
    return () => ro.disconnect();
  }, []);

  return (
    <header className="bg-gray-900 border-b border-gray-700 px-4 py-2 pt-[max(0.5rem,env(safe-area-inset-top))]">
      <div ref={rowRef} className="relative flex items-center gap-3">
        <h1 ref={titleRef} className="text-base font-bold text-foreground whitespace-nowrap shrink-0">Claude Manager</h1>
        {/* 隐藏测量 nav：始终渲染完整按钮以计算所需宽度 */}
        <nav ref={measureRef} aria-hidden className="absolute invisible pointer-events-none flex gap-1.5 whitespace-nowrap">
          {pages.map((p) => (
            <button key={p.key} tabIndex={-1} className="px-3 py-1.5 min-h-[36px] rounded text-sm font-medium">
              {p.label}
            </button>
          ))}
        </nav>
        {/* 实际导航：放得下才显示，放不下收进汉堡 */}
        {!collapsed && (
          <nav className="flex gap-1.5 flex-nowrap overflow-hidden">
            {pages.map((p) => (
              <button
                key={p.key}
                onClick={() => onNavigate(p.key)}
                className={`px-3 py-1.5 min-h-[36px] rounded text-sm font-medium whitespace-nowrap transition-colors ${
                  currentPage === p.key
                    ? 'bg-indigo-600 text-white'
                    : 'text-gray-300 hover:bg-gray-800'
                }`}
              >
                {p.label}
              </button>
            ))}
          </nav>
        )}
        <div ref={rightRef} className="ml-auto flex items-center gap-1">
          <PoolDrawer />
          {runtime && (
            <div
              className="flex items-center gap-1.5 mr-1 px-2 py-1 rounded bg-gray-800 border border-gray-700"
              title={
                !runtime.pty_available
                  ? 'claude_pty 未安装，PTY 模式不可用'
                  : runtime.use_pty_mode
                    ? 'PTY 常驻会话模式：开（多轮免冷启动；切换仅影响新任务）'
                    : 'PTY 常驻会话模式：关（使用 claude -p 一次性进程）'
              }
            >
              <span className={`text-xs font-medium ${runtime.use_pty_mode ? 'text-green-400' : 'text-gray-400'}`}>
                PTY
              </span>
              <button
                onClick={togglePtyMode}
                disabled={!runtime.pty_available || switching}
                className={`relative inline-flex h-4 w-8 items-center rounded-full transition-colors disabled:opacity-50 ${
                  runtime.use_pty_mode ? 'bg-green-500' : 'bg-gray-600'
                }`}
              >
                <span
                  className={`inline-block h-3 w-3 transform rounded-full bg-white transition-transform ${
                    runtime.use_pty_mode ? 'translate-x-4' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>
          )}
          {/* 偏好下拉：时区 + 主题（低频设置收进齿轮，顶栏保持精简） */}
          <div className="relative shrink-0" ref={prefsRef}>
            <button
              onClick={() => setPrefsOpen(!prefsOpen)}
              className={`p-2 rounded transition-colors ${prefsOpen ? 'text-foreground bg-gray-800' : 'text-gray-400 hover:text-foreground hover:bg-gray-800'}`}
              title="偏好设置（时区 / 主题）"
            >
              <Settings size={18} />
            </button>
            {prefsOpen && (
              <div className="absolute top-full right-0 mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-lg z-30 p-3 min-w-[210px] space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-xs text-gray-400 flex items-center gap-1.5"><Globe size={13} /> 时区</span>
                  <select
                    value={tz}
                    onChange={(e) => { setTimezone(e.target.value); setTz(e.target.value); }}
                    className="bg-gray-700 text-gray-200 text-xs rounded px-2 py-1 border border-gray-600 focus:outline-none focus:ring-1 focus:ring-indigo-500 cursor-pointer"
                  >
                    {TIMEZONE_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-xs text-gray-400 flex items-center gap-1.5">
                    {theme === 'dark' ? <Moon size={13} /> : <Sun size={13} />} 主题
                  </span>
                  <button
                    onClick={handleToggleTheme}
                    className="text-xs px-2 py-1 rounded bg-gray-700 text-gray-200 border border-gray-600 hover:bg-gray-600"
                  >
                    {theme === 'dark' ? '切换到浅色' : '切换到深色'}
                  </button>
                </div>
                {runtime && (
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-xs text-gray-400">访问置顶</span>
                    <button
                      onClick={toggleAutoSort}
                      disabled={switching}
                      className={`relative inline-flex h-4 w-8 items-center rounded-full transition-colors disabled:opacity-50 ${
                        runtime.auto_sort_on_access ? 'bg-green-500' : 'bg-gray-600'
                      }`}
                      title={runtime.auto_sort_on_access ? '开启：打开聊天自动置顶任务' : '关闭：打开聊天不改变排序'}
                    >
                      <span
                        className={`inline-block h-3 w-3 transform rounded-full bg-white transition-transform ${
                          runtime.auto_sort_on_access ? 'translate-x-4' : 'translate-x-1'
                        }`}
                      />
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
          {/* 汉堡按钮：导航被收纳时出现 */}
          {collapsed && (
            <button
              onClick={() => setMenuOpen(!menuOpen)}
              className="shrink-0 p-2 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 transition-colors"
            >
              {menuOpen ? <X size={18} /> : <Menu size={18} />}
            </button>
          )}
        </div>
      </div>
      {/* 收纳后的导航下拉 */}
      {collapsed && menuOpen && (
        <nav className="flex flex-col gap-1 mt-2 pb-1">
          {pages.map((p) => (
            <button
              key={p.key}
              onClick={() => { onNavigate(p.key); setMenuOpen(false); }}
              className={`px-3 py-2 rounded text-sm font-medium text-left transition-colors ${
                currentPage === p.key
                  ? 'bg-indigo-600 text-white'
                  : 'text-gray-300 hover:bg-gray-800'
              }`}
            >
              {p.label}
            </button>
          ))}
        </nav>
      )}
    </header>
  );
}
