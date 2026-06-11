import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, X, Users } from 'lucide-react';
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

function AccountCard({ account, preferred, lastSelected, onClearCooldown, onSetPreferred }: {
  account: PoolAccountUsage;
  preferred: string | null;
  lastSelected: string | null;
  onClearCooldown: (id: string) => void;
  onSetPreferred: (id: string | null) => void;
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
        <div className="text-xs text-red-400">
          {account.usage_error === 'no_credentials' && '未找到凭据文件'}
          {account.usage_error === 'token_expired' && 'Token 已过期，需重新登录'}
          {account.usage_error && !['no_credentials', 'token_expired'].includes(account.usage_error) && `额度获取失败: ${account.usage_error}`}
        </div>
      )}
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
            <div className="flex-1 overflow-y-auto p-3 space-y-2">
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
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
