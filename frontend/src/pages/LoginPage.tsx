import { useState } from 'react';
import { setToken } from '../api/client';
import { getApiBase, getServerUrl, setServerUrl } from '../config/server';
import { ChevronDown, ChevronRight } from 'lucide-react';

interface LoginPageProps {
  onLogin: () => void;
}

export function LoginPage({ onLogin }: LoginPageProps) {
  const [mode, setMode] = useState<'token' | 'email' | 'register'>('email');
  const [token, setTokenValue] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  const [serverUrl, setServerUrlValue] = useState(getServerUrl());
  const [showServer, setShowServer] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const base = () => serverUrl.replace(/\/+$/, '') || getApiBase();

  const handleTokenLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    if (serverUrl !== getServerUrl()) setServerUrl(serverUrl);
    try {
      const res = await fetch(`${base()}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });
      if (res.ok) {
        setToken(token);
        onLogin();
      } else {
        setError('Invalid token');
      }
    } catch {
      setError('Connection failed');
    } finally {
      setLoading(false);
    }
  };

  const handleEmailLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    if (serverUrl !== getServerUrl()) setServerUrl(serverUrl);
    try {
      const res = await fetch(`${base()}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      const data = await res.json();
      if (res.ok && data.token) {
        setToken(data.token);
        localStorage.setItem('cc_user', JSON.stringify(data.user));
        onLogin();
      } else {
        setError(data.detail || 'Login failed');
      }
    } catch {
      setError('Connection failed');
    } finally {
      setLoading(false);
    }
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    if (serverUrl !== getServerUrl()) setServerUrl(serverUrl);
    try {
      const res = await fetch(`${base()}/api/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, name, password, invite_code: inviteCode }),
      });
      const data = await res.json();
      if (res.ok && data.token) {
        setToken(data.token);
        localStorage.setItem('cc_user', JSON.stringify(data.user));
        onLogin();
      } else {
        setError(data.detail || 'Registration failed');
      }
    } catch {
      setError('Connection failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center p-4">
      <div className="bg-gray-800 rounded-lg p-6 w-full max-w-sm space-y-4">
        <h1 className="text-foreground text-lg font-bold text-center">Claude Code Manager</h1>

        {/* Mode tabs */}
        <div className="flex gap-1 bg-gray-700 rounded p-0.5">
          {([['email', 'Email'], ['token', 'Token'], ['register', 'Register']] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => { setMode(key); setError(''); }}
              className={`flex-1 py-1.5 text-xs font-medium rounded transition-colors ${
                mode === key ? 'bg-gray-600 text-foreground' : 'text-gray-400 hover:text-gray-300'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Token login */}
        {mode === 'token' && (
          <form onSubmit={handleTokenLogin} className="space-y-3">
            <input
              type="password"
              className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Access Token"
              value={token}
              onChange={(e) => setTokenValue(e.target.value)}
              autoFocus
              required
            />
            <button type="submit" disabled={loading}
              className="w-full bg-indigo-600 hover:bg-indigo-700 text-white py-2 rounded text-sm font-medium disabled:opacity-50">
              {loading ? 'Verifying...' : 'Login'}
            </button>
          </form>
        )}

        {/* Email login */}
        {mode === 'email' && (
          <form onSubmit={handleEmailLogin} className="space-y-3">
            <input
              type="email"
              className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus
              required
            />
            <input
              type="password"
              className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <button type="submit" disabled={loading}
              className="w-full bg-indigo-600 hover:bg-indigo-700 text-white py-2 rounded text-sm font-medium disabled:opacity-50">
              {loading ? 'Logging in...' : 'Login'}
            </button>
          </form>
        )}

        {/* Register */}
        {mode === 'register' && (
          <form onSubmit={handleRegister} className="space-y-3">
            <input
              type="text"
              className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              required
            />
            <input
              type="email"
              className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
            <input
              type="password"
              className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <input
              type="text"
              className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              placeholder="Invite Code"
              value={inviteCode}
              onChange={(e) => setInviteCode(e.target.value)}
              required
            />
            <button type="submit" disabled={loading}
              className="w-full bg-indigo-600 hover:bg-indigo-700 text-white py-2 rounded text-sm font-medium disabled:opacity-50">
              {loading ? 'Registering...' : 'Register'}
            </button>
          </form>
        )}

        {/* Server URL toggle */}
        <div>
          <button
            type="button"
            onClick={() => setShowServer(!showServer)}
            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-300 transition-colors"
          >
            {showServer ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            Server URL
          </button>
          {showServer && (
            <input
              type="url"
              className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 mt-1"
              placeholder="https://your-server.com"
              value={serverUrl}
              onChange={(e) => setServerUrlValue(e.target.value)}
            />
          )}
        </div>

        {error && <p className="text-red-400 text-xs text-center">{error}</p>}
      </div>
    </div>
  );
}
