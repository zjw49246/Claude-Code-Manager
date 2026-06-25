import { useState, useCallback, useRef, useEffect } from 'react';
import { ArrowUpCircle, RefreshCw } from 'lucide-react';
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

export function UpdateButton() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [steps, setSteps] = useState<StepInfo[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [dryRunResult, setDryRunResult] = useState<Record<string, unknown> | null>(null);
  const [skipFrontend, setSkipFrontend] = useState(false);
  const [error, setError] = useState('');
  const [oldCommit, setOldCommit] = useState('');
  const [newCommit, setNewCommit] = useState('');
  const [reconnectCount, setReconnectCount] = useState(0);
  const reconnectTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const logsEndRef = useRef<HTMLDivElement>(null);

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

  const startReconnectPolling = () => {
    if (reconnectTimer.current) clearInterval(reconnectTimer.current);
    let attempts = 0;
    reconnectTimer.current = setInterval(async () => {
      attempts++;
      setReconnectCount(attempts);
      try {
        await api.health();
        if (reconnectTimer.current) clearInterval(reconnectTimer.current);
        reconnectTimer.current = null;
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
          }
        } catch {
          setPhase('completed');
        }
      } catch {
        if (attempts >= 30) {
          if (reconnectTimer.current) clearInterval(reconnectTimer.current);
          reconnectTimer.current = null;
          setError('服务重启超时（60秒），请手动检查');
          setPhase('failed');
        }
      }
    }, 2000);
  };

  const handleCheck = async () => {
    setPhase('checking');
    setError('');
    try {
      const result = await api.startUpdate({ dry_run: true });
      setDryRunResult(result);
      setPhase('confirming');
    } catch (e: any) {
      setError(e.message || '检查更新失败');
      setPhase('failed');
    }
  };

  const handleConfirm = async () => {
    setPhase('running');
    setLogs([]);
    setError('');

    const defaultSteps: StepInfo[] = [
      'git_pull', 'detect_changes', 'backup_database', 'uv_sync',
      'refresh_pty', 'npm_install', 'frontend_build',
      'stop_service', 'alembic_upgrade', 'start_service',
    ].map(name => ({ name, status: 'pending' }));
    setSteps(defaultSteps);

    try {
      const result = await api.startUpdate({ skip_frontend_build: skipFrontend });
      if (result.update_id) {
        setOldCommit(result.old_commit || '');
      }
    } catch (e: any) {
      setError(e.message || '启动更新失败');
      setPhase('failed');
    }
  };

  const handleRollback = async () => {
    if (!confirm('确定要回滚到上一个版本吗？')) return;
    try {
      await api.rollbackUpdate();
      startReconnectPolling();
      setPhase('restarting');
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleClose = () => {
    setPhase('idle');
    setSteps([]);
    setLogs([]);
    setError('');
    setDryRunResult(null);
    setSkipFrontend(false);
    setOldCommit('');
    setNewCommit('');
    setReconnectCount(0);
  };

  const isModalOpen = phase !== 'idle';

  return (
    <>
      <button
        onClick={handleCheck}
        className="p-2 rounded text-gray-400 hover:text-foreground hover:bg-gray-800 transition-colors"
        title="更新并重启"
      >
        <ArrowUpCircle size={18} />
      </button>

      {isModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl w-full max-w-lg mx-4 max-h-[85vh] flex flex-col">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700">
              <h3 className="text-sm font-semibold text-foreground">
                {phase === 'checking' && '检查更新...'}
                {phase === 'confirming' && '确认更新'}
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
                  {!(dryRunResult.has_updates as boolean) ? (
                    <p className="text-sm text-gray-300">已是最新版本，无需更新。</p>
                  ) : (
                    <>
                      <div className="text-sm text-gray-300 space-y-1">
                        <p>发现 <span className="text-indigo-400 font-medium">{dryRunResult.commits_behind as number}</span> 个新提交</p>
                        <p className="text-xs text-gray-500">{dryRunResult.current_commit as string} → {dryRunResult.latest_commit as string}</p>
                      </div>

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
                  <p className="text-xs text-gray-500">每 2 秒检测一次（{reconnectCount}/30）</p>
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
              {phase === 'confirming' && dryRunResult && (dryRunResult.has_updates as boolean) && (
                <>
                  <button onClick={handleClose} className="px-3 py-1.5 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700">取消</button>
                  <button onClick={handleConfirm} className="px-3 py-1.5 text-xs rounded bg-indigo-600 text-white hover:bg-indigo-500">确认更新</button>
                </>
              )}
              {phase === 'confirming' && dryRunResult && !(dryRunResult.has_updates as boolean) && (
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
        </div>
      )}
    </>
  );
}
