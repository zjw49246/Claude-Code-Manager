import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../api/client';
import { useWebSocket } from '../hooks/useWebSocket';
import type { Worker } from '../api/client';
import { Plus, X, RefreshCw, Trash2, Power, Play, Server, ScrollText, KeyRound } from 'lucide-react';

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
  const [name, setName] = useState('');
  const [accounts, setAccounts] = useState<{ email: string; token: string }[]>([{ email: '', token: '' }]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.createWorker({
        name: name.trim(),
        accounts: accounts.filter((a) => a.email.trim()).map((a) => ({ email: a.email.trim(), token: a.token.trim() })),
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
          <div>
            <label className="block text-xs text-gray-400 mb-1">Worker 名称 *（也作为 AWS 实例名）</label>
            <input
              className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={name} onChange={(e) => setName(e.target.value)} placeholder="如 worker-prod-1" required
            />
          </div>
          {accounts.map((acct, i) => (
            <div key={i} className="flex gap-2 items-start">
              <div className="flex-1 space-y-2">
                <input
                  className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
                  value={acct.email} placeholder={`账号 ${i + 1} Email`}
                  onChange={(e) => setAccounts(accounts.map((a, j) => (j === i ? { ...a, email: e.target.value } : a)))}
                />
                <input
                  className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
                  value={acct.token} placeholder="接码 Token（mail.com 域填邮箱密码）"
                  onChange={(e) => setAccounts(accounts.map((a, j) => (j === i ? { ...a, token: e.target.value } : a)))}
                />

              </div>
              {accounts.length > 1 && (
                <button type="button" className="text-gray-500 hover:text-red-400 mt-2"
                  onClick={() => setAccounts(accounts.filter((_, j) => j !== i))}><X size={16} /></button>
              )}
            </div>
          ))}
          <button type="button" onClick={() => setAccounts([...accounts, { email: '', token: '' }])}
            className="text-xs text-indigo-400 hover:text-indigo-300">+ 再加一个账号</button>
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-gray-300 hover:text-white">Cancel</button>
            <button type="submit" disabled={submitting || !name.trim()}
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
    api.getWorkerLogs(worker.id).then((r) => setLog(r.bootstrap_log || '')).catch(() => {});
  }, [worker.id]);

  // 增量日志走 WS（worker_update 带 log_line），不再轮询全量日志
  useWebSocket(['workers'], (msg) => {
    const data = (msg.data || {}) as Record<string, unknown>;
    if (msg.channel === 'workers' && data.worker_id === worker.id && typeof data.log_line === 'string') {
      setLog((prev) => prev + data.log_line);
    }
  });

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

function shortName(w: Worker): string {
  return w.name;
}

function WorkerCard({ worker, onAction }: { worker: Worker; onAction: () => void }) {
  const [logsOpen, setLogsOpen] = useState(false);
  const [poolOpen, setPoolOpen] = useState(false);
  const [pool, setPool] = useState<Awaited<ReturnType<typeof api.getWorkerPool>> | null>(null);
  const [poolErr, setPoolErr] = useState<string | null>(null);
  const busy = BUSY.has(worker.status);

  const togglePool = async () => {
    if (poolOpen) { setPoolOpen(false); return; }
    setPoolOpen(true);
    setPool(null); setPoolErr(null);
    try {
      setPool(await api.getWorkerPool(worker.id));
    } catch (e) {
      setPoolErr(String(e));
    }
  };

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
          <span className="text-foreground font-medium truncate" title={worker.name}>{shortName(worker)}</span>
          <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${STATUS_COLORS[worker.status] || 'bg-gray-500/20 text-gray-400'}`}>
            {worker.status}{busy && worker.bootstrap_step ? `: ${worker.bootstrap_step}` : ''}
          </span>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {worker.status === 'ready' && (
            <button title="CC 账号池" onClick={togglePool}
              className={`p-1.5 ${poolOpen ? 'text-teal-300' : 'text-gray-400'} hover:text-teal-300`}><KeyRound size={15} /></button>
          )}
          <button title="日志" onClick={() => setLogsOpen(true)}
            className="p-1.5 text-gray-400 hover:text-gray-200"><ScrollText size={15} /></button>
          {worker.status === 'error' && (
            <button title="重试 bootstrap" onClick={() => act(api.retryWorker)}
              className="p-1.5 text-gray-400 hover:text-blue-400"><RefreshCw size={15} /></button>
          )}
          {worker.status === 'ready' && (
            <button title="关机（EC2 stop，数据保留）" onClick={() => act(api.stopWorker, `关机 ${shortName(worker)}？数据保留，停机期间不可派发任务。`)}
              className="p-1.5 text-gray-400 hover:text-yellow-400"><Power size={15} /></button>
          )}
          {worker.status === 'stopped' && (
            <button title="开机" onClick={() => act(api.startWorker)}
              className="p-1.5 text-gray-400 hover:text-green-400"><Play size={15} /></button>
          )}
          {(
            <button title="销毁（terminate EC2）"
              onClick={() => act(api.destroyWorker, `销毁 ${shortName(worker)}？EC2 实例将被 terminate，不可恢复！`)}
              className="p-1.5 text-gray-400 hover:text-red-400"><Trash2 size={15} /></button>
          )}
        </div>
      </div>
      <div className="text-xs text-gray-400 flex flex-wrap gap-x-4 gap-y-1">
        {worker.private_ip && <span>内网 {worker.private_ip}</span>}
        {worker.cloud_instance_id && <span>{worker.cloud_instance_id}</span>}
        {worker.ccm_commit && <span title={worker.ccm_commit}>@{worker.ccm_commit.slice(0, 8)}</span>}
        {worker.last_heartbeat && <span>心跳 {new Date(worker.last_heartbeat + 'Z').toLocaleTimeString()}</span>}
      </div>
      {(worker.accounts || []).length > 0 && (
        <div className="text-xs flex flex-wrap gap-2">
          {(worker.accounts || []).map((a, i) => (
            <span key={i} className={`${ACCOUNT_COLORS[a.status] || 'text-gray-400'}`}>{a.email} ({a.status})</span>
          ))}
        </div>
      )}
      {poolOpen && (
        <div className="text-xs bg-gray-900/60 rounded p-2 space-y-2">
          {poolErr ? (
            <span className="text-red-400 break-all">{poolErr}</span>
          ) : pool === null ? (
            <span className="text-gray-500">加载账号池…</span>
          ) : (
            <>
              <div className="text-gray-500">
                {pool.accounts.length === 0 ? '暂无账号' :
                  (pool as { single_account?: boolean }).single_account ? '单账号模式' : `号池 ${pool.available}/${pool.total} 可用`}
              </div>
              {pool.accounts.map((a) => (
                <div key={a.id} className="flex items-center gap-2">
                  <span className={a.available ? 'text-emerald-400' : a.enabled ? 'text-yellow-400' : 'text-gray-500'}>●</span>
                  <span className="text-gray-300 flex-1">{a.email || a.id}</span>
                  <button onClick={async () => {
                    if (!window.confirm(`从 Worker 号池删除 ${a.id}？`)) return;
                    try { await api.deleteWorkerAccount(worker.id, a.id); togglePool(); togglePool(); } catch (e) { window.alert(String(e)); }
                  }} className="text-gray-500 hover:text-red-400" title="删除">×</button>
                </div>
              ))}
              <div className="pt-1 border-t border-gray-700">
                <button
                  onClick={() => {
                    const email = window.prompt('邮箱');
                    if (!email) return;
                    const token = window.prompt('接码 Token（mail.com 域填邮箱密码）');
                    if (!token) return;
                    api.addWorkerAccount(worker.id, { email, token }).then(() => {
                      window.alert('登录已启动，可能需要 1-2 分钟。完成后刷新查看。');
                    }).catch((e) => window.alert(String(e)));
                  }}
                  className="text-indigo-400 hover:text-indigo-300"
                >+ 添加账号</button>
              </div>
            </>
          )}
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
    // WS 是主信号，这里只留慢速兜底轮询（WS 断开/丢消息时）
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  // 状态变化实时推送：provisioner 每次 _update 都广播到 workers channel
  useWebSocket(['workers'], (msg) => {
    if (msg.channel === 'workers') load();
  });

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
