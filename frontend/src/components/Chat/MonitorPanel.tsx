import { useState, useEffect, useCallback } from 'react';
import { api } from '../../api/client';
import type { MonitorSession, MonitorCheck } from '../../api/client';
import { Activity, Plus, Trash2, ChevronDown, ChevronRight, X } from 'lucide-react';

interface MonitorPanelProps {
  taskId: number;
  taskMode: string;
  monitorSessions: MonitorSession[];
  onSessionsChange: (sessions: MonitorSession[]) => void;
}

export function MonitorPanel({ taskId, taskMode, monitorSessions, onSessionsChange }: MonitorPanelProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [selectedSession, setSelectedSession] = useState<MonitorSession | null>(null);
  const [checks, setChecks] = useState<MonitorCheck[]>([]);
  const [expandedChecks, setExpandedChecks] = useState<Set<number>>(new Set());

  const canCreate = taskMode === 'auto';

  useEffect(() => {
    if (isOpen && monitorSessions.length === 0) {
      api.listMonitorSessions(taskId).then(onSessionsChange).catch(() => {});
    }
  }, [isOpen, taskId]);

  const loadChecks = useCallback((session: MonitorSession) => {
    setSelectedSession(session);
    api.getMonitorChecks(taskId, session.id).then(c => {
      setChecks(c.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()));
    }).catch(() => {});
  }, [taskId]);

  const handleDelete = useCallback(async (sessionId: number) => {
    try {
      await api.deleteMonitorSession(taskId, sessionId);
      onSessionsChange(monitorSessions.filter(s => s.id !== sessionId));
      if (selectedSession?.id === sessionId) {
        setSelectedSession(null);
        setChecks([]);
      }
    } catch { /* ignore */ }
  }, [taskId, monitorSessions, selectedSession, onSessionsChange]);

  const toggleCheck = (checkId: number) => {
    setExpandedChecks(prev => {
      const next = new Set(prev);
      if (next.has(checkId)) next.delete(checkId);
      else next.add(checkId);
      return next;
    });
  };

  if (taskMode !== 'auto' && taskMode !== 'loop') return null;

  return (
    <>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-600 dark:text-gray-300"
      >
        <Activity size={12} />
        监控列表({monitorSessions.length})
      </button>

      {isOpen && (
        <div className="fixed inset-0 z-50 flex justify-end">
          <div className="absolute inset-0 bg-black/30" onClick={() => setIsOpen(false)} />
          <div className="relative w-[480px] max-w-full bg-white dark:bg-gray-800 shadow-xl flex flex-col h-full">
            <div className="flex items-center justify-between p-4 border-b dark:border-gray-700">
              <h3 className="font-semibold text-gray-900 dark:text-gray-100">监控列表</h3>
              <div className="flex items-center gap-2">
                {canCreate && (
                  <button
                    onClick={() => setShowCreateDialog(true)}
                    className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded bg-blue-500 text-white hover:bg-blue-600"
                  >
                    <Plus size={12} /> 新建监控
                  </button>
                )}
                <button onClick={() => setIsOpen(false)} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded">
                  <X size={16} />
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto">
              {selectedSession ? (
                <div className="p-4">
                  <button
                    onClick={() => { setSelectedSession(null); setChecks([]); }}
                    className="text-xs text-blue-500 hover:underline mb-3 block"
                  >
                    &larr; 返回列表
                  </button>
                  <div className="mb-3">
                    <div className="font-medium text-sm text-gray-900 dark:text-gray-100">{selectedSession.description}</div>
                    <div className="text-xs text-gray-500 mt-1">
                      {selectedSession.source === 'manual'
                        ? `${selectedSession.checks_done}/${selectedSession.max_checks} 次`
                        : `已检查 ${selectedSession.checks_done} 次`}
                      <span className="ml-2">状态: {selectedSession.status}</span>
                    </div>
                  </div>
                  {checks.length === 0 ? (
                    <div className="text-sm text-gray-400">暂无检查记录</div>
                  ) : (
                    <div className="space-y-2">
                      {checks.map(check => (
                        <div key={check.id} className="border dark:border-gray-700 rounded p-2">
                          <div
                            className="flex items-center gap-2 cursor-pointer"
                            onClick={() => toggleCheck(check.id)}
                          >
                            {expandedChecks.has(check.id) ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                            <span className="text-xs font-mono">#{check.check_number}</span>
                            <span className={`text-xs px-1 rounded ${check.status === 'completed' ? 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300' : 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300'}`}>
                              {check.status}
                            </span>
                            <span className="text-xs text-gray-500 ml-auto">
                              {new Date(check.created_at).toLocaleTimeString()}
                            </span>
                          </div>
                          {check.summary && (
                            <div className="text-xs text-gray-600 dark:text-gray-400 mt-1 ml-5">{check.summary}</div>
                          )}
                          {expandedChecks.has(check.id) && check.full_output && (
                            <pre className="text-xs bg-gray-50 dark:bg-gray-900 p-2 mt-2 rounded overflow-x-auto max-h-60 whitespace-pre-wrap">
                              {check.full_output}
                            </pre>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="p-4 space-y-2">
                  {monitorSessions.length === 0 ? (
                    <div className="text-sm text-gray-400 text-center py-8">暂无监控会话</div>
                  ) : (
                    monitorSessions.map(session => (
                      <div
                        key={session.id}
                        className="border dark:border-gray-700 rounded p-3 hover:bg-gray-50 dark:hover:bg-gray-750 cursor-pointer"
                        onClick={() => loadChecks(session)}
                      >
                        <div className="flex items-center justify-between">
                          <div className="font-medium text-sm text-gray-900 dark:text-gray-100 truncate flex-1">
                            {session.description}
                          </div>
                          <div className="flex items-center gap-2 ml-2">
                            <span className={`text-xs px-1.5 py-0.5 rounded ${
                              session.status === 'running' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300' :
                              session.status === 'completed' ? 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300' :
                              'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                            }`}>
                              {session.status}
                            </span>
                            {session.source === 'manual' && session.status === 'running' && (
                              <button
                                onClick={(e) => { e.stopPropagation(); handleDelete(session.id); }}
                                className="p-1 text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded"
                              >
                                <Trash2 size={12} />
                              </button>
                            )}
                          </div>
                        </div>
                        <div className="text-xs text-gray-500 mt-1">
                          {session.source === 'manual'
                            ? `${session.checks_done}/${session.max_checks} 次`
                            : `已检查 ${session.checks_done} 次`}
                          {session.last_summary && (
                            <span className="ml-2 truncate">{session.last_summary}</span>
                          )}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {showCreateDialog && (
        <CreateMonitorDialog
          taskId={taskId}
          onClose={() => setShowCreateDialog(false)}
          onCreated={(session) => {
            onSessionsChange([...monitorSessions, session]);
            setShowCreateDialog(false);
          }}
        />
      )}
    </>
  );
}

interface CreateMonitorDialogProps {
  taskId: number;
  onClose: () => void;
  onCreated: (session: MonitorSession) => void;
}

function CreateMonitorDialog({ taskId, onClose, onCreated }: CreateMonitorDialogProps) {
  const [description, setDescription] = useState('');
  const [monitorContext, setMonitorContext] = useState('');
  const [interval, setInterval_] = useState(300);
  const [maxChecks, setMaxChecks] = useState(100);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async () => {
    if (!description.trim()) return;
    setSubmitting(true);
    try {
      const session = await api.createMonitorSession(taskId, {
        description: description.trim(),
        monitor_context: monitorContext.trim() || undefined,
        interval,
        max_checks: maxChecks,
      });
      onCreated(session);
    } catch { /* ignore */ }
    setSubmitting(false);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl p-6 w-[400px] max-w-[90vw]">
        <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">新建监控</h3>
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">描述 *</label>
            <input
              type="text"
              value={description}
              onChange={e => setDescription(e.target.value)}
              className="w-full px-3 py-2 border dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-sm"
              placeholder="监控什么..."
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">监控上下文</label>
            <textarea
              value={monitorContext}
              onChange={e => setMonitorContext(e.target.value)}
              className="w-full px-3 py-2 border dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-sm"
              rows={3}
              placeholder="提供给监控进程的额外背景信息..."
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs text-gray-500 mb-1">间隔(秒)</label>
              <input
                type="number"
                value={interval}
                onChange={e => setInterval_(Number(e.target.value))}
                className="w-full px-3 py-2 border dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-sm"
              />
            </div>
            <div className="flex-1">
              <label className="block text-xs text-gray-500 mb-1">最大检查次数</label>
              <input
                type="number"
                value={maxChecks}
                onChange={e => setMaxChecks(Number(e.target.value))}
                className="w-full px-3 py-2 border dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-sm"
              />
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-3 py-1.5 text-sm rounded border dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700">
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={!description.trim() || submitting}
            className="px-3 py-1.5 text-sm rounded bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-50"
          >
            {submitting ? '创建中...' : '创建'}
          </button>
        </div>
      </div>
    </div>
  );
}
