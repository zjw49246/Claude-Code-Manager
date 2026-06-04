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

function App() {
  const [page, setPage] = useState('tasks');
  const [authenticated, setAuthenticated] = useState(false);
  const [checking, setChecking] = useState(true);
  const [needsServerConfig, setNeedsServerConfig] = useState(false);

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
        }
        // 401 means auth is required and token is invalid/missing
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
      <div className="min-h-screen bg-gray-900 text-foreground flex flex-col">
        <Header currentPage={page} onNavigate={setPage} />
        <main className="flex-1 max-w-6xl mx-auto w-full p-4">
          {page === 'dashboard' && <Dashboard />}
          {page === 'tasks' && <TasksPage />}
          {page === 'projects' && <ProjectsPage />}
          {page === 'secrets' && <SecretsPage />}
          {page === 'files' && <FilesPage />}
          {page === 'discussions' && <DiscussionsPage />}
          {page === 'server' && (
            <ServerConfigPage onConfigured={() => window.location.reload()} />
          )}
        </main>
      </div>
    </ErrorBoundary>
  );
}

export default App;
