import { useState, useCallback, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { ArrowUpCircle, RefreshCw, X } from '../icons';
import { api } from '../../api/client';
import { useWebSocket } from '../../hooks/useWebSocket';

interface StepInfo {
  name: string;
  status: string;
  duration_ms?: number | null;
  message?: string | null;
  result?: Record<string, unknown> | null;
}

interface UpdateStatusData {
  update_id?: string;
  status: string;
  steps?: StepInfo[];
  old_commit?: string;
  new_commit?: string;
  error?: string;
  current_step?: number;
  total_steps?: number;
}

interface ActiveTaskSummary {
  id: number;
  title: string;
  status: string;
}

type Phase = 'idle' | 'checking' | 'confirming' | 'running' | 'restarting' | 'completed' | 'failed';

const STEP_LABELS: Record<string, string> = {
  git_pull: '拉取代码',
  detect_changes: '检测变更',
  backup_database: '备份数据库',
  uv_sync: 'Python 依赖',
  refresh_pty: 'PTY 依赖',
  npm_install: '前端依赖',
  frontend_build: '构建前端',
  stop_service: '停止服务',
  alembic_upgrade: '数据库迁移',
  start_service: '启动服务',
};

const STATUS_ICON: Record<string, string> = {
  pending: '○',
  running: '⏳',
  completed: '✅',
  failed: '❌',
  skipped: '⏭',
};

const INITIAL_UPDATE_CHECK_DELAY_MS = 1_000;
const UPDATE_CHECK_INTERVAL_MS = 60 * 60_000;

function reminderFingerprint(result: Record<string, unknown>): string {
  if (result.has_updates) return `update:${String(result.latest_commit || '')}`;
  if (result.needs_restart) return `restart:${String(result.current_commit || '')}`;
  return '';
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function UpdateButton() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [steps, setSteps] = useState<StepInfo[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [dryRunResult, setDryRunResult] = useState<Record<string, unknown> | null>(null);
  const [skipFrontend, setSkipFrontend] = useState(false);
  const [branch, setBranch] = useState('');
  const [error, setError] = useState('');
  const [oldCommit, setOldCommit] = useState('');
  const [newCommit, setNewCommit] = useState('');
  const [reconnectCount, setReconnectCount] = useState(0);
  const [reconnectSlow, setReconnectSlow] = useState(false);
  const [autoPrompt, setAutoPrompt] = useState(false);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const [showUpdateNotice, setShowUpdateNotice] = useState(false);
  const reconnectTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const logsEndRef = useRef<HTMLDivElement>(null);
  const autoCheckInFlightRef = useRef(false);
  const remindedFingerprintsRef = useRef(new Set<string>());
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  const onWsMessage = useCallback((msg: Record<string, unknown>) => {
    const data = msg.data as Record<string, unknown> | undefined;
    if (!data || (msg.channel !== 'system_update')) return;

    const event = data.event as string;

    if (event === 'step_update') {
      setSteps(prev => {
        const stepName = data.step as string;
        const exists = prev.find(s => s.name === stepName);
        if (exists) {
          return prev.map(s =>
            s.name === stepName
              ? { ...s, status: data.status as string, duration_ms: data.duration_ms as number | undefined, message: data.message as string | undefined, result: data.result as Record<string, unknown> | undefined }
              : s
          );
        }
        return prev;
      });
    }

    if (event === 'log_line') {
      const log = data.log as string;
      if (log) setLogs(prev => [...prev.slice(-200), log]);
    }

    if (event === 'update_complete') {
      setUpdateAvailable(false);
      setShowUpdateNotice(false);
      setPhase('completed');
    }

    if (event === 'update_failed') {
      setError(data.message as string || '更新失败');
      setPhase('failed');
    }

    if (event === 'restarting') {
      setPhase('restarting');
      startReconnectPolling();
    }
  }, []);

  useWebSocket(['system_update'], onWsMessage);

  useEffect(() => {
    if (logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs]);

  useEffect(() => {
    return () => {
      if (reconnectTimer.current) clearInterval(reconnectTimer.current);
    };
  }, []);

  useEffect(() => {
    const onVisible = async () => {
      if (document.visibilityState !== 'visible') return;
      const p = phaseRef.current;
      if (p !== 'running' && p !== 'restarting') return;
      try {
        const status = await api.getUpdateStatus() as UpdateStatusData;
        if (status.old_commit) setOldCommit(status.old_commit);
        if (status.new_commit) setNewCommit(status.new_commit);
        if (status.steps) setSteps(status.steps);
        if (status.status === 'completed') {
          setPhase('completed');
        } else if (status.status === 'rolled_back' || status.status === 'failed') {
          setError(status.error || '更新失败');
          setPhase('failed');
        }
      } catch {
        // Server may still be restarting — keep current phase
      }
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, []);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = (delay: number) => {
      if (!disposed) timer = setTimeout(runCheck, delay);
    };

    const runCheck = async () => {
      if (disposed) return;
      if (
        document.visibilityState !== 'visible'
        || phaseRef.current !== 'idle'
        || autoCheckInFlightRef.current
      ) {
        schedule(UPDATE_CHECK_INTERVAL_MS);
        return;
      }

      autoCheckInFlightRef.current = true;
      try {
        const status = await api.getUpdateStatus() as UpdateStatusData | undefined;
        if (status?.status === 'running' || status?.status === 'restarting') return;
        if (disposed || phaseRef.current !== 'idle') return;

        // Background checks are deliberately dry-run only: they fetch refs and
        // notify the user, but never pull code or restart the service.
        const result = await api.startUpdate({ dry_run: true }) as Record<string, unknown> | undefined;
        if (disposed || phaseRef.current !== 'idle' || !result) return;
        // A remote fetch error is normally silent, but it must not hide a
        // locally detected manual pull that still needs a service restart.
        if (!result.has_updates && !result.needs_restart) {
          if (!result.error) {
            setUpdateAvailable(false);
            setShowUpdateNotice(false);
          }
          return;
        }

        setUpdateAvailable(true);
        const fingerprint = reminderFingerprint(result);
        if (fingerprint && !remindedFingerprintsRef.current.has(fingerprint)) {
          remindedFingerprintsRef.current.add(fingerprint);
          setDryRunResult(result);
          setAutoPrompt(true);
          setShowUpdateNotice(true);
        }
      } catch {
        // Automatic checks stay silent on transient network/git failures.
      } finally {
        autoCheckInFlightRef.current = false;
        schedule(UPDATE_CHECK_INTERVAL_MS);
      }
    };

    schedule(INITIAL_UPDATE_CHECK_DELAY_MS);
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const startReconnectPolling = () => {
    if (reconnectTimer.current) clearInterval(reconnectTimer.current);
    let attempts = 0;
    let sawDown = false;
    setReconnectSlow(false);

    const poll = async () => {
      attempts++;
      setReconnectCount(attempts);
      try {
        await api.health();
        if (!sawDown) {
          // Server hasn't gone down yet — the restart command fires with
          // a 2s delay so early polls can hit the OLD still-alive server.
          // Wait until it actually dies before accepting a success.
          return;
        }
        if (reconnectTimer.current) clearInterval(reconnectTimer.current);
        reconnectTimer.current = null;
        setReconnectSlow(false);
        try {
          const status = await api.getUpdateStatus() as UpdateStatusData;
          if (status.old_commit) setOldCommit(status.old_commit);
          if (status.new_commit) setNewCommit(status.new_commit);
          if (status.steps) setSteps(status.steps);
          if (status.status === 'rolled_back') {
            setError(status.error || '迁移失败，已自动回滚');
            setPhase('failed');
          } else {
            setPhase('completed');
            setTimeout(() => window.location.reload(), 1500);
          }
        } catch {
          setPhase('completed');
          setTimeout(() => window.location.reload(), 1500);
        }
      } catch {
        sawDown = true;
        // After 120s (60 fast polls), switch to slow polling instead of giving up
        if (attempts === 60) {
          setReconnectSlow(true);
          if (reconnectTimer.current) clearInterval(reconnectTimer.current);
          reconnectTimer.current = setInterval(poll, 5000);
        }
      }
    };

    reconnectTimer.current = setInterval(poll, 2000);
  };

  const handleCheck = async () => {
    setPhase('checking');
    setAutoPrompt(false);
    setShowUpdateNotice(false);
    setError('');
    try {
      const result = await api.startUpdate({ dry_run: true, force: true, branch: branch || undefined }) as Record<string, unknown>;
      if (result.error && !result.needs_restart) throw new Error(String(result.error));
      const fingerprint = reminderFingerprint(result);
      if (fingerprint) remindedFingerprintsRef.current.add(fingerprint);
      setDryRunResult(result);
      setUpdateAvailable(Boolean(result.has_updates || result.needs_restart));
      setPhase('confirming');
    } catch (e: unknown) {
      setError(errorMessage(e, '检查更新失败'));
      setPhase('failed');
    }
  };

  const handleConfirm = async () => {
    setPhase('running');
    setShowUpdateNotice(false);
    setLogs([]);
    setError('');

    const defaultSteps: StepInfo[] = [
      'git_pull', 'detect_changes', 'backup_database', 'uv_sync',
      'refresh_pty', 'npm_install', 'frontend_build',
      'stop_service', 'alembic_upgrade', 'start_service',
    ].map(name => ({ name, status: 'pending' }));
    setSteps(defaultSteps);

    try {
      const result = await api.startUpdate({ skip_frontend_build: skipFrontend, branch: branch || undefined });
      if (result.update_id) {
        setOldCommit(result.old_commit || '');
      }
    } catch (e: unknown) {
      setError(errorMessage(e, '启动更新失败'));
      setPhase('failed');
    }
  };

  const handleRollback = async () => {
    if (!confirm('确定要回滚到上一个版本吗？')) return;
    try {
      await api.rollbackUpdate();
      startReconnectPolling();
      setPhase('restarting');
    } catch (e: unknown) {
      setError(errorMessage(e, '回滚失败'));
    }
  };

  const handleClose = () => {
    setPhase('idle');
    setSteps([]);
    setLogs([]);
    setError('');
    setDryRunResult(null);
    setSkipFrontend(false);
    setBranch('');
    setOldCommit('');
    setNewCommit('');
    setReconnectCount(0);
    setReconnectSlow(false);
    setAutoPrompt(false);
    setShowUpdateNotice(false);
  };

  const handleOpenUpdateNotice = () => {
    setShowUpdateNotice(false);
    setAutoPrompt(true);
    setPhase('confirming');
  };

  const isModalOpen = phase !== 'idle';
  const activeTasks = (dryRunResult?.active_tasks || []) as ActiveTaskSummary[];
  const activeTaskCount = Number(dryRunResult?.active_task_count || activeTasks.length || 0);
  const updateBlocked = Boolean(dryRunResult?.update_blocked || activeTaskCount > 0);

  return (
    <>
      {showUpdateNotice && dryRunResult && createPortal(
        <div
          className="pointer-events-none fixed inset-x-0 top-3 z-[60] flex justify-center px-4"
          data-testid="update-available-notice"
          aria-live="polite"
        >
          <div
            className="pointer-events-auto flex w-full max-w-xl items-center gap-3 rounded-lg border border-amber-500/40 bg-gray-900/95 px-4 py-3 shadow-xl backdrop-blur"
            role="status"
          >
            <ArrowUpCircle size={18} className="shrink-0 text-amber-400" />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-foreground">
                {(dryRunResult.needs_restart as boolean) ? '检测到待完成的本地更新' : '发现可用更新'}
              </p>
              <p className="mt-0.5 truncate text-xs text-gray-400">
                {(dryRunResult.needs_restart as boolean)
                  ? '当前服务仍在运行旧版本，可稍后安全完成部署。'
                  : `有 ${Number(dryRunResult.commits_behind || 0)} 个新提交可用。`}
              </p>
            </div>
            <button
              type="button"
              onClick={handleOpenUpdateNotice}
              className="shrink-0 rounded bg-amber-500/15 px-2.5 py-1.5 text-xs font-medium text-amber-300 hover:bg-amber-500/25"
            >
              查看详情
            </button>
            <button
              type="button"
              onClick={() => setShowUpdateNotice(false)}
              className="shrink-0 rounded p-1 text-gray-500 hover:bg-gray-800 hover:text-gray-300"
              aria-label="关闭更新提醒"
            >
              <X size={15} />
            </button>
          </div>
        </div>,
        document.body,
      )}

      <button
        onClick={handleCheck}
        className="relative p-2 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 transition-colors"
        title="更新并重启"
      >
        <ArrowUpCircle size={18} />
        {updateAvailable && (
          <span
            data-testid="update-available-dot"
            className="absolute right-1 top-1 h-2 w-2 rounded-full bg-amber-400 ring-2 ring-gray-900"
          />
        )}
      </button>

      {isModalOpen && createPortal(
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60">
          <div className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl w-full max-w-lg mx-4 max-h-[85vh] flex flex-col">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700">
              <h3 className="text-sm font-semibold text-foreground">
                {phase === 'checking' && '检查更新...'}
                {phase === 'confirming' && (autoPrompt
                  ? ((dryRunResult?.needs_restart as boolean) ? '检测到待完成的更新' : '发现可用更新')
                  : '确认更新')}
                {phase === 'running' && '更新中...'}
                {phase === 'restarting' && '重启中...'}
                {phase === 'completed' && '更新完成'}
                {phase === 'failed' && '更新失败'}
              </h3>
              {(phase === 'completed' || phase === 'failed' || phase === 'confirming') && (
                <button onClick={handleClose} className="text-gray-400 hover:text-foreground text-lg">✕</button>
              )}
            </div>

            {/* Body */}
            <div className="flex-1 overflow-y-auto p-4 space-y-3">
              {/* Checking phase */}
              {phase === 'checking' && (
                <div className="flex items-center gap-2 text-gray-400 text-sm">
                  <RefreshCw size={14} className="animate-spin" />
                  正在检查是否有新版本...
                </div>
              )}

              {/* Confirm phase */}
              {phase === 'confirming' && dryRunResult && (
                <div className="space-y-3">
                  {/* Branch selector */}
                  <div className="flex items-center gap-2">
                    <label className="text-xs text-gray-400 shrink-0">分支:</label>
                    <input
                      value={branch}
                      onChange={e => setBranch(e.target.value)}
                      placeholder="main"
                      className="flex-1 bg-gray-800 text-foreground text-xs rounded px-2 py-1 border border-gray-700 focus:outline-none focus:border-indigo-500"
                    />
                    <button
                      onClick={handleCheck}
                      className="px-2 py-1 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700 shrink-0"
                    >
                      重新检查
                    </button>
                  </div>
                  {branch && branch !== 'main' && (
                    <p className="text-xs text-amber-400">⚠️ 将从分支 <span className="font-mono">{branch}</span> 更新（非 main）</p>
                  )}

                  {updateBlocked && (
                    <div className="rounded border border-amber-700/60 bg-amber-950/30 p-3 text-xs text-amber-200" role="alert">
                      <p className="font-medium">当前有 {activeTaskCount} 个任务正在执行，暂不能更新或重启。</p>
                      <p className="mt-1 text-amber-300/80">请等待任务完成后点击“重新检查”。系统不会中断这些任务。</p>
                      {activeTasks.length > 0 && (
                        <ul className="mt-2 space-y-1 text-amber-300/80">
                          {activeTasks.slice(0, 5).map(task => (
                            <li key={task.id}>#{task.id} {task.title || '未命名任务'}（{task.status}）</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}

                  {!(dryRunResult.has_updates as boolean) && !(dryRunResult.needs_restart as boolean) ? (
                    <p className="text-sm text-gray-300">已是最新版本，无需更新。</p>
                  ) : !(dryRunResult.has_updates as boolean) && (dryRunResult.needs_restart as boolean) ? (
                    <div className="space-y-1 text-sm text-yellow-300">
                      <p>检测到磁盘代码已更新，但服务仍在运行旧版本。</p>
                      <p className="text-xs text-yellow-400/80">继续后会补齐依赖、迁移和前端构建，再安全重启服务。</p>
                      {Boolean(dryRunResult.error) && (
                        <p className="text-xs text-yellow-400/80">远端更新检查失败，但不影响完成本地代码的部署和重启。</p>
                      )}
                    </div>
                  ) : (
                    <>
                      <div className="text-sm text-gray-300 space-y-1">
                        <p>发现 <span className="text-indigo-400 font-medium">{dryRunResult.commits_behind as number}</span> 个新提交</p>
                        <p className="text-xs text-gray-500">{dryRunResult.current_commit as string} → {dryRunResult.latest_commit as string}</p>
                        {Boolean(dryRunResult.remote) && (
                          <p className="text-xs text-gray-600">来源：{dryRunResult.remote as string}/{(dryRunResult.branch as string) || 'main'}</p>
                        )}
                      </div>

                      {(dryRunResult.needs_restart as boolean) && (
                        <p className="text-xs text-yellow-400">⚠️ 磁盘上还有尚未加载的手动更新，本次会一并完成部署。</p>
                      )}

                      {(dryRunResult.commit_messages as string[] || []).length > 0 && (
                        <div className="bg-gray-800 rounded p-2 max-h-32 overflow-y-auto">
                          {(dryRunResult.commit_messages as string[]).map((msg, i) => (
                            <p key={i} className="text-xs text-gray-400 py-0.5">{msg}</p>
                          ))}
                        </div>
                      )}

                      <div className="flex flex-wrap gap-2 text-xs">
                        {(dryRunResult.has_new_migrations as boolean) && (
                          <span className="px-2 py-0.5 rounded bg-yellow-900/50 text-yellow-300">
                            {dryRunResult.migration_count as number} 个迁移
                          </span>
                        )}
                        {(dryRunResult.has_frontend_changes as boolean) && (
                          <span className="px-2 py-0.5 rounded bg-blue-900/50 text-blue-300">前端变更</span>
                        )}
                        {(dryRunResult.has_package_changes as boolean) && (
                          <span className="px-2 py-0.5 rounded bg-purple-900/50 text-purple-300">依赖变更</span>
                        )}
                      </div>

                      <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={skipFrontend}
                          onChange={e => setSkipFrontend(e.target.checked)}
                          className="rounded border-gray-600"
                        />
                        跳过前端构建（仅后端更新）
                      </label>

                      {(dryRunResult.has_new_migrations as boolean) && (
                        <p className="text-xs text-yellow-400">
                          ⚠️ 包含数据库迁移，更新时会短暂停服。数据库将自动备份。
                        </p>
                      )}
                    </>
                  )}
                </div>
              )}

              {/* Running / completed / failed — show steps */}
              {(phase === 'running' || phase === 'completed' || phase === 'failed') && steps.length > 0 && (
                <div className="space-y-1">
                  {steps.map(step => (
                    <div key={step.name} className="flex items-center gap-2 text-xs">
                      <span className="w-4 text-center">{STATUS_ICON[step.status] || '○'}</span>
                      <span className={`flex-1 ${step.status === 'running' ? 'text-foreground' : step.status === 'completed' ? 'text-gray-400' : step.status === 'failed' ? 'text-red-400' : 'text-gray-600'}`}>
                        {STEP_LABELS[step.name] || step.name}
                        {step.message && step.status !== 'running' && (
                          <span className="text-gray-600 ml-1">— {step.message}</span>
                        )}
                      </span>
                      {step.duration_ms != null && (
                        <span className="text-gray-600 text-[10px]">{(step.duration_ms / 1000).toFixed(1)}s</span>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {/* Restarting phase */}
              {phase === 'restarting' && (
                <div className="flex flex-col items-center gap-3 py-6">
                  <RefreshCw size={24} className="animate-spin text-indigo-400" />
                  <p className="text-sm text-gray-300">服务正在重启...</p>
                  <p className="text-xs text-gray-500">
                    {reconnectSlow
                      ? `等待服务恢复（每 5 秒检测，已等待 ${Math.round((60 * 2 + (reconnectCount - 60) * 5))}秒）`
                      : `每 2 秒检测一次（${reconnectCount}/60）`
                    }
                  </p>
                  {reconnectSlow && (
                    <>
                      <p className="text-xs text-yellow-400">重启时间超过预期，可能正在执行数据库迁移...</p>
                      <button
                        onClick={() => window.location.reload()}
                        className="px-3 py-1.5 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700"
                      >
                        手动刷新页面
                      </button>
                    </>
                  )}
                </div>
              )}

              {/* Completed */}
              {phase === 'completed' && (
                <div className="bg-green-900/20 border border-green-800/50 rounded p-3 text-sm text-green-300">
                  ✅ 更新完成
                  {oldCommit && newCommit && oldCommit !== newCommit && (
                    <span className="text-xs text-gray-500 ml-2">{oldCommit.slice(0, 7)} → {newCommit.slice(0, 7)}</span>
                  )}
                </div>
              )}

              {/* Failed */}
              {phase === 'failed' && error && (
                <div className="bg-red-900/20 border border-red-800/50 rounded p-3 text-sm text-red-300">
                  {error}
                </div>
              )}

              {/* Logs */}
              {logs.length > 0 && (phase === 'running' || phase === 'failed') && (
                <div className="bg-gray-950 rounded border border-gray-800 p-2 max-h-40 overflow-y-auto font-mono text-[11px] text-gray-500">
                  {logs.map((line, i) => (
                    <div key={i}>{line}</div>
                  ))}
                  <div ref={logsEndRef} />
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex justify-end gap-2 px-4 py-3 border-t border-gray-700">
              {phase === 'confirming' && dryRunResult && ((dryRunResult.has_updates as boolean) || (dryRunResult.needs_restart as boolean)) && (
                <>
                  <button onClick={handleClose} className="px-3 py-1.5 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700">取消</button>
                  <button
                    onClick={handleConfirm}
                    disabled={updateBlocked}
                    className="px-3 py-1.5 text-xs rounded bg-indigo-600 text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {updateBlocked ? '等待任务完成' : ((dryRunResult.has_updates as boolean) ? '确认更新' : '完成部署并重启')}
                  </button>
                </>
              )}
              {phase === 'confirming' && dryRunResult && !(dryRunResult.has_updates as boolean) && !(dryRunResult.needs_restart as boolean) && (
                <button onClick={handleClose} className="px-3 py-1.5 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700">关闭</button>
              )}
              {phase === 'completed' && (
                <>
                  <button onClick={handleRollback} className="px-3 py-1.5 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700">回滚</button>
                  <button onClick={() => window.location.reload()} className="px-3 py-1.5 text-xs rounded bg-indigo-600 text-white hover:bg-indigo-500">刷新页面</button>
                </>
              )}
              {phase === 'failed' && (
                <>
                  {oldCommit && <button onClick={handleRollback} className="px-3 py-1.5 text-xs rounded bg-red-900/50 text-red-300 hover:bg-red-900/70">回滚</button>}
                  <button onClick={handleClose} className="px-3 py-1.5 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700">关闭</button>
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
