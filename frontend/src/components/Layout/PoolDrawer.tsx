import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Plus, RefreshCw, X, Users, Settings } from '../icons';
import { api } from '../../api/client';
import type { CodexLoginMethod, CodexLoginStatus, CodexPoolAccountUsage, CodexPoolUsageStatus, PoolAccountUsage, PoolUsageStatus, PoolUsageWindow } from '../../api/client';

const ACTIVE_CODEX_LOGIN_STATUSES = new Set([
  'running', 'awaiting_otp', 'verifying_otp', 'finalizing',
]);

function barColor(utilization: number): string {
  if (utilization >= 90) return 'bg-red-500';
  if (utilization >= 60) return 'bg-yellow-500';
  return 'bg-green-500';
}

function textColor(utilization: number): string {
  if (utilization >= 90) return 'text-red-400';
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

// --- Codex quota helpers ---
function formatResetCountdown(epochSec: number | null): string {
  if (!epochSec) return '';
  const now = Date.now() / 1000;
  const diff = epochSec - now;
  if (diff <= 0) return '已重置';
  const d = Math.floor(diff / 86400);
  const h = Math.floor((diff % 86400) / 3600);
  const m = Math.floor((diff % 3600) / 60);
  if (d > 0) return `${d}天${h}小时后重置`;
  if (h > 0) return `${h}小时${m}分钟后重置`;
  return `${m}分钟后重置`;
}

function formatWindowName(minutes: number | null): string {
  if (!minutes) return '';
  const days = Math.round(minutes / 60 / 24);
  if (days >= 7) return '7天窗口';
  if (days >= 1) return `${days}天窗口`;
  const hours = Math.round(minutes / 60);
  return `${hours}小时窗口`;
}

function AccountCard({ account, preferred, lastSelected, onClearCooldown, onSetPreferred, onRelogin, onRetryUsage, onDelete, reloginState }: {
  account: PoolAccountUsage;
  preferred: string | null;
  lastSelected: string | null;
  onClearCooldown: (id: string) => void;
  onSetPreferred: (id: string | null) => void;
  onRelogin: (id: string) => void;
  onRetryUsage: () => void;
  onDelete: (id: string) => void;
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
          <span className="px-1.5 py-0.5 rounded bg-cyan-600/30 text-cyan-300 text-[10px] font-semibold" title="最近一次 launch 选中的账号">
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
                title="切换到此账号"
              >
                切换到此账号
              </button>
            )
          )}
          <button
            onClick={() => onDelete(account.id)}
            className="text-[10px] px-1.5 py-0.5 rounded border border-gray-600 text-gray-400 hover:text-red-400 hover:border-red-500"
            title="从号池删除"
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
        <div className={`text-xs space-y-1 ${account.usage_error === 'token_expired' ? 'text-yellow-400' : 'text-red-400'}`}>
          <div className="flex items-center gap-2">
            <span>
              {account.usage_error === 'no_credentials' && '未找到凭据文件'}
              {account.usage_error === 'token_expired' && 'Token 过期，将在使用时自动刷新'}
              {account.usage_error && !['no_credentials', 'token_expired'].includes(account.usage_error) && `额度获取失败: ${account.usage_error}`}
            </span>
            {account.usage_error === 'no_credentials' && (
              <button
                onClick={() => onRelogin(account.id)}
                disabled={reloginState?.status === 'running'}
                className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border border-red-500/50 text-red-300 hover:bg-red-600/20 disabled:opacity-50"
              >
                {reloginState?.status === 'running' ? '登录中…' : '重新登录'}
              </button>
            )}
            {account.usage_error && !['no_credentials', 'token_expired'].includes(account.usage_error) && (
              <button
                onClick={onRetryUsage}
                className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border border-yellow-500/50 text-yellow-300 hover:bg-yellow-600/20"
              >
                重试
              </button>
            )}
          </div>
          {reloginState?.message && (
            <div className="text-[10px] text-gray-400 whitespace-pre-wrap break-all">{reloginState.message}</div>
          )}
        </div>
      )}
    </div>
  );
}

// --- Codex Account Card ---
function CodexOtpPrompt({ state, onSubmit }: {
  state: CodexLoginStatus;
  onSubmit: (code: string) => Promise<void>;
}) {
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!/^\d{6}$/.test(code)) {
      setError('请输入 6 位数字验证码');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(code);
      setCode('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '验证码提交失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="rounded border border-amber-500/40 bg-amber-500/10 p-2 space-y-2">
      <div className="text-xs text-amber-300">OpenAI 要求邮箱验证码，请从邮箱中取得最新的 6 位码。</div>
      {state.expires_at && <div className="text-[10px] text-gray-400">{formatResetCountdown(state.expires_at)}</div>}
      <div className="flex gap-2">
        <input
          aria-label="OpenAI 邮箱验证码"
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={6}
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
          className="min-w-0 flex-1 bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-amber-500"
          placeholder="6 位验证码"
        />
        <button
          type="button"
          onClick={submit}
          disabled={submitting || code.length !== 6}
          className="px-2.5 py-1.5 text-xs rounded bg-amber-600 text-white hover:bg-amber-500 disabled:opacity-50"
        >
          {submitting ? '提交中…' : '继续登录'}
        </button>
      </div>
      {error && <div className="text-[10px] text-red-400">{error}</div>}
    </div>
  );
}

function CodexAccountCard({ account, preferred, onClearCooldown, onSetPreferred, onRelogin, onSubmitOtp, onDelete, onRetryUsage, reloginState }: {
  account: CodexPoolAccountUsage;
  preferred: string | null;
  onClearCooldown: (id: string) => void;
  onSetPreferred: (id: string | null) => void;
  onRelogin: (id: string) => void;
  onSubmitOtp: (state: CodexLoginStatus, code: string) => Promise<void>;
  onDelete: (id: string) => void;
  onRetryUsage: () => void;
  reloginState?: CodexLoginStatus;
}) {
  const statusDot = !account.enabled
    ? { cls: 'bg-gray-500', label: '已禁用' }
    : account.available
      ? { cls: 'bg-green-500', label: '可用' }
      : { cls: 'bg-yellow-500', label: '冷却中' };

  const q = account.quota;
  const isPreferred = preferred === account.id;

  return (
    <div className={`rounded-lg border bg-gray-800 p-3 space-y-2 ${isPreferred ? 'border-emerald-500' : 'border-gray-700'}`}>
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${statusDot.cls}`} title={statusDot.label} />
        <span className="text-sm font-medium text-foreground truncate">{account.id}</span>
        {account.plan_type && (
          <span className="px-1.5 py-0.5 rounded bg-emerald-600/30 text-emerald-300 text-[10px] font-semibold uppercase">
            {account.plan_type}
          </span>
        )}
        {q?.has_credits && (
          <span className="px-1.5 py-0.5 rounded bg-amber-600/30 text-amber-300 text-[10px] font-semibold">
            Credits
          </span>
        )}
        {isPreferred && (
          <span className="px-1.5 py-0.5 rounded bg-green-600/30 text-green-300 text-[10px] font-semibold">
            当前指定
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {!account.available && account.enabled && (
            <button onClick={() => onClearCooldown(account.id)} className="text-[10px] text-gray-400 hover:text-foreground underline">
              解除冷却
            </button>
          )}
          {account.enabled && (
            isPreferred ? (
              <button
                onClick={() => onSetPreferred(null)}
                className="text-[10px] px-1.5 py-0.5 rounded border border-gray-600 text-gray-400 hover:text-foreground hover:border-gray-400"
                title="取消指定，恢复 Codex 自动轮换"
              >
                恢复自动
              </button>
            ) : (
              <button
                onClick={() => onSetPreferred(account.id)}
                className="text-[10px] px-1.5 py-0.5 rounded border border-emerald-500/50 text-emerald-300 hover:bg-emerald-600/20"
                title="切换到此 Codex 账号"
              >
                切换到此账号
              </button>
            )
          )}
          <button
            onClick={() => onRelogin(account.id)}
            disabled={Boolean(reloginState && ACTIVE_CODEX_LOGIN_STATUSES.has(reloginState.status))}
            className="text-[10px] px-1.5 py-0.5 rounded border border-emerald-500/50 text-emerald-300 hover:bg-emerald-600/20 disabled:opacity-50"
          >
            {reloginState && ACTIVE_CODEX_LOGIN_STATUSES.has(reloginState.status) ? '登录中…' : '重新登录'}
          </button>
          <button
            onClick={() => onDelete(account.id)}
            className="text-[10px] px-1.5 py-0.5 rounded border border-gray-600 text-gray-400 hover:text-red-400 hover:border-red-500"
          >
            删除
          </button>
        </div>
      </div>
      {account.email && <div className="text-xs text-gray-500 truncate">{account.email}</div>}
      <div className="text-[10px] text-gray-500 font-mono truncate" title={account.codex_home}>
        CODEX_HOME: {account.codex_home}
      </div>
      {q ? (
        <div className="space-y-2">
          {/* Primary window */}
          {q.primary_used_percent != null && (
            <div className="space-y-1">
              <div className="flex items-center justify-between text-[10px]">
                <span className="text-gray-400">{formatWindowName(q.primary_window_minutes) || '主窗口'}</span>
                <span className="text-gray-500">{formatResetCountdown(q.primary_resets_at)}</span>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <div className="flex-1 h-2.5 rounded-full bg-gray-700 overflow-hidden">
                  <div className={`h-full rounded-full ${barColor(q.primary_used_percent)}`} style={{ width: `${Math.min(100, q.primary_used_percent)}%` }} />
                </div>
                <span className={`w-16 shrink-0 text-right font-medium ${textColor(q.primary_used_percent)}`}>
                  已用 {q.primary_used_percent.toFixed(1)}%
                </span>
              </div>
              <div className="text-[10px] text-gray-500">
                剩余 {(100 - q.primary_used_percent).toFixed(1)}%
              </div>
            </div>
          )}
          {/* Secondary window */}
          {q.secondary_used_percent != null && (
            <div className="space-y-1">
              <div className="flex items-center justify-between text-[10px]">
                <span className="text-gray-400">{formatWindowName(q.secondary_window_minutes) || '副窗口'}</span>
                <span className="text-gray-500">{formatResetCountdown(q.secondary_resets_at)}</span>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <div className="flex-1 h-2.5 rounded-full bg-gray-700 overflow-hidden">
                  <div className={`h-full rounded-full ${barColor(q.secondary_used_percent)}`} style={{ width: `${Math.min(100, q.secondary_used_percent)}%` }} />
                </div>
                <span className={`w-16 shrink-0 text-right font-medium ${textColor(q.secondary_used_percent)}`}>
                  已用 {q.secondary_used_percent.toFixed(1)}%
                </span>
              </div>
            </div>
          )}
          {q.is_rate_limited && (
            <div className="text-[10px] text-red-400 font-medium">已触发限速</div>
          )}
        </div>
      ) : (
        <div className="text-xs space-y-1">
          <div className="flex items-center gap-2 text-gray-500">
            <span>{account.quota_error === 'no_rollout_data'
              ? '暂无额度数据（使用后自动更新）'
              : account.quota_error === 'live_unavailable'
                ? '实时额度查询失败，无法确认当前额度'
                : (account.quota_error || '未知')}</span>
            <button
              onClick={onRetryUsage}
              className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border border-gray-600 text-gray-400 hover:text-foreground"
            >
              刷新
            </button>
          </div>
        </div>
      )}
      {reloginState?.status === 'running' && (
        <div className="text-xs text-blue-400">自动登录中…</div>
      )}
      {reloginState?.status === 'awaiting_otp' && (
        <CodexOtpPrompt state={reloginState} onSubmit={(code) => onSubmitOtp(reloginState, code)} />
      )}
      {reloginState?.status === 'verifying_otp' && (
        <div className="text-xs text-blue-400">验证码已提交，正在继续登录…</div>
      )}
      {reloginState?.status === 'finalizing' && (
        <div className="text-xs text-blue-400">登录已完成，正在安全提交登录结果…</div>
      )}
      {reloginState && ACTIVE_CODEX_LOGIN_STATUSES.has(reloginState.status) && reloginState.detail && (
        <div className="text-[10px] text-amber-400 break-all">{reloginState.detail}</div>
      )}
      {(reloginState?.status === 'failed' || reloginState?.status === 'expired') && (
        <div className="text-xs text-red-400 break-all">{reloginState.detail || '登录失败'}</div>
      )}
      {reloginState?.status === 'success' && (
        <div className="text-xs text-green-400">登录成功</div>
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
          <h3 className="text-sm font-semibold text-foreground">添加 Claude 账号</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={14} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-3">
          <div>
            <label className="block text-xs text-gray-400 mb-1">邮箱</label>
            <input className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-indigo-500"
              value={email} onChange={e => setEmail(e.target.value)} placeholder="user@example.com" required />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">接码 API Token</label>
            <input className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-indigo-500"
              type="password" value={token} onChange={e => setToken(e.target.value)}
              placeholder="171mail / MailCatcher Token" required />
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
              <option value="onet">Onet（Token 接码）</option>
              <option value="gazeta">Gazeta（Token 接码）</option>
            </select>
          </div>
          {status === 'running' && <p className="text-xs text-blue-400">登录中… 请等待（可能需要 1-2 分钟）</p>}
          {status === 'failed' && <p className="text-xs text-red-400 break-all">{detail || '登录失败'}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className="px-3 py-1.5 text-xs text-gray-300 hover:text-foreground">取消</button>
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


function AddCodexAccountModal({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [email, setEmail] = useState('');
  const [token, setToken] = useState('');
  const [password, setPassword] = useState('');
  const [loginMethod, setLoginMethod] = useState<CodexLoginMethod | ''>('');

  const [loginState, setLoginState] = useState<CodexLoginStatus | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const emailDomain = email.trim().toLowerCase().split('@').pop() || '';
  const detectedMethod: CodexLoginMethod = emailDomain === '163.com'
    ? 'mailcatcher'
    : emailDomain === 'mail.com'
      ? 'mailcom'
    : emailDomain === 'onet.pl'
      ? 'onet'
      : emailDomain === 'gazeta.pl'
        ? 'gazeta'
        : '171mail';
  const activeMethod = loginMethod || detectedMethod;
  const usesMailCatcher = activeMethod !== '171mail';
  const loginActive = Boolean(loginState && ACTIVE_CODEX_LOGIN_STATUSES.has(loginState.status));

  useEffect(() => {
    if (!loginActive || !email.trim()) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const state = await api.codexPoolAddStatus(email.trim());
        if (cancelled) return;
        setLoginState(state);
        if (state.status === 'success') {
          onAdded();
          onClose();
          return;
        }
        if (ACTIVE_CODEX_LOGIN_STATUSES.has(state.status)) {
          timer = setTimeout(poll, 2000);
        }
      } catch (e) {
        if (!cancelled) {
          setLoginState((current) => ({
            ...(current || { status: 'running' }),
            detail: e instanceof Error
              ? `状态查询暂时失败，正在重试：${e.message}`
              : '状态查询暂时失败，正在重试',
          }));
          timer = setTimeout(poll, 2000);
        }
      }
    };

    timer = setTimeout(poll, 1000);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [loginActive, email, onAdded, onClose]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim()) return;
    setSubmitting(true);
    try {
      const state = await api.codexPoolAddAccount({
        email: email.trim(),
        token: token.trim() || undefined,
        password: password || undefined,
        login_method: loginMethod || undefined,
      });
      // The child login process has received the credentials at this point;
      // do not retain reusable secrets in React state for the rest of the
      // potentially long-running browser/OTP flow.
      setToken('');
      setPassword('');
      setLoginState({ status: state.status, attempt_id: state.attempt_id });
    } catch (e) {
      setLoginState({
        status: 'failed',
        detail: e instanceof Error ? e.message : '请求失败',
      });
    } finally {
      setSubmitting(false);
    }
  };

  const submitOtp = async (code: string) => {
    if (!loginState?.attempt_id || !loginState.challenge_id) {
      throw new Error('验证码挑战信息缺失，请重新登录');
    }
    await api.codexPoolSubmitOtp(loginState.attempt_id, loginState.challenge_id, code);
    setLoginState({ ...loginState, status: 'verifying_otp' });
  };

  return (
    <div className="absolute inset-0 bg-gray-900/80 z-10 flex items-start justify-center pt-16">
      <div className="bg-gray-800 rounded-lg shadow-xl w-full max-w-xs">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700">
          <h3 className="text-sm font-semibold text-foreground">添加 Codex 账号</h3>
          <button disabled={loginActive} onClick={onClose} className="text-gray-400 hover:text-gray-200 disabled:opacity-40" title={loginActive ? '请等待登录完成' : undefined}><X size={14} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-3">
          <div>
            <label htmlFor="codex-account-email" className="block text-xs text-gray-400 mb-1">OpenAI 邮箱</label>
            <input id="codex-account-email" className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-emerald-500"
              value={email} onChange={e => setEmail(e.target.value)} placeholder="user@example.com" required disabled={loginActive} />
          </div>
          <div>
            <label htmlFor="codex-login-method" className="block text-xs text-gray-400 mb-1">验证码邮箱来源</label>
            <select
              id="codex-login-method"
              className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-emerald-500"
              value={loginMethod}
              onChange={e => setLoginMethod(e.target.value as CodexLoginMethod | '')}
              disabled={loginActive}
            >
              <option value="">自动识别（163/mail.com/Onet/Gazeta → MailCatcher）</option>
              <option value="171mail">171mail（API 接码）</option>
              <option value="mailcatcher">MailCatcher（163 / mail.com / Onet / Gazeta 等）</option>
              <option value="mailcom">mail.com（MailCatcher 接码）</option>
              <option value="onet">Onet（MailCatcher 接码）</option>
              <option value="gazeta">Gazeta（MailCatcher 接码）</option>
            </select>
          </div>
          <div>
            <label htmlFor="codex-mail-credential" className="block text-xs text-gray-400 mb-1">
              {activeMethod === '171mail' ? '171mail API Token（可选）' : 'MailCatcher 查询 Token（可选）'}
            </label>
            <input id="codex-mail-credential" type="password" className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-emerald-500"
              value={token} onChange={e => setToken(e.target.value)} placeholder="仅在使用邮箱验证码时需要" disabled={loginActive} />
            {usesMailCatcher && <p className="mt-1 text-[11px] text-gray-500">填写 MailCatcher 平台签发的查询 Token，不是邮箱密码。</p>}
          </div>
          <div>
            <label htmlFor="codex-openai-password" className="block text-xs text-gray-400 mb-1">OpenAI 密码（可选）</label>
            <input id="codex-openai-password" type="password" className="w-full bg-gray-700 text-foreground text-xs rounded px-2.5 py-1.5 outline-none focus:ring-1 focus:ring-emerald-500"
              value={password} onChange={e => setPassword(e.target.value)} placeholder="有密码时优先使用密码登录" disabled={loginActive} />
            <p className="mt-1 text-[11px] text-gray-500">Token 和密码都可不填：CCM 会尝试切换到 OpenAI 邮箱验证码，并在这里等你输入 6 位码；若账号只提供密码登录，会提示补密码重试。只有实际填写的长期凭据才会以 0600 权限保存在 CCM 服务器；验证码不会保存。</p>
          </div>
          {loginState?.status === 'running' && <p className="text-xs text-blue-400">登录中… 请等待（可能需要 1-3 分钟）</p>}
          {loginState?.status === 'awaiting_otp' && <CodexOtpPrompt state={loginState} onSubmit={submitOtp} />}
          {loginState?.status === 'verifying_otp' && <p className="text-xs text-blue-400">验证码已提交，正在继续登录…</p>}
          {loginState?.status === 'finalizing' && <p className="text-xs text-blue-400">登录已完成，正在安全提交登录结果…</p>}
          {loginActive && loginState?.detail && <p className="text-xs text-amber-400 break-all">{loginState.detail}</p>}
          {(loginState?.status === 'failed' || loginState?.status === 'expired') && <p className="text-xs text-red-400 break-all">{loginState.detail || '登录失败'}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" disabled={loginActive} onClick={onClose} className="px-3 py-1.5 text-xs text-gray-300 hover:text-white disabled:opacity-40">取消</button>
            <button type="submit" disabled={submitting || loginActive || !email.trim()}
              className="px-3 py-1.5 text-xs bg-emerald-600 text-white rounded hover:bg-emerald-500 disabled:opacity-50">
              {loginActive ? '登录中…' : '添加'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function CcSettingsModal({ onClose }: { onClose: () => void }) {
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  useEffect(() => {
    api.getCcSettings()
      .then((r) => setText(JSON.stringify(r.settings, null, 2)))
      .catch((e) => setText(`// 加载失败: ${e instanceof Error ? e.message : e}`))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setResult(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(text);
    } catch {
      setResult({ ok: false, message: 'JSON 格式错误' });
      return;
    }
    setSaving(true);
    try {
      const r = await api.putCcSettings(parsed);
      setResult({ ok: true, message: `已同步到 ${r.synced} 个账号` });
    } catch (e) {
      setResult({ ok: false, message: e instanceof Error ? e.message : '保存失败' });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="absolute inset-0 bg-gray-900/80 z-10 flex items-start justify-center pt-12">
      <div className="bg-gray-800 rounded-lg shadow-xl w-full max-w-xs flex flex-col max-h-[80%]">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700">
          <h3 className="text-sm font-semibold text-foreground">CC Settings 模板</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-foreground"><X size={14} /></button>
        </div>
        <div className="flex-1 overflow-hidden p-3 flex flex-col gap-2">
          <p className="text-[10px] text-gray-500">编辑后保存将同步到所有 Pool 账号的 settings.json（hooks 字段会保留）</p>
          {loading ? (
            <div className="text-xs text-gray-500 py-4 text-center">加载中…</div>
          ) : (
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              className="flex-1 min-h-[200px] bg-gray-900 text-gray-300 text-[11px] font-mono rounded border border-gray-700 p-2 resize-none focus:outline-none focus:border-indigo-500"
              spellCheck={false}
            />
          )}
          {result && (
            <div className={`text-xs ${result.ok ? 'text-green-400' : 'text-red-400'}`}>{result.message}</div>
          )}
        </div>
        <div className="flex justify-end gap-2 px-4 py-3 border-t border-gray-700">
          <button onClick={onClose} className="px-3 py-1.5 text-xs rounded bg-gray-700 text-gray-300 hover:bg-gray-600">关闭</button>
          <button
            onClick={handleSave}
            disabled={saving || loading}
            className="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-500 disabled:opacity-50"
          >
            {saving ? '同步中…' : '保存并同步'}
          </button>
        </div>
      </div>
    </div>
  );
}

type PoolTab = 'claude' | 'codex';

export function PoolDrawer() {
  const [claudeEnabled, setClaudeEnabled] = useState(false);
  const [codexEnabled, setCodexEnabled] = useState(false);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<PoolTab>('claude');

  // Claude pool state
  const [claudeStatus, setClaudeStatus] = useState<PoolUsageStatus | null>(null);
  const [claudeLoading, setClaudeLoading] = useState(false);
  const [claudeError, setClaudeError] = useState<string | null>(null);

  // Codex pool state
  const [codexStatus, setCodexStatus] = useState<CodexPoolUsageStatus | null>(null);
  const [codexLoading, setCodexLoading] = useState(false);
  const [codexError, setCodexError] = useState<string | null>(null);
  const codexUsageRequestSeq = useRef(0);

  useEffect(() => {
    api.getPoolStatus()
      .then((s) => setClaudeEnabled(s.enabled))
      .catch(() => setClaudeEnabled(false));
    api.getCodexPoolStatus()
      .then(() => { setCodexEnabled(true); })
      .catch(() => setCodexEnabled(false));
  }, []);

  const loadClaudeUsage = useCallback(async (force?: boolean) => {
    setClaudeLoading(true);
    setClaudeError(null);
    try {
      setClaudeStatus(await api.getPoolUsage(force));
    } catch (e) {
      setClaudeError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setClaudeLoading(false);
    }
  }, []);

  const loadCodexUsage = useCallback(async (force = true) => {
    const requestSeq = ++codexUsageRequestSeq.current;
    setCodexLoading(true);
    setCodexError(null);
    if (force) {
      // A previous account snapshot is not evidence of the current quota.
      // Hide it while the live-only request is pending or if that request fails.
      setCodexStatus(null);
    }
    try {
      const status = await api.getCodexPoolUsage(force);
      if (requestSeq === codexUsageRequestSeq.current) {
        setCodexStatus(status);
      }
    } catch (e) {
      if (requestSeq === codexUsageRequestSeq.current) {
        setCodexError(e instanceof Error ? e.message : '加载失败');
      }
    } finally {
      if (requestSeq === codexUsageRequestSeq.current) {
        setCodexLoading(false);
      }
    }
  }, []);

  useEffect(() => () => {
    // Invalidate any request still in flight when the drawer component leaves.
    codexUsageRequestSeq.current += 1;
  }, []);

  useEffect(() => {
    if (open) {
      if (tab === 'claude') loadClaudeUsage();
      else loadCodexUsage(true);
    }
  }, [open, tab, loadClaudeUsage, loadCodexUsage]);

  // Claude handlers
  const handleClaudeClearCooldown = useCallback(async (accountId: string) => {
    try { await api.clearPoolCooldown(accountId); await loadClaudeUsage(); } catch { /* Keep current drawer state on request failure. */ }
  }, [loadClaudeUsage]);

  const handleClaudeSetPreferred = useCallback(async (accountId: string | null) => {
    try { await api.setPoolPreferred(accountId); await loadClaudeUsage(); } catch { /* Keep current drawer state on request failure. */ }
  }, [loadClaudeUsage]);

  const [relogin, setRelogin] = useState<Record<string, { status: string; message?: string }>>({});
  const [showAdd, setShowAdd] = useState(false);
  const [showCodexAdd, setShowCodexAdd] = useState(false);
  const [showCcSettings, setShowCcSettings] = useState(false);

  const handleClaudeRelogin = useCallback(async (accountId: string) => {
    setRelogin((m) => ({ ...m, [accountId]: { status: 'running' } }));
    try {
      const res = await api.poolRelogin(accountId);
      if (res.status === 'success') {
        setRelogin((m) => ({ ...m, [accountId]: { status: 'success' } }));
        await loadClaudeUsage();
        return;
      }
      const poll = async () => {
        const s = await api.poolReloginStatus(accountId);
        if (s.status === 'running') { setTimeout(poll, 5000); return; }
        setRelogin((m) => ({ ...m, [accountId]: {
          status: s.status,
          message: s.status === 'failed' ? `登录失败：${(s.detail || '').slice(-300)}` : undefined,
        } }));
        if (s.status === 'success') await loadClaudeUsage();
      };
      setTimeout(poll, 5000);
    } catch (e) {
      setRelogin((m) => ({ ...m, [accountId]: {
        status: 'failed',
        message: e instanceof Error ? e.message : '重新登录失败',
      } }));
    }
  }, [loadClaudeUsage]);

  // Codex handlers
  const handleCodexClearCooldown = useCallback(async (accountId: string) => {
    try { await api.clearCodexPoolCooldown(accountId); await loadCodexUsage(); } catch { /* Keep current drawer state on request failure. */ }
  }, [loadCodexUsage]);

  const handleCodexSetPreferred = useCallback(async (accountId: string | null) => {
    try { await api.setCodexPoolPreferred(accountId); await loadCodexUsage(); } catch { /* Keep current drawer state on request failure. */ }
  }, [loadCodexUsage]);

  const [codexRelogin, setCodexRelogin] = useState<Record<string, CodexLoginStatus>>({});
  const codexReloginAlive = useRef(true);
  const codexReloginTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  useEffect(() => {
    // React StrictMode runs an extra setup/cleanup cycle in development, so
    // restore the live flag on every setup rather than only initializing it.
    codexReloginAlive.current = true;
    const timers = codexReloginTimers.current;
    return () => {
      codexReloginAlive.current = false;
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, []);

  const handleCodexRelogin = useCallback(async (accountId: string) => {
    setCodexRelogin((m) => ({ ...m, [accountId]: { status: 'running' } }));
    try {
      const started = await api.codexPoolRelogin(accountId);
      setCodexRelogin((m) => ({ ...m, [accountId]: {
        status: started.status,
        attempt_id: started.attempt_id,
      } }));

      const schedulePoll = (delay: number) => {
        const previous = codexReloginTimers.current.get(accountId);
        if (previous) clearTimeout(previous);
        const timer = setTimeout(poll, delay);
        codexReloginTimers.current.set(accountId, timer);
      };
      const poll = async () => {
        if (!codexReloginAlive.current) return;
        try {
          const s = await api.codexPoolReloginStatus(accountId);
          if (!codexReloginAlive.current) return;
          setCodexRelogin((m) => ({ ...m, [accountId]: s }));
          if (ACTIVE_CODEX_LOGIN_STATUSES.has(s.status)) {
            schedulePoll(2000);
            return;
          }
          codexReloginTimers.current.delete(accountId);
          if (s.status === 'success') await loadCodexUsage();
        } catch (e) {
          if (!codexReloginAlive.current) return;
          setCodexRelogin((current) => ({
            ...current,
            [accountId]: {
              ...(current[accountId] || { status: 'running' }),
              detail: e instanceof Error
                ? `状态查询暂时失败，正在重试：${e.message}`
                : '状态查询暂时失败，正在重试',
            },
          }));
          schedulePoll(2000);
        }
      };
      schedulePoll(1000);
    } catch (e) {
      setCodexRelogin((m) => ({ ...m, [accountId]: {
        status: 'failed',
        detail: e instanceof Error ? e.message : '重新登录失败',
      } }));
    }
  }, [loadCodexUsage]);

  const handleCodexSubmitOtp = useCallback(async (
    accountId: string,
    state: CodexLoginStatus,
    code: string,
  ) => {
    if (!state.attempt_id || !state.challenge_id) {
      throw new Error('验证码挑战信息缺失，请重新登录');
    }
    await api.codexPoolSubmitOtp(state.attempt_id, state.challenge_id, code);
    setCodexRelogin((current) => ({
      ...current,
      [accountId]: { ...state, status: 'verifying_otp' },
    }));
  }, []);

  if (!claudeEnabled && !codexEnabled) return null;

  const hasBothPools = claudeEnabled && codexEnabled;
  const loading = tab === 'claude' ? claudeLoading : codexLoading;

  return (
    <>
      <button
        onClick={() => {
          if (!claudeEnabled && codexEnabled) setTab('codex');
          setOpen(true);
        }}
        className="flex items-center gap-1 px-2 py-1 rounded bg-gray-800 border border-gray-700 hover:border-indigo-500 transition-colors"
        title="账号池额度"
      >
        <Users size={13} className="text-indigo-400" />
        <span className="text-xs font-semibold text-indigo-300">Pro</span>
      </button>
      {open && createPortal(
        <div className="fixed inset-0 z-[70]">
          <div className="absolute inset-0 bg-black/50" onClick={() => { if (!showCodexAdd) setOpen(false); }} />
          <div className="absolute right-0 top-0 h-full w-full max-w-sm bg-gray-900 border-l border-gray-700 shadow-xl flex flex-col pt-[env(safe-area-inset-top)]">
            <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-700">
              <Users size={16} className="text-indigo-400" />
              <h2 className="text-sm font-semibold text-foreground">
                {tab === 'claude' ? 'Claude Pool' : 'Codex Pool'}
              </h2>
              {tab === 'claude' && claudeStatus && (
                <span className="text-xs text-gray-500">
                  {claudeStatus.available}/{claudeStatus.total} 可用
                </span>
              )}
              {tab === 'codex' && codexStatus && (
                <span className="text-xs text-gray-500">
                  {codexStatus.available}/{codexStatus.total} 可用
                </span>
              )}
              <div className="ml-auto flex items-center gap-1">
                {tab === 'claude' && (
                  <button
                    onClick={() => setShowCcSettings(true)}
                    className="p-1.5 rounded text-gray-400 hover:text-foreground hover:bg-gray-800"
                    title="CC Settings 模板"
                  >
                    <Settings size={14} />
                  </button>
                )}
                <button
                  onClick={() => tab === 'claude' ? setShowAdd(true) : setShowCodexAdd(true)}
                  className="p-1.5 rounded text-gray-400 hover:text-foreground hover:bg-gray-800"
                  title="添加账号"
                >
                  <Plus size={14} />
                </button>
                <button
                  onClick={() => tab === 'claude' ? loadClaudeUsage(true) : loadCodexUsage(true)}
                  disabled={loading}
                  className="p-1.5 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 disabled:opacity-50"
                  title="刷新"
                >
                  <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
                </button>
                <button
                  onClick={() => { if (!showCodexAdd) setOpen(false); }}
                  disabled={showCodexAdd}
                  className="p-1.5 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 disabled:opacity-40"
                  title={showCodexAdd ? '请先关闭账号登录窗口' : undefined}
                >
                  <X size={14} />
                </button>
              </div>
            </div>

            {/* Tab bar */}
            {hasBothPools && (
              <div className="flex border-b border-gray-700">
                <button
                  onClick={() => setTab('claude')}
                  className={`flex-1 py-2 text-xs font-medium text-center transition-colors ${
                    tab === 'claude'
                      ? 'text-indigo-300 border-b-2 border-indigo-500 bg-gray-800/50'
                      : 'text-gray-500 hover:text-gray-300'
                  }`}
                >
                  Claude
                </button>
                <button
                  onClick={() => setTab('codex')}
                  className={`flex-1 py-2 text-xs font-medium text-center transition-colors ${
                    tab === 'codex'
                      ? 'text-emerald-300 border-b-2 border-emerald-500 bg-gray-800/50'
                      : 'text-gray-500 hover:text-gray-300'
                  }`}
                >
                  Codex
                </button>
              </div>
            )}

            <div className="flex-1 overflow-y-auto p-3 space-y-2 relative">
              {showAdd && <AddAccountModal onClose={() => setShowAdd(false)} onAdded={loadClaudeUsage} />}
              {showCodexAdd && <AddCodexAccountModal onClose={() => setShowCodexAdd(false)} onAdded={loadCodexUsage} />}
              {showCcSettings && <CcSettingsModal onClose={() => setShowCcSettings(false)} />}

              {/* Claude tab */}
              {tab === 'claude' && (
                <>
                  {claudeError && <div className="text-xs text-red-400">{claudeError}</div>}
                  {claudeLoading && !claudeStatus && <div className="text-xs text-gray-500">加载中…</div>}
                  {claudeStatus?.accounts.map((a) => (
                    <AccountCard
                      key={a.id}
                      account={a}
                      preferred={claudeStatus?.preferred ?? null}
                      lastSelected={claudeStatus?.last_selected ?? null}
                      onClearCooldown={handleClaudeClearCooldown}
                      onSetPreferred={handleClaudeSetPreferred}
                      onRelogin={handleClaudeRelogin}
                      onRetryUsage={() => loadClaudeUsage(true)}
                      onDelete={async (id) => {
                        if (!window.confirm(`从 Claude 号池中删除 ${id}？`)) return;
                        try { await api.poolDeleteAccount(id); await loadClaudeUsage(); } catch (e) { window.alert(String(e)); }
                      }}
                      reloginState={relogin[a.id]}
                    />
                  ))}
                </>
              )}

              {/* Codex tab */}
              {tab === 'codex' && (
                <>
                  {codexError && <div className="text-xs text-red-400">{codexError}</div>}
                  {codexLoading && !codexStatus && <div className="text-xs text-gray-500">加载中…</div>}
                  {codexStatus?.accounts.map((a) => (
                    <CodexAccountCard
                      key={`${a.id}:${a.codex_home}`}
                      account={a}
                      preferred={codexStatus.preferred ?? null}
                      onClearCooldown={handleCodexClearCooldown}
                      onSetPreferred={handleCodexSetPreferred}
                      onRelogin={handleCodexRelogin}
                      onSubmitOtp={(state, code) => handleCodexSubmitOtp(a.id, state, code)}
                      onDelete={async (id) => {
                        if (!window.confirm(`从 Codex 号池中删除 ${id}？将清除 OAuth、邮箱 Token、OpenAI 密码以及该账号的日志、历史和配置；仅保留原生会话文件用于任务上下文迁移。`)) return;
                        try { await api.codexPoolDeleteAccount(id); await loadCodexUsage(); } catch (e) { window.alert(String(e)); }
                      }}
                      onRetryUsage={() => loadCodexUsage(true)}
                      reloginState={codexRelogin[a.id]}
                    />
                  ))}
                </>
              )}
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
