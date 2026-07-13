import { useState } from 'react';
import { getServerUrl, setServerUrl } from '../config/server';

interface ServerConfigPageProps {
  onConfigured: () => void;
}

export function ServerConfigPage({ onConfigured }: ServerConfigPageProps) {
  const [url, setUrl] = useState(getServerUrl());
  const [error, setError] = useState('');
  const [testing, setTesting] = useState(false);
  const [success, setSuccess] = useState(false);

  const handleTest = async () => {
    if (!url.trim()) {
      setError('Please enter a server URL');
      return;
    }
    setTesting(true);
    setError('');
    setSuccess(false);
    try {
      const normalized = url.replace(/\/+$/, '');
      const res = await fetch(`${normalized}/api/system/health`);
      if (res.ok) {
        setSuccess(true);
      } else {
        setError(`Server responded with status ${res.status}`);
      }
    } catch {
      setError('Cannot connect to server. Check the URL and try again.');
    } finally {
      setTesting(false);
    }
  };

  const handleSave = () => {
    if (!url.trim()) {
      setError('Please enter a server URL');
      return;
    }
    setServerUrl(url.trim());
    onConfigured();
  };

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center p-4">
      <div className="bg-gray-800 rounded-lg p-6 w-full max-w-sm space-y-4">
        <h1 className="text-foreground text-lg font-bold text-center">Claude Code Manager</h1>
        <p className="text-gray-400 text-sm text-center">Enter your server address</p>
        <input
          type="url"
          className="w-full bg-gray-700 text-foreground rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          placeholder="https://ccm.example.com"
          value={url}
          onChange={(e) => { setUrl(e.target.value); setSuccess(false); setError(''); }}
          autoFocus
        />
        {error && <p className="text-red-400 text-xs">{error}</p>}
        {success && <p className="text-green-400 text-xs">Connection successful!</p>}
        <div className="flex gap-2">
          <button
            type="button"
            onClick={handleTest}
            disabled={testing}
            className="flex-1 bg-gray-600 hover:bg-gray-500 text-foreground py-2 rounded text-sm font-medium disabled:opacity-50"
          >
            {testing ? 'Testing...' : 'Test Connection'}
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!success}
            className="flex-1 bg-indigo-600 hover:bg-indigo-700 text-white py-2 rounded text-sm font-medium disabled:opacity-50"
          >
            Connect
          </button>
        </div>
      </div>
    </div>
  );
}
