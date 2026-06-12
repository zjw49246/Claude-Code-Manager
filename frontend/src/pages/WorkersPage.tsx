import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../api/client';
import type { Worker } from '../api/client';
import { Plus, X, RefreshCw, Trash2, Power, Play, Server, ScrollText } from 'lucide-react';

const STATUS_COLORS: Record<string, string> = {
  creating: 'bg-blue-500/20 text-blue-400',
  bootstrapping: 'bg-blue-500/20 text-blue-400',
  ready: 'bg-green-500/20 text-green-400',
  error: 'bg-red-500/20 text-red-400',
  stopping: 'bg-yellow-500/20 text-yellow-400',
  stopped: 'bg-gray-500/20 text-gray-400',
  starting: 'bg-blue-500/20 text-blue-400',
  destroying: 'bg-red-500/20 text-red-400',
  terminated: 'bg-gray-500/20 text-gray-500',
};

const ACCOUNT_COLORS: Record<string, string> = {
  logged_in: 'text-green-400',
  pending: 'text-yellow-400',
  failed: 'text-red-400',
};

const BUSY = new Set(['creating', 'bootstrapping', 'stopping', 'starting', 'destroying']);

function AddWorkerModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [accounts, setAccounts] = useState<{ email: string; password: string }[]>([{ email: '', password: '' }]);
  const [adoptId, setAdoptId] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.createWorker({
        accounts: accounts.filter((a) => a.email.trim()).map((a) => ({ email: a.email.trim(), password: a.password || undefined })),
        adopt_instance_id: adoptId.trim() || undefined,
      });
      onSaved();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-xl shadow-2xl w-full max-w-md">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
          <h3 className="text-foreground font-semibold">Add Worker</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={18} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <p className="text-xs text-gray-400">
            新 EC2 配置自动继承本机（机型/镜像/子网/密钥）。账号在 Worker 本机登录，Manager 不保存 Claude 凭证。
          </p>
          {accounts.map((acct, i) => (
            <div key={i} className="flex gap-2 items-start">
              <div className="flex-1 space-y-2">
                <input
                  className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
                  value={acct.email} placeholder={`账号 ${i + 1} Email`}
                  onChange={(e) => setAccounts(accounts.map((a, j) => (j === i ? { ...a, email: e.target.value } : a)))}
                />
                <input
                  type="password"
                  className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
                  value={acct.password} placeholder="密码（可选，登录方式按 auto_login）"
                  onChange={(e) => setAccounts(accounts.map((a, j) => (j === i ? { ...a, password: e.target.value } : a)))}
                />
              </div>
              {accounts.length > 1 && (
                <button type="button" className="text-gray-500 hover:text-red-400 mt-2"
                  onClick={() => setAccounts(accounts.filter((_, j) => j !== i))}><X size={16} /></button>
              )}
            </div>
          ))}
          <button type="button" onClick={() => setAccounts([...accounts, { email: '', password: '' }])}
            className="text-xs text-indigo-400 hover:text-indigo-300">+ 再加一个账号</button>
          <div>
            <label className="block text-xs text-gray-400 mb-1">收养已有实例 ID（可选，跳过开机直接 bootstrap）</label>
            <input
              className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={adoptId} onChange={(e) => setAdoptId(e.target.value)} placeholder="i-xxxxxxxxxxxx"
            />
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-gray-300 hover:text-white">Cancel</button>
            <button type="submit" disabled={submitting}
              className="px-4 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500 disabled:opacity-50">
              {submitting ? 'Creating...' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function LogsModal({ worker, onClose }: { worker: Worker; onClose: () => void }) {
  const [log, setLog] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    const load = () => api.getWorkerLogs(worker.id).then((r) => { if (alive) setLog(r.bootstrap_log || ''); }).catch(() => {});
    load();
    const t = setInterval(load, 3000);
    return () => { alive = false; clearInterval(t); };
  }, [worker.id]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [log]);

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-xl shadow-2xl w-full max-w-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
          <h3 className="text-foreground font-semibold">{worker.name} — Bootstrap Log</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={18} /></button>
        </div>
        <pre className="p-4 text-xs text-gray-300 font-mono overflow-auto max-h-[60vh] whitespace-pre-wrap">
          {log || '(暂无日志)'}
          <div ref={bottomRef} />
        </pre>
      </div>
    </div>
  );
}

function WorkerCard({ worker, onAction }: { worker: Worker; onAction: () => void }) {
  const [logsOpen, setLogsOpen] = useState(false);
  const busy = BUSY.has(worker.status);

  const act = async (fn: (id: number) => Promise<Worker>, confirmMsg?: string) => {
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    try {
      await fn(worker.id);
      onAction();
    } catch (e) {
      window.alert(String(e));
    }
  };

  return (
    <div className="bg-gray-800 rounded-lg p-4 space-y-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Server size={16} className="text-indigo-400 shrink-0" />
          <span className="text-foreground font-medium truncate">{worker.name}</span>
          <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${STATUS_COLORS[worker.status] || 'bg-gray-500/20 text-gray-400'}`}>
            {worker.status}{busy && worker.bootstrap_step ? `: ${worker.bootstrap_step}` : ''}
          </span>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button title="日志" onClick={() => setLogsOpen(true)}
            className="p-1.5 text-gray-400 hover:text-gray-200"><ScrollText size={15} /></button>
          {worker.status === 'error' && (
            <button title="重试 bootstrap" onClick={() => act(api.retryWorker)}
              className="p-1.5 text-gray-400 hover:text-blue-400"><RefreshCw size={15} /></button>
          )}
          {worker.status === 'ready' && (
            <button title="关机（EC2 stop，数据保留）" onClick={() => act(api.stopWorker, `关机 ${worker.name}？数据保留，停机期间不可派发任务。`)}
              className="p-1.5 text-gray-400 hover:text-yellow-400"><Power size={15} /></button>
          )}
          {worker.status === 'stopped' && (
            <button title="开机" onClick={() => act(api.startWorker)}
              className="p-1.5 text-gray-400 hover:text-green-400"><Play size={15} /></button>
          )}
          {!busy && (
            <button title={worker.adopted ? '移除（收养实例只关机不销毁）' : '销毁（terminate EC2）'}
              onClick={() => act(api.destroyWorker, worker.adopted
                ? `移除 ${worker.name}？收养的实例只会关机，不会销毁。`
                : `销毁 ${worker.name}？EC2 实例将被 terminate，不可恢复！`)}
              className="p-1.5 text-gray-400 hover:text-red-400"><Trash2 size={15} /></button>
          )}
        </div>
      </div>
      <div className="text-xs text-gray-400 flex flex-wrap gap-x-4 gap-y-1">
        {worker.private_ip && <span>内网 {worker.private_ip}</span>}
        {worker.cloud_instance_id && <span>{worker.cloud_instance_id}</span>}
        {worker.ccm_commit && <span title={worker.ccm_commit}>@{worker.ccm_commit.slice(0, 8)}</span>}
        {worker.last_heartbeat && <span>心跳 {new Date(worker.last_heartbeat + 'Z').toLocaleTimeString()}</span>}
        {worker.adopted && <span className="text-amber-400/80">收养</span>}
      </div>
      {(worker.accounts || []).length > 0 && (
        <div className="text-xs flex flex-wrap gap-2">
          {(worker.accounts || []).map((a, i) => (
            <span key={i} className={`${ACCOUNT_COLORS[a.status] || 'text-gray-400'}`}>{a.email} ({a.status})</span>
          ))}
        </div>
      )}
      {worker.bootstrap_error && (
        <p className="text-xs text-red-400 break-all">[{worker.bootstrap_step}] {worker.bootstrap_error}</p>
      )}
      {logsOpen && <LogsModal worker={worker} onClose={() => setLogsOpen(false)} />}
    </div>
  );
}

export default function WorkersPage() {
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.listWorkers()
      .then((w) => { setWorkers(w); setError(null); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-foreground">Workers</h2>
        <button onClick={() => setAdding(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500">
          <Plus size={15} /> Add Worker
        </button>
      </div>
      {error && <p className="text-red-400 text-sm">{error}</p>}
      {loading ? (
        <p className="text-gray-400 text-sm">Loading...</p>
      ) : workers.length === 0 ? (
        <div className="text-center py-16 text-gray-500">
          <Server size={32} className="mx-auto mb-3 opacity-50" />
          <p className="text-sm">还没有 Worker。点击 Add Worker 开一台和本机同配置的 EC2。</p>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {workers.map((w) => <WorkerCard key={w.id} worker={w} onAction={load} />)}
        </div>
      )}
      {adding && <AddWorkerModal onClose={() => setAdding(false)} onSaved={load} />}
    </div>
  );
}
