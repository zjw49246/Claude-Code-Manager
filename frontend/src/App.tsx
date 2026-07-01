import { useState, useEffect, Component } from 'react';
import type { ReactNode, ErrorInfo } from 'react';
import { Header } from './components/Layout/Header';
import { Dashboard } from './pages/Dashboard';
import { TasksPage } from './pages/TasksPage';
import { LoginPage } from './pages/LoginPage';
import { ServerConfigPage } from './pages/ServerConfigPage';
import { ProjectsPage } from './pages/ProjectsPage';
import { SecretsPage } from './pages/SecretsPage';
import { FilesPage } from './pages/FilesPage';
import { DiscussionsPage } from './pages/DiscussionsPage';
import { PRMonitorPage } from './pages/PRMonitorPage';
import WorkersPage from './pages/WorkersPage';
import TeamPage from './pages/TeamPage';
import { SkillsPage } from './pages/SkillsPage';

import { getToken } from './api/client';
import { isCapacitor, getServerUrl, getApiBase } from './config/server';

class ErrorBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(error: Error) {
    return { error: error.message };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('React error:', error, info);
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 40, color: '#f87171', background: '#1a1a2e', minHeight: '100vh' }}>
          <h1 style={{ fontSize: 24, marginBottom: 16 }}>Something went wrong</h1>
          <pre style={{ whiteSpace: 'pre-wrap' }}>{this.state.error}</pre>
          <button
            onClick={() => { this.setState({ error: null }); window.location.reload(); }}
            style={{ marginTop: 16, padding: '8px 16px', background: '#4f46e5', color: 'white', border: 'none', borderRadius: 4, cursor: 'pointer' }}
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const VALID_PAGES = new Set(['tasks', 'dashboard', 'projects', 'secrets', 'files', 'discussions', 'pr-monitor', 'workers', 'skills', 'team', 'server']);

function parseHash(): { page: string; chatTaskId: number | null } {
  const hash = window.location.hash.replace(/^#\/?/, '');
  const parts = hash.split('/');
  const page = VALID_PAGES.has(parts[0]) ? parts[0] : 'tasks';
  let chatTaskId: number | null = null;
  if (page === 'tasks' && parts[1] === 'chat' && parts[2]) {
    const id = parseInt(parts[2], 10);
    if (id > 0) chatTaskId = id;
  }
  return { page, chatTaskId };
}

function updateHash(page: string, chatTaskId: number | null) {
  let hash = `#/${page}`;
  if (page === 'tasks' && chatTaskId) hash += `/chat/${chatTaskId}`;
  if (window.location.hash !== hash) {
    window.history.replaceState(null, '', hash);
  }
}

function App() {
  const initial = parseHash();
  const [page, setPage] = useState(initial.page);
  const [chatTaskId, setChatTaskId] = useState<number | null>(initial.chatTaskId);
  const [authenticated, setAuthenticated] = useState(false);
  const [checking, setChecking] = useState(true);
  const [needsServerConfig, setNeedsServerConfig] = useState(false);

  useEffect(() => {
    updateHash(page, chatTaskId);
  }, [page, chatTaskId]);

  useEffect(() => {
    const onHashChange = () => {
      const parsed = parseHash();
      setPage(parsed.page);
      setChatTaskId(parsed.chatTaskId);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const handleNavigate = (p: string) => {
    setPage(p);
    if (p !== 'tasks') setChatTaskId(null);
  };

  useEffect(() => {
    // In Capacitor, require server URL to be configured first
    if (isCapacitor() && !getServerUrl()) {
      setNeedsServerConfig(true);
      setChecking(false);
      return;
    }

    const base = getApiBase();
    // Check if auth is required
    fetch(`${base}/api/system/health`)
      .then((res) => {
        if (res.ok) {
          // Health is public, now check if auth is needed by trying a protected endpoint
          const token = getToken();
          return fetch(`${base}/api/instances`, {
            headers: token ? { Authorization: `Bearer ${token}` } : {},
          });
        }
        throw new Error('Server unreachable');
      })
      .then((res) => {
        if (res.ok) {
          setAuthenticated(true);
          // Ensure cc_user is populated (token login may not have set it)
          const ccUser = JSON.parse(localStorage.getItem('cc_user') || '{}');
          if (!ccUser.name) {
            const token = getToken();
            fetch(`${base}/api/auth/me`, {
              headers: token ? { Authorization: `Bearer ${token}` } : {},
            }).then(r => r.ok ? r.json() : null).then(d => {
              if (d?.user) localStorage.setItem('cc_user', JSON.stringify(d.user));
            }).catch(() => {});
          }
        }
      })
      .catch(() => {
        // Server down, show login anyway
      })
      .finally(() => setChecking(false));
  }, []);

  if (needsServerConfig) {
    return (
      <ServerConfigPage onConfigured={() => {
        setNeedsServerConfig(false);
        window.location.reload();
      }} />
    );
  }

  if (checking) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <p className="text-gray-400">Connecting...</p>
      </div>
    );
  }

  if (!authenticated) {
    return <LoginPage onLogin={() => setAuthenticated(true)} />;
  }

  return (
    <ErrorBoundary>
      <div className="min-h-screen bg-gray-900 text-foreground flex flex-col overflow-x-clip">
        <Header currentPage={page} onNavigate={handleNavigate} />
        <main className={`flex-1 mx-auto w-full p-4 ${page === 'tasks' && chatTaskId ? 'max-w-[1400px]' : 'max-w-6xl'}`}>
          {page === 'dashboard' && <Dashboard />}
          {page === 'tasks' && <TasksPage chatTaskId={chatTaskId} onChatTaskChange={setChatTaskId} />}
          {page === 'projects' && <ProjectsPage />}
          {page === 'secrets' && <SecretsPage />}
          {page === 'files' && <FilesPage />}
          {page === 'discussions' && <DiscussionsPage />}
          {page === 'pr-monitor' && <PRMonitorPage />}
          {page === 'workers' && <WorkersPage />}
          {page === 'skills' && <SkillsPage />}
          {page === 'team' && <TeamPage />}
          {page === 'server' && (
            <ServerConfigPage onConfigured={() => window.location.reload()} />
          )}
        </main>
      </div>
    </ErrorBoundary>
  );
}

export default App;
