import { useState } from 'react';
import { Sun, Moon, Globe, Menu, X } from 'lucide-react';
import { isCapacitor } from '../../config/server';
import { getTheme, toggleTheme } from '../../config/theme';
import { getTimezone, setTimezone, TIMEZONE_OPTIONS } from '../../config/timezone';

interface HeaderProps {
  currentPage: string;
  onNavigate: (page: string) => void;
}

export function Header({ currentPage, onNavigate }: HeaderProps) {
  const [theme, setTheme] = useState(getTheme());
  const [tz, setTz] = useState(getTimezone());
  const [menuOpen, setMenuOpen] = useState(false);

  const pages = [
    { key: 'dashboard', label: 'Dashboard' },
    { key: 'tasks', label: 'Tasks' },
    { key: 'projects', label: 'Projects' },
    { key: 'secrets', label: 'Secrets' },
    { key: 'files', label: 'Files' },
    { key: 'discussions', label: 'Discussions' },
    ...(isCapacitor() ? [{ key: 'server', label: 'Server' }] : []),
  ];

  const handleToggleTheme = () => {
    const next = toggleTheme();
    setTheme(next);
  };

  return (
    <header className="bg-gray-900 border-b border-gray-700 px-4 py-2 pt-[max(0.5rem,env(safe-area-inset-top))]">
      <div className="flex items-center gap-3">
        <h1 className="text-base font-bold text-foreground">Claude Manager</h1>
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
          <div className="relative flex items-center">
            <Globe size={16} className="absolute left-2 text-gray-500 pointer-events-none" />
            <select
              value={tz}
              onChange={(e) => { setTimezone(e.target.value); setTz(e.target.value); }}
              className="appearance-none bg-gray-800 text-gray-300 text-xs rounded pl-7 pr-6 py-1.5 border border-gray-700 hover:border-gray-600 focus:outline-none focus:ring-1 focus:ring-indigo-500 cursor-pointer"
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
            className="sm:hidden p-2 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 transition-colors"
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
