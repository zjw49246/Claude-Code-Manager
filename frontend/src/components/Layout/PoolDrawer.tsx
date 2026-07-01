import { useCallback, useEffect, useState } from 'react';
import { Plus, RefreshCw, X, Users } from 'lucide-react';
import { api } from '../../api/client';
import type { PoolAccountUsage, PoolUsageStatus, PoolUsageWindow } from '../../api/client';

function barColor(utilization: number): string {
  if (utilization >= 85) return 'bg-red-500';
  if (utilization >= 60) return 'bg-yellow-500';
  return 'bg-green-500';
}

function textColor(utilization: number): string {
  if (utilization >= 85) return 'text-red-400';
  if (utilization >= 60) return 'text-yellow-400';
  return 'text-green-400';
}

function formatReset(resetsAt: string | null): string {
  if (!resetsAt) return '';
  const d = new Date(resetsAt);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function UsageBar({ label, window: w }: { label: string; window: PoolUsageWindow | null }) {
  if (!w || w.utilization == null) return null;
  const pct = Math.min(100, Math.max(0, w.utilization));
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-7 shrink-0 text-gray-500">{label}</span>
      <div className="flex-1 h-2 rounded-full bg-gray-700 overflow-hidden">
        <div className={`h-full rounded-full ${barColor(pct)}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`w-10 shrink-0 text-right font-medium ${textColor(pct)}`}>{pct.toFixed(0)}%</span>
      <span className="w-24 shrink-0 text-right text-gray-500" title="额度重置时间">{formatReset(w.resets_at)}</span>
    </div>
  );
}

function AccountCard({ account, preferred, lastSelected, onClearCooldown, onSetPreferred, onRelogin, onRetryUsage, reloginState }: {
  account: PoolAccountUsage;
  preferred: string | null;
  lastSelected: string | null;
  onClearCooldown: (id: string) => void;
  onSetPreferred: (id: string | null) => void;
  onRelogin: (id: string) => void;
  onRetryUsage: () => void;
  reloginState?: { status: string; message?: string };
}) {
  const statusDot = !account.enabled
    ? { cls: 'bg-gray-500', label: '已禁用' }
    : account.available
      ? { cls: 'bg-green-500', label: '可用' }
      : { cls: 'bg-yellow-500', label: '冷却中' };
  const isPreferred = preferred === account.id;
  const isLastSelected = lastSelected === account.id;

  return (
    <div className={`rounded-lg border bg-gray-800 p-3 space-y-2 ${isPreferred ? 'border-indigo-500' : 'border-gray-700'}`}>
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${statusDot.cls}`} title={statusDot.label} />
        <span className="text-sm font-medium text-foreground truncate">{account.id}</span>
        {account.subscription_type && (
          <span className="px-1.5 py-0.5 rounded bg-indigo-600/30 text-indigo-300 text-[10px] font-semibold uppercase">
            {account.subscription_type}
          </span>
        )}
        {isPreferred && (
          <span className="px-1.5 py-0.5 rounded bg-green-600/30 text-green-300 text-[10px] font-semibold">
            当前指定
          </span>
        )}
        {isLastSelected && (
          <span className="px-1.5 py-0.5 rounded bg-cyan-600/30 text-cyan-300 text-[10px] font-semibold" title="最近一次 launch 选中的账号（每次发消息时重新选号）">
            最近使用
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {!account.available && account.enabled && (
            <button
              onClick={() => onClearCooldown(account.id)}
              className="text-[10px] text-gray-400 hover:text-foreground underline"
              title="清除冷却，立即恢复可用"
            >
              解除冷却
            </button>
          )}
          {account.enabled && (
            isPreferred ? (
              <button
                onClick={() => onSetPreferred(null)}
                className="text-[10px] px-1.5 py-0.5 rounded border border-gray-600 text-gray-400 hover:text-foreground hover:border-gray-400"
                title="取消指定，恢复自动轮换"
              >
                恢复自动
              </button>
            ) : (
              <button
                onClick={() => onSetPreferred(account.id)}
                className="text-[10px] px-1.5 py-0.5 rounded border border-indigo-500/50 text-indigo-300 hover:bg-indigo-600/20"
                title="下个 turn 起切换到此账号（session 自动迁移；若限流则自动回落其他账号）"
              >
                切换到此账号
              </button>
            )
          )}
          <button
            onClick={async () => {
              if (!window.confirm(`从号池中删除 ${account.id}（${account.email}）？\nconfig_dir 文件夹保留，可以重新登录其他号。`)) return;
              try { await api.poolDeleteAccount(account.id); window.location.reload(); } catch (e) { window.alert(String(e)); }
            }}
            className="text-[10px] px-1.5 py-0.5 rounded border border-gray-600 text-gray-400 hover:text-red-400 hover:border-red-500"
            title="从号池删除（保留文件夹）"
          >
            删除
          </button>
        </div>
      </div>
      {account.email && <div className="text-xs text-gray-500 truncate">{account.email}</div>}
      {account.usage ? (
        <div className="space-y-1.5">
          <UsageBar label="5h" window={account.usage.five_hour} />
          <UsageBar label="7d" window={account.usage.seven_day} />
          <UsageBar label="Opus" window={account.usage.seven_day_opus} />
        </div>
      ) : (
        <div className="text-xs text-red-400 space-y-1">
          <div className="flex items-center gap-2">
            <span>
              {account.usage_error === 'no_credentials' && '未找到凭据文件'}
              {account.usage_error === 'token_expired' && 'Token 刷新失败，需重新登录'}
              {account.usage_error && !['no_credentials', 'token_expired'].includes(account.usage_error) && `额度获取失败: ${account.usage_error}`}
            </span>
            {account.usage_error && (() => {
              const needsRelogin = ['no_credentials', 'token_expired'].includes(account.usage_error!);
              return (<>
                {needsRelogin ? (<>
                  <button
                    onClick={() => onRelogin(account.id)}
                    disabled={reloginState?.status === 'running'}
                    className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border border-red-500/50 text-red-300 hover:bg-red-600/20 disabled:opacity-50"
                    title="先尝试刷新 OAuth token；刷新失败则后台跑 auto_login 重新登录"
                  >
                    {reloginState?.status === 'running' ? '登录中…' : '重新登录'}
                  </button>
                </>) : (
                  <button
                    onClick={onRetryUsage}
                    className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border border-yellow-500/50 text-yellow-300 hover:bg-yellow-600/20"
                    title="临时错误，重新拉取额度"
                  >
                    重试
                  </button>
                )}
              </>);
            })()}
          </div>
          {reloginState?.message && (
            <div className="text-[10px] text-gray-400 whitespace-pre-wrap break-all">{reloginState.message}</div>
          )}
        </div>
      )}
    </div>
  );
}


function AddAccountModal({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [email, setEmail] = useState('');
  const [token, setToken] = useState('');
  const [loginMethod, setLoginMethod] = useState('');

  const [status, setStatus] = useState<string | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !token.trim()) return;
    setSubmitting(true);
    setDetail(null);
    try {
      await api.poolAddAccount({ email: email.trim(), token: token.trim(), login_method: loginMethod || undefined });
      setStatus('running');
      const poll = async () => {
        const s = await api.poolAddStatus(email.trim());
        if (s.status === 'running') { setTimeout(poll, 5000); return; }
        setStatus(s.status);
        if (s.status === 'failed') setDetail(s.detail?.slice(-500) || '登录失败');
        if (s.status === 'success') { onAdded(); onClose(); }
      };
      setTimeout(poll, 5000);
    } catch (e) {
      setStatus('failed');
      setDetail(e instanceof Error ? e.message : '请求失败');
      setSubmitting(false);
    }
  };

  return (
    <div className="absolute inset-0 bg-gray-900/80 z-10 flex items-start justify-center pt-16">
      <div className="bg-gray-800 rounded-lg shadow-xl w-full max-w-xs">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700">
          <h3 className="text-sm font-semibold text-foreground">添加账号</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={14} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-3">
          <div>
            <label className="block text-xs text-gray-400 mb-1">邮箱</label>
            <input className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-indigo-500"
              value={email} onChange={e => setEmail(e.target.value)} placeholder="user@example.com" required />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">接码 Token / 密码</label>
            <input className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-indigo-500"
              value={token} onChange={e => setToken(e.target.value)} placeholder="Token 或邮箱密码" required />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">登录方式</label>
            <select
              className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-indigo-500"
              value={loginMethod} onChange={e => setLoginMethod(e.target.value)}
            >
              <option value="">自动识别（按邮箱后缀）</option>
              <option value="171mail">171mail（API 接码）</option>
              <option value="mailcom">mail.com（Chrome 接码）</option>
            </select>
          </div>
          {status === 'running' && <p className="text-xs text-blue-400">登录中… 请等待（可能需要 1-2 分钟）</p>}
          {status === 'failed' && <p className="text-xs text-red-400 break-all">{detail || '登录失败'}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className="px-3 py-1.5 text-xs text-gray-300 hover:text-white">取消</button>
            <button type="submit" disabled={submitting || status === 'running' || !email.trim() || !token.trim()}
              className="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-500 disabled:opacity-50">
              {status === 'running' ? '登录中…' : '添加'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function PoolDrawer() {
  const [poolEnabled, setPoolEnabled] = useState(false);
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<PoolUsageStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getPoolStatus()
      .then((s) => setPoolEnabled(s.enabled))
      .catch(() => setPoolEnabled(false)); // pool 未启用时后端返回 404
  }, []);

  const loadUsage = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await api.getPoolUsage());
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) loadUsage();
  }, [open, loadUsage]);

  const handleClearCooldown = useCallback(async (accountId: string) => {
    try {
      await api.clearPoolCooldown(accountId);
      await loadUsage();
    } catch {
      // 失败时保持原状态
    }
  }, [loadUsage]);

  const handleSetPreferred = useCallback(async (accountId: string | null) => {
    try {
      await api.setPoolPreferred(accountId);
      await loadUsage();
    } catch {
      // 失败时保持原状态
    }
  }, [loadUsage]);

  const [relogin, setRelogin] = useState<Record<string, { status: string; message?: string }>>({});
  const [showAdd, setShowAdd] = useState(false);

  const handleRelogin = useCallback(async (accountId: string) => {
    setRelogin((m) => ({ ...m, [accountId]: { status: 'running' } }));
    try {
      const res = await api.poolRelogin(accountId);
      if (res.status === 'success') {
        // OAuth refresh 直接成功（最常见）
        setRelogin((m) => ({ ...m, [accountId]: { status: 'success' } }));
        await loadUsage();
        return;
      }
      // auto_login 后台跑，轮询直到结束
      const poll = async () => {
        const s = await api.poolReloginStatus(accountId);
        if (s.status === 'running') { setTimeout(poll, 5000); return; }
        setRelogin((m) => ({ ...m, [accountId]: {
          status: s.status,
          message: s.status === 'failed' ? `登录失败：${(s.detail || '').slice(-300)}` : undefined,
        } }));
        if (s.status === 'success') await loadUsage();
      };
      setTimeout(poll, 5000);
    } catch (e) {
      setRelogin((m) => ({ ...m, [accountId]: {
        status: 'failed',
        message: e instanceof Error ? e.message : '重新登录失败',
      } }));
    }
  }, [loadUsage]);

  if (!poolEnabled) return null;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-1 px-2 py-1 rounded bg-gray-800 border border-gray-700 hover:border-indigo-500 transition-colors"
        title="Claude Pool 账号额度"
      >
        <Users size={13} className="text-indigo-400" />
        <span className="text-xs font-semibold text-indigo-300">Pro</span>
      </button>
      {open && (
        <div className="fixed inset-0 z-50">
          <div className="absolute inset-0 bg-black/50" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-0 h-full w-full max-w-sm bg-gray-900 border-l border-gray-700 shadow-xl flex flex-col">
            <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-700">
              <Users size={16} className="text-indigo-400" />
              <h2 className="text-sm font-semibold text-foreground">Claude Pool 额度</h2>
              {status && (
                <span className="text-xs text-gray-500">
                  {status.available}/{status.total} 可用
                </span>
              )}
              <div className="ml-auto flex items-center gap-1">
                <button
                  onClick={() => setShowAdd(true)}
                  className="p-1.5 rounded text-gray-400 hover:text-foreground hover:bg-gray-800"
                  title="添加账号"
                >
                  <Plus size={14} />
                </button>
                <button
                  onClick={loadUsage}
                  disabled={loading}
                  className="p-1.5 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 disabled:opacity-50"
                  title="刷新"
                >
                  <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
                </button>
                <button
                  onClick={() => setOpen(false)}
                  className="p-1.5 rounded text-gray-400 hover:text-foreground hover:bg-gray-800"
                >
                  <X size={14} />
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto p-3 space-y-2 relative">
              {showAdd && <AddAccountModal onClose={() => setShowAdd(false)} onAdded={loadUsage} />}
              {error && <div className="text-xs text-red-400">{error}</div>}
              {loading && !status && <div className="text-xs text-gray-500">加载中…</div>}
              {status?.accounts.map((a) => (
                <AccountCard
                  key={a.id}
                  account={a}
                  preferred={status?.preferred ?? null}
                  lastSelected={status?.last_selected ?? null}
                  onClearCooldown={handleClearCooldown}
                  onSetPreferred={handleSetPreferred}
                  onRelogin={handleRelogin}
                  onRetryUsage={loadUsage}
                  reloginState={relogin[a.id]}
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
