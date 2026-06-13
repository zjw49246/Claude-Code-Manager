import { useCallback, useEffect, useState } from 'react';
import { Sun, Moon, Globe, Menu, X } from 'lucide-react';
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

  return (
    <header className="bg-gray-900 border-b border-gray-700 px-4 py-2 pt-[max(0.5rem,env(safe-area-inset-top))]">
      <div className="flex items-center gap-3">
        <h1 className="text-base font-bold text-foreground truncate min-w-0">Claude Manager</h1>
        {/* Desktop nav */}
        <nav className="hidden sm:flex gap-1.5 flex-wrap">
          {pages.map((p) => (
            <button
              key={p.key}
              onClick={() => onNavigate(p.key)}
              className={`px-3 py-1.5 min-h-[36px] rounded text-xs sm:text-sm font-medium transition-colors ${
                currentPage === p.key
                  ? 'bg-indigo-600 text-white'
                  : 'text-gray-300 hover:bg-gray-800'
              }`}
            >
              {p.label}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-1">
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
          <div className="relative flex items-center shrink-0">
            <Globe size={16} className="absolute left-2 text-gray-500 pointer-events-none" />
            {/* 手机端只留图标宽度（文字透明），避免 "Auto" 把汉堡按钮挤出屏幕 */}
            <select
              value={tz}
              onChange={(e) => { setTimezone(e.target.value); setTz(e.target.value); }}
              className="appearance-none bg-gray-800 text-transparent sm:text-gray-300 text-xs rounded pl-7 pr-0 sm:pr-6 py-1.5 w-8 sm:w-auto border border-gray-700 hover:border-gray-600 focus:outline-none focus:ring-1 focus:ring-indigo-500 cursor-pointer"
              title="Timezone"
            >
              {TIMEZONE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
          <button
            onClick={handleToggleTheme}
            className="p-2 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 transition-colors"
            title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
          </button>
          {/* Mobile menu button */}
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="sm:hidden shrink-0 p-2 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 transition-colors"
          >
            {menuOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
        </div>
      </div>
      {/* Mobile nav dropdown */}
      {menuOpen && (
        <nav className="sm:hidden flex flex-col gap-1 mt-2 pb-1">
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
