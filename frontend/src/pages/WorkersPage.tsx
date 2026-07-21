import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../api/client';
import { useWebSocket } from '../hooks/useWebSocket';
import type { Worker, TeamUser } from '../api/client';
import { Plus, X, RefreshCw, Trash2, Power, Play, Server, ScrollText, Pencil } from '../components/icons';

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

const BUSY = new Set(['creating', 'bootstrapping', 'stopping', 'starting', 'destroying']);

function AddWorkerModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState('');
  const [accounts, setAccounts] = useState<{ email: string; token: string; login_method: string }[]>([{ email: '', token: '', login_method: '' }]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.createWorker({
        name: name.trim(),
        accounts: accounts.filter((a) => a.email.trim()).map((a) => ({ email: a.email.trim(), token: a.token.trim(), login_method: a.login_method || undefined })),
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
                  value={acct.token} placeholder="Token 或邮箱密码"
                  onChange={(e) => setAccounts(accounts.map((a, j) => (j === i ? { ...a, token: e.target.value } : a)))}
                />
                <select
                  className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
                  value={acct.login_method}
                  onChange={(e) => setAccounts(accounts.map((a, j) => (j === i ? { ...a, login_method: e.target.value } : a)))}
                >
                  <option value="">自动识别（按邮箱后缀）</option>
                  <option value="171mail">171mail（API 接码）</option>
                  <option value="mailcom">mail.com（Chrome 接码）</option>
                  <option value="onet">Onet（Token 接码）</option>
                  <option value="gazeta">Gazeta（Token 接码）</option>
                </select>
              </div>
              {accounts.length > 1 && (
                <button type="button" className="text-gray-500 hover:text-red-400 mt-2"
                  onClick={() => setAccounts(accounts.filter((_, j) => j !== i))}><X size={16} /></button>
              )}
            </div>
          ))}
          <button type="button" onClick={() => setAccounts([...accounts, { email: '', token: '', login_method: '' }])}
            className="text-xs text-indigo-400 hover:text-indigo-300">+ 再加一个账号</button>
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-gray-300 hover:text-foreground">Cancel</button>
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

function WorkerCard({ worker, onAction, users, isAdmin }: { worker: Worker; onAction: () => void; users: TeamUser[]; isAdmin: boolean }) {
  const [logsOpen, setLogsOpen] = useState(false);
  const [poolOpen, setPoolOpen] = useState(false);
  const [pool, setPool] = useState<Awaited<ReturnType<typeof api.getWorkerPool>> | null>(null);
  const [poolErr, setPoolErr] = useState<string | null>(null);
  const [ptyEnabled, setPtyEnabled] = useState<boolean | null>(null);
  const [ptySwitching, setPtySwitching] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(worker.name);
  const editRef = useRef<HTMLInputElement>(null);
  const busy = BUSY.has(worker.status);

  const ccU = JSON.parse(localStorage.getItem('cc_user') || '{}');
  const canControl = isAdmin || worker.owner_user_id === ccU.id;

  useEffect(() => {
    if (editing && editRef.current) editRef.current.focus();
  }, [editing]);

  const saveRename = async () => {
    const trimmed = editName.trim();
    if (!trimmed || trimmed === worker.name) { setEditing(false); setEditName(worker.name); return; }
    try {
      await api.renameWorker(worker.id, trimmed);
      onAction();
    } catch (e) {
      window.alert(String(e));
      setEditName(worker.name);
    }
    setEditing(false);
  };

  useEffect(() => {
    if (worker.status !== 'ready') return;
    api.getWorkerRuntimeSettings(worker.id).then((s) => setPtyEnabled(s.use_pty_mode)).catch(() => {});
  }, [worker.id, worker.status]);

  const togglePool = async () => {
    if (poolOpen) { setPoolOpen(false); return; }
    setPoolOpen(true);
    setPool(null); setPoolErr(null);
    try {
      setPool(await api.getWorkerPoolUsage(worker.id));
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
          {editing ? (
            <input
              ref={editRef}
              className="bg-gray-700 text-foreground text-sm font-medium rounded px-2 py-0.5 outline-none focus:ring-1 focus:ring-indigo-500 min-w-0"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') saveRename(); if (e.key === 'Escape') { setEditing(false); setEditName(worker.name); } }}
              onBlur={saveRename}
            />
          ) : (
            <span
              className={`text-foreground font-medium truncate ${canControl ? 'cursor-pointer hover:text-indigo-300 group' : ''}`}
              title={canControl ? 'Click to rename' : worker.name}
              onClick={() => { if (canControl) { setEditName(worker.name); setEditing(true); } }}
            >
              {shortName(worker)}
              {canControl && <Pencil size={11} className="inline ml-1 opacity-0 group-hover:opacity-60" />}
            </span>
          )}
          <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${STATUS_COLORS[worker.status] || 'bg-gray-500/20 text-gray-400'}`}>
            {worker.status}{busy && worker.bootstrap_step ? `: ${worker.bootstrap_step}` : ''}
          </span>
          {isAdmin && (
            <select
              value={worker.owner_user_id ?? ''}
              onChange={async (e) => {
                const val = e.target.value ? Number(e.target.value) : null;
                try { await api.assignWorker(worker.id, val); onAction(); } catch {}
              }}
              className="text-xs bg-gray-700 text-gray-300 rounded px-1.5 py-0.5 border border-gray-600 shrink-0"
              title="Assign to user"
            >
              <option value="">Public Pool</option>
              {users.filter(u => u.role === 'member').map(u => <option key={u.id} value={u.id}>{u.name}</option>)}
            </select>
          )}
          {!isAdmin && worker.owner_user_id && (
            <span className="text-xs text-gray-500 shrink-0">
              {users.find(u => u.id === worker.owner_user_id)?.name || ''}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {(() => {
            return (<>
              {worker.status === 'ready' && ptyEnabled !== null && canControl && (
                <button
                  title={ptyEnabled ? 'PTY 模式：开（点击关闭）' : 'PTY 模式：关（点击开启）'}
                  disabled={ptySwitching}
                  onClick={async () => {
                    if (ptyEnabled && !window.confirm('关闭 PTY 模式将回退到 claude -p 一次性进程。确定？')) return;
                    setPtySwitching(true);
                    try {
                      const r = await api.updateWorkerRuntimeSettings(worker.id, { use_pty_mode: !ptyEnabled });
                      setPtyEnabled(r.use_pty_mode);
                    } catch { /* keep current */ }
                    finally { setPtySwitching(false); }
                  }}
                  className={`flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-semibold ${ptyEnabled ? 'bg-green-600/30 text-green-400' : 'bg-gray-700 text-gray-400'}`}
                >PTY</button>
              )}
              {worker.status === 'ready' && canControl && (
                <button title="Worker 号池额度" onClick={togglePool}
                  className={`flex items-center gap-0.5 px-1.5 py-0.5 rounded ${poolOpen ? 'bg-indigo-600/30 text-indigo-300' : 'bg-gray-700 text-gray-400'} hover:text-indigo-300 text-[10px] font-semibold`}>Pro</button>
              )}
              {canControl && (
                <button title="日志" onClick={() => setLogsOpen(true)}
                  className="p-1.5 text-gray-400 hover:text-gray-200"><ScrollText size={15} /></button>
              )}
              {canControl && worker.status === 'error' && (
                <button title="重试 bootstrap" onClick={() => act(api.retryWorker)}
                  className="p-1.5 text-gray-400 hover:text-blue-400"><RefreshCw size={15} /></button>
              )}
              {canControl && worker.status === 'ready' && (
                <button title="关机（EC2 stop，数据保留）" onClick={() => act(api.stopWorker, `关机 ${shortName(worker)}？数据保留，停机期间不可派发任务。`)}
                  className="p-1.5 text-gray-400 hover:text-yellow-400"><Power size={15} /></button>
              )}
              {canControl && worker.status === 'stopped' && (
                <button title="开机" onClick={() => act(api.startWorker)}
                  className="p-1.5 text-gray-400 hover:text-green-400"><Play size={15} /></button>
              )}
              {isAdmin && (
                <button title="销毁（terminate EC2）"
                  onClick={() => act(api.destroyWorker, `销毁 ${shortName(worker)}？EC2 实例将被 terminate，不可恢复！`)}
                  className="p-1.5 text-gray-400 hover:text-red-400"><Trash2 size={15} /></button>
              )}
            </>);
          })()}
        </div>
      </div>
      {isAdmin && (
        <div className="text-xs text-gray-400 flex flex-wrap gap-x-4 gap-y-1">
          {worker.private_ip && <span>内网 {worker.private_ip}</span>}
          {worker.cloud_instance_id && <span>{worker.cloud_instance_id}</span>}
          {worker.ccm_commit && <span title={worker.ccm_commit}>@{worker.ccm_commit.slice(0, 8)}</span>}
          {worker.last_heartbeat && <span>心跳 {new Date(worker.last_heartbeat + 'Z').toLocaleTimeString()}</span>}
        </div>
      )}

      {poolOpen && (
        <div className="bg-gray-900/60 rounded p-3 space-y-2">
          {poolErr ? (
            <span className="text-xs text-red-400 break-all">{poolErr}</span>
          ) : pool === null ? (
            <span className="text-xs text-gray-500">加载额度…</span>
          ) : (
            <>
              <div className="flex items-center gap-2 text-xs text-gray-500">
                <span>{pool.accounts?.length || 0} 个账号</span>
                {pool.available != null && <span>· {pool.available}/{pool.total} 可用</span>}
              </div>
              {pool.accounts && pool.accounts.length > 0 ? pool.accounts.map((a: any) => {
                const dot = !a.enabled ? 'bg-gray-500' : a.available ? 'bg-green-500' : 'bg-yellow-500';
                const barColor = (u: number) => u >= 85 ? 'bg-red-500' : u >= 60 ? 'bg-yellow-500' : 'bg-green-500';
                const textColor = (u: number) => u >= 85 ? 'text-red-400' : u >= 60 ? 'text-yellow-400' : 'text-green-400';
                const fmtReset = (s: string | null) => {
                  if (!s) return '';
                  const d = new Date(s);
                  return isNaN(d.getTime()) ? '' : d.toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
                };
                return (
                  <div key={a.id} className="rounded-lg border border-gray-700 p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className={`h-2 w-2 shrink-0 rounded-full ${dot}`} />
                      <span className="text-sm font-medium text-foreground truncate">{a.id}</span>
                      {a.subscription_type && (
                        <span className="px-1.5 py-0.5 rounded bg-indigo-600/30 text-indigo-300 text-[10px] font-semibold uppercase">{a.subscription_type}</span>
                      )}
                      <div className="ml-auto flex items-center gap-1">
                        <button onClick={async () => {
                          if (!window.confirm(`从 Worker 号池删除 ${a.id}（${a.email || ''})？`)) return;
                          try { await api.deleteWorkerAccount(worker.id, a.id); setPoolOpen(false); setTimeout(togglePool, 300); } catch (e) { window.alert(String(e)); }
                        }} className="text-[10px] px-1.5 py-0.5 rounded border border-gray-600 text-gray-400 hover:text-red-400 hover:border-red-500">删除</button>
                      </div>
                    </div>
                    {a.email && <div className="text-xs text-gray-500 truncate">{a.email}</div>}
                    {a.usage ? (
                      <div className="space-y-1.5">
                        {a.usage.five_hour && (
                          <div className="flex items-center gap-2 text-xs">
                            <span className="w-7 shrink-0 text-gray-500">5h</span>
                            <div className="flex-1 h-2 rounded-full bg-gray-700 overflow-hidden">
                              <div className={`h-full rounded-full ${barColor(a.usage.five_hour.utilization || 0)}`} style={{width: `${Math.min(100, a.usage.five_hour.utilization || 0)}%`}} />
                            </div>
                            <span className={`w-10 shrink-0 text-right font-medium ${textColor(a.usage.five_hour.utilization || 0)}`}>{(a.usage.five_hour.utilization || 0).toFixed(0)}%</span>
                            <span className="w-24 shrink-0 text-right text-gray-500">{fmtReset(a.usage.five_hour.resets_at)}</span>
                          </div>
                        )}
                        {a.usage.seven_day && (
                          <div className="flex items-center gap-2 text-xs">
                            <span className="w-7 shrink-0 text-gray-500">7d</span>
                            <div className="flex-1 h-2 rounded-full bg-gray-700 overflow-hidden">
                              <div className={`h-full rounded-full ${barColor(a.usage.seven_day.utilization || 0)}`} style={{width: `${Math.min(100, a.usage.seven_day.utilization || 0)}%`}} />
                            </div>
                            <span className={`w-10 shrink-0 text-right font-medium ${textColor(a.usage.seven_day.utilization || 0)}`}>{(a.usage.seven_day.utilization || 0).toFixed(0)}%</span>
                            <span className="w-24 shrink-0 text-right text-gray-500">{fmtReset(a.usage.seven_day.resets_at)}</span>
                          </div>
                        )}
                        {a.usage.seven_day_opus && (
                          <div className="flex items-center gap-2 text-xs">
                            <span className="w-7 shrink-0 text-gray-500">Opus</span>
                            <div className="flex-1 h-2 rounded-full bg-gray-700 overflow-hidden">
                              <div className={`h-full rounded-full ${barColor(a.usage.seven_day_opus.utilization || 0)}`} style={{width: `${Math.min(100, a.usage.seven_day_opus.utilization || 0)}%`}} />
                            </div>
                            <span className={`w-10 shrink-0 text-right font-medium ${textColor(a.usage.seven_day_opus.utilization || 0)}`}>{(a.usage.seven_day_opus.utilization || 0).toFixed(0)}%</span>
                            <span className="w-24 shrink-0 text-right text-gray-500">{fmtReset(a.usage.seven_day_opus.resets_at)}</span>
                          </div>
                        )}
                      </div>
                    ) : a.usage_error ? (
                      <div className="text-xs text-red-400">
                        {a.usage_error === 'no_credentials' ? '未找到凭据文件' : a.usage_error === 'token_expired' ? 'Token 过期' : a.usage_error}
                      </div>
                    ) : null}
                  </div>
                );
              }) : <div className="text-xs text-gray-500">暂无账号</div>}
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
                  className="text-xs text-indigo-400 hover:text-indigo-300"
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
  const [users, setUsers] = useState<TeamUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ccUser = JSON.parse(localStorage.getItem('cc_user') || '{}');
  const isAdmin = ccUser.role === 'admin' || ccUser.role === 'super_admin' || !ccUser.role;

  const load = useCallback(() => {
    api.listWorkers()
      .then((w) => { setWorkers(w); setError(null); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
    if (isAdmin) {
      api.getTeamUsers().then(setUsers).catch(() => {});
    }
  }, [isAdmin]);

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
        <h2 className="text-lg font-semibold text-foreground">{isAdmin ? 'Workers' : 'My Workers'}</h2>
        {isAdmin && (
          <button onClick={() => setAdding(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500">
            <Plus size={15} /> Add Worker
          </button>
        )}
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
          {workers.map((w) => <WorkerCard key={w.id} worker={w} onAction={load} users={users} isAdmin={isAdmin} />)}
        </div>
      )}
      {adding && <AddWorkerModal onClose={() => setAdding(false)} onSaved={load} />}
    </div>
  );
}
