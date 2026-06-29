import { useState, useEffect, useRef, useMemo, useCallback, memo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../../api/client';
import type { ChatMessage, FileAttachment, Task, Project, UploadResult, MonitorSession, AskUserQuestion, AskUserAnswer } from '../../api/client';
import { useWebSocket } from '../../hooks/useWebSocket';
import { Send, ArrowLeft, Loader2, ChevronDown, ChevronRight, ChevronUp, Copy, Check, Paperclip, X, StopCircle, Pencil, ArrowDown, Star, ListPlus, Trash2 } from 'lucide-react';
import { SecretPicker } from '../Secrets/SecretPicker';
import { QuickPhraseDropdown } from '../QuickPhrases/QuickPhraseDropdown';
import { ListFilter, Syringe } from 'lucide-react';
import { TaskConfigBadge } from '../Tasks/TaskBadges';
import { ExpandableText } from '../ExpandableText';
import { formatMessageTime } from '../../config/timezone';
import { useFileDrop } from '../../hooks/useFileDrop';
import { SubAgentIndicator } from './SubAgentIndicator';
import { MonitorPanel } from './MonitorPanel';

interface ChatViewProps {
  task: Task;
  projects: Project[];
  onBack: () => void;
  onTaskUpdated?: () => void;
  inline?: boolean;
}

interface QueuedMessage {
  text: string;
  uploadResults?: UploadResult[];
}

type MessageGroup =
  | { type: 'tool-group'; messages: ChatMessage[] }
  | { type: 'single'; message: ChatMessage };

function groupMessages(messages: ChatMessage[]): MessageGroup[] {
  const groups: MessageGroup[] = [];
  let toolBuf: ChatMessage[] = [];

  const flushTools = () => {
    if (toolBuf.length > 0) {
      groups.push({ type: 'tool-group', messages: [...toolBuf] });
      toolBuf = [];
    }
  };

  for (const msg of messages) {
    const isTool = msg.event_type === 'tool_use' || msg.event_type === 'tool_result';
    if (isTool) {
      toolBuf.push(msg);
    } else {
      flushTools();
      groups.push({ type: 'single', message: msg });
    }
  }
  flushTools();
  return groups;
}

interface ContextUsage {
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  total_input_tokens: number;
  context_window?: number;
}

function formatTokenCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function ContextUsageIndicator({ usage }: { usage: ContextUsage }) {
  const contextWindow = usage.context_window;
  const totalUsed = usage.total_input_tokens + usage.output_tokens;
  const percentage = contextWindow ? Math.min((totalUsed / contextWindow) * 100, 100) : null;

  // Color based on usage level
  let barColor = 'bg-emerald-500';
  let textColor = 'text-emerald-400';
  if (percentage !== null && percentage > 80) {
    barColor = 'bg-red-500';
    textColor = 'text-red-400';
  } else if (percentage !== null && percentage > 50) {
    barColor = 'bg-amber-500';
    textColor = 'text-amber-400';
  }

  return (
    <div className="flex items-center gap-2 text-xs shrink-0" title={`Input: ${formatTokenCount(usage.input_tokens)} | Cache read: ${formatTokenCount(usage.cache_read_input_tokens)} | Cache create: ${formatTokenCount(usage.cache_creation_input_tokens)} | Output: ${formatTokenCount(usage.output_tokens)}${contextWindow ? ` | Context window: ${formatTokenCount(contextWindow)}` : ' | Context window: unknown'}`}>
      <div className="flex items-center gap-1.5">
        <span className={`${textColor} font-medium`}>{formatTokenCount(totalUsed)}</span>
        <span className="text-gray-600">/</span>
        <span className="text-gray-500">{contextWindow ? formatTokenCount(contextWindow) : 'unknown'}</span>
      </div>
      {percentage !== null && (
        <>
          <div className="w-16 h-1.5 bg-gray-700 rounded-full overflow-hidden">
            <div className={`h-full ${barColor} rounded-full transition-all duration-300`} style={{ width: `${percentage}%` }} />
          </div>
          <span className={`${textColor} w-8 text-right`}>{percentage.toFixed(0)}%</span>
        </>
      )}
    </div>
  );
}

export function ChatView({ task, projects, onBack, onTaskUpdated, inline }: ChatViewProps) {
  const projectName = useMemo(() => {
    if (!task.project_id) return null;
    const p = projects.find((p) => p.id === task.project_id);
    return p?.name ?? null;
  }, [task.project_id, projects]);
  const providerLabel = task.provider === 'codex' ? 'Codex' : 'Claude';
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // Draft buffer: unsent input survives refresh / re-entering the chat
  const [input, setInput] = useState(() => {
    try { return localStorage.getItem(`ccm-chat-draft-${task.id}`) || ''; } catch { return ''; }
  });
  const [sending, setSending] = useState(false);
  const [localStatus, setLocalStatus] = useState<string | null>(task.status);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [interrupting, setInterrupting] = useState(false);
  const [stillRunning, setStillRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dropError, setDropError] = useState<string | null>(null);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [filePreviews, setFilePreviews] = useState<string[]>([]);
  const [selectedSecretIds, setSelectedSecretIds] = useState<number[]>([]);
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(task.context_window_usage ?? null);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(task.title || '');
  const titleInputRef = useRef<HTMLInputElement>(null);
  const [titleExpanded, setTitleExpanded] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [starred, setStarred] = useState(task.starred);

  // Temp model override (one-shot per message, not persisted to the task)
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [showModelMenu, setShowModelMenu] = useState(false);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [ptyMode, setPtyMode] = useState(false);
  const [injecting, setInjecting] = useState(false);
  // 注入模式开关：开启后「发送」走 PTY 注入逻辑而不是排队新 turn
  const [injectMode, setInjectMode] = useState(false);

  useEffect(() => {
    api.getRuntimeSettings().then((s) => setPtyMode(s.use_pty_mode)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!showModelMenu) return;
    if (modelOptions.length === 0) {
      api.config().then((c) => {
        const opts = (task.provider === 'codex' ? c.codex_model_options : c.model_options).filter((m) => m !== 'default');
        setModelOptions(opts);
      }).catch(() => {});
    }
    const handle = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest('[data-temp-model]')) setShowModelMenu(false);
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [showModelMenu, modelOptions.length, task.provider]);

  const handleInject = async () => {
    const text = input.trim();
    if (!text || injecting) return;
    setInjecting(true);
    setError(null);
    try {
      await api.injectTaskMessage(task.id, text);
      setInput('');
    } catch (e) {
      setError(`注入失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setInjecting(false);
    }
  };

  // Persist the draft as the user types; cleared when input empties (e.g. send)
  useEffect(() => {
    try {
      if (input) localStorage.setItem(`ccm-chat-draft-${task.id}`, input);
      else localStorage.removeItem(`ccm-chat-draft-${task.id}`);
    } catch { /* storage may be unavailable */ }
  }, [input, task.id]);
  const [monitorSessions, setMonitorSessions] = useState<MonitorSession[]>([]);
  const [showMonitorPanel, setShowMonitorPanel] = useState(false);
  const effectiveStatus = localStatus || task.status;
  const isProcessing = sending || ['in_progress', 'executing'].includes(effectiveStatus);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const HISTORY_PAGE_SIZE = 200;

  const navigateUserMessage = useCallback((direction: 'up' | 'down') => {
    const container = messagesContainerRef.current;
    if (!container) return;
    const nodes = Array.from(container.querySelectorAll<HTMLElement>('[data-user-msg]'));
    if (nodes.length === 0) return;

    const containerRect = container.getBoundingClientRect();
    const threshold = 30;

    if (direction === 'up') {
      for (let i = nodes.length - 1; i >= 0; i--) {
        const rect = nodes[i].getBoundingClientRect();
        if (rect.top < containerRect.top - threshold) {
          nodes[i].scrollIntoView({ behavior: 'smooth', block: 'start' });
          return;
        }
      }
    } else {
      for (const node of nodes) {
        const rect = node.getBoundingClientRect();
        if (rect.top > containerRect.top + threshold) {
          node.scrollIntoView({ behavior: 'smooth', block: 'start' });
          return;
        }
      }
    }
  }, []);

  // Message queue: pre-queue messages to auto-send after current turn completes
  const [messageQueue, setMessageQueue] = useState<QueuedMessage[]>(() => {
    try {
      const saved = localStorage.getItem(`ccm-chat-queue-${task.id}`);
      if (!saved) return [];
      const parsed = JSON.parse(saved);
      // Migrate legacy string[] format
      if (Array.isArray(parsed) && parsed.length > 0 && typeof parsed[0] === 'string') {
        return (parsed as string[]).map(text => ({ text }));
      }
      return parsed;
    } catch { return []; }
  });
  const messageQueueRef = useRef(messageQueue);
  useEffect(() => {
    messageQueueRef.current = messageQueue;
    localStorage.setItem(`ccm-chat-queue-${task.id}`, JSON.stringify(messageQueue));
  }, [messageQueue, task.id]);

  const addToQueue = useCallback(async (text: string, files?: File[]) => {
    let uploadResults: UploadResult[] | undefined;
    if (files && files.length > 0) {
      try {
        uploadResults = await api.uploadImages(files);
      } catch {
        // Upload failed — queue text only
      }
    }
    setMessageQueue(prev => [...prev, { text, uploadResults }]);
  }, []);

  const removeFromQueue = useCallback((index: number) => {
    setMessageQueue(prev => prev.filter((_, i) => i !== index));
  }, []);

  const editQueueItem = useCallback((index: number) => {
    const item = messageQueueRef.current[index];
    if (!item) return;
    setInput(prev => prev.trim() ? `${prev.trim()}\n\n${item.text}` : item.text);
    setMessageQueue(prev => prev.filter((_, i) => i !== index));
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, []);

  const mergeQueueToInput = useCallback(() => {
    const queued = messageQueueRef.current;
    if (queued.length === 0) return;
    setInput(prev => {
      const current = prev.trim();
      const merged = queued.map(q => q.text).join('\n\n');
      return current ? `${current}\n\n${merged}` : merged;
    });
    setMessageQueue([]);
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, []);

  const moveQueueItem = useCallback((index: number, direction: 'up' | 'down') => {
    setMessageQueue(prev => {
      const next = [...prev];
      const target = direction === 'up' ? index - 1 : index + 1;
      if (target < 0 || target >= next.length) return prev;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }, []);

  // Auto-dequeue: triggered by process_exit via flag increment
  const [autoDequeueFlag, setAutoDequeueFlag] = useState(0);
  const sendingRef = useRef(false);
  sendingRef.current = sending;
  const handleSendRef = useRef<(text: string, uploadResults?: UploadResult[]) => void>(() => {});

  useEffect(() => {
    if (autoDequeueFlag === 0) return;
    // Delay to let React flush setSending(false) from status_change/process_exit
    // before we check sendingRef. Without this, PTY mode (no process_exit)
    // triggers autoDequeue in the same cycle as setSending(false) and the
    // ref still reads true → skips the queued message.
    const timer = setTimeout(() => {
      if (sendingRef.current) return;
      const queue = messageQueueRef.current;
      if (queue.length > 0) {
        const next = queue[0];
        setMessageQueue(prev => prev.slice(1));
        setTimeout(() => handleSendRef.current(next.text, next.uploadResults), 300);
      }
    }, 200);
    return () => clearTimeout(timer);
  }, [autoDequeueFlag]);

  useEffect(() => {
    const prev = document.title;
    const label = task.title || task.description || '';
    const preview = label.length > 30 ? label.slice(0, 30) + '…' : label;
    document.title = preview ? `#${task.id} ${preview}` : `#${task.id} - CCM`;
    return () => { document.title = prev; };
  }, [task.id, task.title, task.description]);

  // Handle real-time WebSocket messages via callback (not state) to avoid
  // losing messages when React batches rapid state updates.
  const handleWsMessage = useCallback((raw: Record<string, unknown>) => {
    const msg = raw as { channel?: string; data?: Record<string, unknown> };
    // System channel: react to PTY mode toggling without a refresh
    if (msg.channel === 'system' && msg.data?.event === 'runtime_settings_changed') {
      setPtyMode(Boolean(msg.data.use_pty_mode));
      return;
    }
    // Status change: update local override for "thinking" indicator.
    // Handles both "tasks" global channel and "task:{id}" channel (from SharedRelay mirror).
    const isStatusChange = (
      (msg.channel === 'tasks' && msg.data?.event === 'status_change' && msg.data.task_id === task.id) ||
      (msg.channel === `task:${task.id}` && (msg.data?.event === 'status_change' || msg.data?.event_type === 'status_change'))
    );
    if (isStatusChange) {
      const newStatus = (msg.data!.new_status as string) || '';
      if (newStatus) {
        setLocalStatus(newStatus);
      }
      return;
    }

    if (msg.channel !== `task:${task.id}` || !msg.data) return;

    const eventType = msg.data.event_type as string || (msg.data.event as string);

    if (eventType === 'monitor_session_created' || eventType === 'monitor_session_status'
        || eventType === 'sub_agent_session_created' || eventType === 'sub_agent_session_status') {
      api.listMonitorSessions(task.id).then(setMonitorSessions).catch(() => {});
      return;
    }

    // 权限透传：CC 请求权限 → 聊天卡片；用户点按钮回包
    if (eventType === 'permission_request') {
      const entry: ChatMessage = {
        id: Date.now() + Math.random(),
        role: 'system',
        event_type: 'permission_request',
        content: (msg.data.description as string) || null,
        tool_name: (msg.data.tool_name as string) || null,
        tool_input: (msg.data.input_preview as string) || null,
        tool_output: null,
        is_error: false,
        loop_iteration: null,
        timestamp: new Date().toISOString(),
        image_urls: null,
        attachments: null,
        request_id: (msg.data.request_id as string) || null,
        permission_status: 'pending',
      };
      setMessages((prev) => [...prev, entry]);
      return;
    }
    if (eventType === 'permission_resolved') {
      const rid = msg.data.request_id as string;
      const behavior = msg.data.behavior as 'allow' | 'deny';
      setMessages((prev) => prev.map((m) =>
        m.event_type === 'permission_request' && m.request_id === rid
          ? { ...m, permission_status: behavior }
          : m
      ));
      return;
    }

    // ask_user：CC 调用内置 AskUserQuestion 被 hook 拦截 → 可选卡片；用户选完回包
    if (eventType === 'ask_user_question') {
      const rid = (msg.data.request_id as string) || null;
      const questions = (msg.data.questions as AskUserQuestion[]) || [];
      setMessages((prev) => {
        if (rid && prev.some((m) => m.event_type === 'ask_user_question' && m.request_id === rid)) {
          return prev; // 去重（重连回填可能与 WS 撞车）
        }
        const entry: ChatMessage = {
          id: Date.now() + Math.random(),
          role: 'system',
          event_type: 'ask_user_question',
          content: null,
          tool_name: 'AskUserQuestion',
          tool_input: null,
          tool_output: null,
          is_error: false,
          loop_iteration: null,
          timestamp: new Date().toISOString(),
          image_urls: null,
          attachments: null,
          request_id: rid,
          ask_questions: questions,
          ask_status: 'pending',
        };
        return [...prev, entry];
      });
      return;
    }
    if (eventType === 'ask_user_resolved') {
      const rid = msg.data.request_id as string;
      const timedOut = !!msg.data.timed_out;
      setMessages((prev) => prev.map((m) =>
        m.event_type === 'ask_user_question' && m.request_id === rid
          ? { ...m, ask_status: timedOut ? 'timed_out' : 'answered' }
          : m
      ));
      return;
    }

    // 模型原生子 agent 的进度（PTY 观测，经 sub_agent_sessions 镜像）
    if (eventType === 'sub_agent_report') {
      api.listMonitorSessions(task.id).then(setMonitorSessions).catch(() => {});
      return;
    }

    if (eventType === 'monitor_check') {
      api.listMonitorSessions(task.id).then(setMonitorSessions).catch(() => {});
      const summary = msg.data.summary as string;
      const monitorSessionId = msg.data.monitor_session_id as number;
      const checkNumber = msg.data.check_number as number;
      if (summary) {
        const entry: ChatMessage = {
          id: Date.now() + Math.random(),
          role: 'system',
          event_type: 'system_event',
          content: `[Monitor #${monitorSessionId}] Check #${checkNumber}: ${summary}`,
          tool_name: null,
          tool_input: null,
          tool_output: null,
          is_error: (msg.data.status as string) === 'failed',
          loop_iteration: null,
          timestamp: new Date().toISOString(),
          image_urls: null,
          attachments: null,
          source: 'monitor',
        };
        setMessages((prev) => [...prev, entry]);
      }
      return;
    }

    // Anthropic 基础设施侧临时限流/过载（非额度用尽）：后端正在退避后用同一
    // 账号自动重试。提示用户并保持"处理中"指示（PTY 下这是 exit_code=0 的
    // 中止 turn，process_exit 可能先到、会熄灭 spinner，这里重新点亮）。
    if (eventType === 'transient_retry') {
      const attempt = (msg.data.attempt as number) || 1;
      const maxAttempts = (msg.data.max_attempts as number) || 0;
      const delay = (msg.data.delay as number) || 0;
      setSending(true);
      const entry: ChatMessage = {
        id: Date.now() + Math.random(),
        role: 'system',
        event_type: 'transient_retry',
        content: `服务端临时限流（非额度用尽）· 第 ${attempt}${maxAttempts ? `/${maxAttempts}` : ''} 次自动重试，约 ${delay}s 后继续…`,
        tool_name: null,
        tool_input: null,
        tool_output: null,
        is_error: false,
        loop_iteration: null,
        timestamp: new Date().toISOString(),
        image_urls: null,
        attachments: null,
        source: 'transient_retry',
      };
      setMessages((prev) => [...prev, entry]);
      return;
    }

    if (eventType === 'process_exit') {
      // Small delay so any final output messages queued just before
      // process_exit are rendered before the "thinking" indicator hides.
      setTimeout(() => {
        setSending(false);
        setLocalStatus(null);  // Reset — status_change WS may have been missed
        setAutoDequeueFlag(f => f + 1);
      }, 500);
      return;
    }

    // Track context window usage
    if (eventType === 'context_usage' && msg.data) {
      setContextUsage((prev) => ({
        input_tokens: (msg.data!.input_tokens as number) || 0,
        cache_read_input_tokens: (msg.data!.cache_read_input_tokens as number) || 0,
        cache_creation_input_tokens: (msg.data!.cache_creation_input_tokens as number) || 0,
        output_tokens: (msg.data!.output_tokens as number) || 0,
        total_input_tokens: (msg.data!.total_input_tokens as number) || 0,
        context_window: (msg.data!.context_window as number) || prev?.context_window,
      }));
      return;
    }

    // WS user_message: append unless already shown (optimistic queue send).
    // Also trigger "thinking" indicator.
    if (eventType === 'user_message') {
      const content = (msg.data.content as string) || '';
      const source = (msg.data.source as string) || null;
      const imageUrls = (msg.data.image_urls as string[]) || null;
      const attachments = (msg.data.attachments as { url: string; name: string; is_image: boolean }[]) || null;
      setSending(true);
      setMessages((prev) => {
        // Skip if last message is an optimistic duplicate (same content, recent)
        const last = prev[prev.length - 1];
        if (last && last.role === 'user' && last.event_type === 'user_message' && last.content === content) {
          return prev;
        }
        return [...prev, {
          id: Date.now() + Math.random(), role: 'user', event_type: 'user_message',
          content, tool_name: null, tool_input: null, tool_output: null,
          is_error: false, loop_iteration: null, timestamp: new Date().toISOString(),
          image_urls: imageUrls, attachments: attachments, source,
        }];
      });
      return;
    }

    const showTypes = ['message', 'result', 'tool_use', 'tool_result', 'system_init', 'system_event', 'thinking'];
    if (!showTypes.includes(eventType)) return;

    // Skip noisy system events (heartbeats, telemetry subtypes)
    const skipSystemContent = ['task_progress', 'thinking_tokens', 'token_usage', 'api_request', 'api_response'];
    if (eventType === 'system_event' && skipSystemContent.includes(msg.data.content as string)) return;

    const content = (msg.data.content as string) || null;
    // Skip empty assistant messages (partial streaming chunks with no text)
    if ((eventType === 'message' || eventType === 'result') && !content) return;
    // Skip CC internal messages (compact summaries, task-notifications) — real user input uses event_type=user_message
    if (eventType === 'message' && (msg.data.role as string) === 'user') return;

    const entry: ChatMessage = {
      id: Date.now() + Math.random(),
      role: (msg.data.role as string) || 'assistant',
      event_type: eventType,
      content,
      tool_name: (msg.data.tool_name as string) || null,
      tool_input: (msg.data.tool_input as string) || null,
      tool_output: (msg.data.tool_output as string) || null,
      is_error: (msg.data.is_error as boolean) || false,
      loop_iteration: (msg.data.loop_iteration as number) || null,
      timestamp: new Date().toISOString(),
      image_urls: (msg.data.image_urls as string[]) || null,
      attachments: (msg.data.attachments as FileAttachment[]) || null,
      source: (msg.data.source as string) || null,
    };
    setMessages((prev) => [...prev, entry]);
  }, [task.id]);

  const fetchHistory = useCallback(() => {
    setHistoryLoading(true);
    Promise.all([
      api.getTaskChatHistory(task.id, true, HISTORY_PAGE_SIZE, 0, true),
      api.getAskUserPending(task.id).catch(() => ({ pending: [] as { request_id: string; questions: AskUserQuestion[] }[] })),
    ]).then(([msgs, askPending]) => {
      const filtered = msgs.filter((m) =>
        !((m.event_type === 'message' || m.event_type === 'result') && !m.content)
      );
      setHasMoreHistory(msgs.length >= HISTORY_PAGE_SIZE);
      const existingIds = new Set(
        filtered.filter((m) => m.event_type === 'ask_user_question').map((m) => m.request_id)
      );
      const cards: ChatMessage[] = (askPending.pending || [])
        .filter((p) => !existingIds.has(p.request_id))
        .map((p) => ({
          id: Date.now() + Math.random(),
          role: 'system' as const,
          event_type: 'ask_user_question',
          content: null,
          tool_name: 'AskUserQuestion',
          tool_input: null,
          tool_output: null,
          is_error: false,
          loop_iteration: null,
          timestamp: new Date().toISOString(),
          image_urls: null,
          attachments: null,
          request_id: p.request_id,
          ask_questions: p.questions,
          ask_status: 'pending',
        }));
      setMessages(cards.length ? [...filtered, ...cards] : filtered);
    }).catch(() => {}).finally(() => setHistoryLoading(false));
  }, [task.id]);

  const scrollRestorationRef = useRef<number | null>(null);

  const loadMoreHistory = useCallback(() => {
    if (loadingMore || !hasMoreHistory || messages.length === 0) return;
    const oldestId = messages[0]?.id;
    if (!oldestId) return;
    const container = messagesContainerRef.current;
    if (container) scrollRestorationRef.current = container.scrollHeight;
    setLoadingMore(true);
    api.getTaskChatHistory(task.id, true, HISTORY_PAGE_SIZE, oldestId).then((msgs) => {
      const filtered = msgs.filter((m) =>
        !((m.event_type === 'message' || m.event_type === 'result') && !m.content)
      );
      if (filtered.length > 0) {
        setMessages((prev) => [...filtered, ...prev]);
      }
      setHasMoreHistory(msgs.length >= HISTORY_PAGE_SIZE);
    }).catch(() => {}).finally(() => setLoadingMore(false));
  }, [task.id, messages, loadingMore, hasMoreHistory]);

  useEffect(() => {
    if (scrollRestorationRef.current !== null && !loadingMore) {
      const container = messagesContainerRef.current;
      if (container) {
        container.scrollTop += container.scrollHeight - scrollRestorationRef.current;
      }
      scrollRestorationRef.current = null;
    }
  }, [loadingMore]);

  // Re-fetch history when WebSocket reconnects to pick up any messages
  // that arrived during the disconnection gap
  const handleReconnect = useCallback(() => {
    fetchHistory();
  }, [fetchHistory]);

  useWebSocket([`task:${task.id}`, 'system', 'tasks'], handleWsMessage, handleReconnect);

  // Reset sending state when task reaches a terminal status
  // (catches cases where process_exit WebSocket event is missed — e.g. WS disconnect)
  // Also trigger auto-dequeue so pending box messages get sent.
  useEffect(() => {
    if (['completed', 'failed', 'cancelled', 'pending'].includes(effectiveStatus)) {
      setSending(false);
      setAutoDequeueFlag(f => f + 1);
    }
  }, [effectiveStatus]);

  // Load chat history
  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  // Always load monitor sessions (commands can create monitors even without permanent skill)
  useEffect(() => {
    api.listMonitorSessions(task.id).then(setMonitorSessions).catch(() => {});
  }, [task.id]);


  const monitorCount = useMemo(
    () => monitorSessions.filter((s) => s.status === 'running').length,
    [monitorSessions]
  );

  const grouped = useMemo(() => groupMessages(messages), [messages]);

  // Reset scroll flag when switching tasks
  const hasScrolledRef = useRef(false);
  useEffect(() => {
    hasScrolledRef.current = false;
  }, [task.id]);

  // Lock body scroll while ChatView is open to prevent scroll bleed-through
  useEffect(() => {
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = '';
    };
  }, []);


  const loadMoreRef = useRef(loadMoreHistory);
  loadMoreRef.current = loadMoreHistory;

  // Auto-scroll only on initial history load
  useEffect(() => {
    if (messages.length > 0 && !hasScrolledRef.current) {
      hasScrolledRef.current = true;
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = el.scrollHeight + 'px';
  }, [input]);

  const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
  const isImageFile = (f: File) => IMAGE_EXTS.some((ext) => f.name.toLowerCase().endsWith(ext));

  const { addFiles } = useFileDrop({
    pendingFiles,
    setPendingFiles,
    setFilePreviews,
    disabled: !task.session_id && !task.shared_from_id,
    onError: (msg) => setDropError(msg),
  });

  useEffect(() => {
    if (!task.session_id && !task.shared_from_id) return;
    const handlePaste = (e: ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const files: File[] = [];
      for (const item of items) {
        if (item.kind === 'file') {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length > 0) {
        e.preventDefault();
        addFiles(files);
      }
    };
    document.addEventListener('paste', handlePaste);
    return () => document.removeEventListener('paste', handlePaste);
  }, [task.session_id, addFiles]);

  useEffect(() => {
    if (dropError) {
      const t = setTimeout(() => setDropError(null), 2000);
      return () => clearTimeout(t);
    }
  }, [dropError]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const combined = [...pendingFiles, ...files].slice(0, 10);
    setPendingFiles(combined);
    setFilePreviews(combined.map((f) => isImageFile(f) ? URL.createObjectURL(f) : ''));
    e.target.value = '';
  };

  const removeFile = (idx: number) => {
    if (filePreviews[idx]) URL.revokeObjectURL(filePreviews[idx]);
    setPendingFiles((prev) => prev.filter((_, i) => i !== idx));
    setFilePreviews((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleTitleSave = async () => {
    const trimmed = titleDraft.trim();
    if (trimmed === (task.title || '')) {
      setEditingTitle(false);
      return;
    }
    try {
      await api.updateTask(task.id, { title: trimmed });
      onTaskUpdated?.();
    } catch { /* ignore */ }
    setEditingTitle(false);
  };

  const handleStar = async () => {
    try {
      const updated = await api.starTask(task.id);
      setStarred(updated.starred);
      onTaskUpdated?.();
    } catch { /* ignore */ }
  };

  const handleSend = async (overrideText?: string, fromQueue?: boolean, preUploadedResults?: UploadResult[]) => {
    const text = (overrideText ?? input).trim();
    if (!text && pendingFiles.length === 0 && !preUploadedResults?.length) return;

    // 注入模式：发送动作改走 PTY 注入（仅文本；不开新 turn、不排队）
    if (injectMode && ptyMode && !fromQueue) {
      if (text) await handleInject();
      return;
    }

    // If currently sending and not from auto-dequeue, add to queue (with files)
    if (isProcessing && !fromQueue) {
      if (text || pendingFiles.length > 0) {
        const filesToQueue = pendingFiles.length > 0 ? [...pendingFiles] : undefined;
        addToQueue(text, filesToQueue);
        setInput('');
        if (filesToQueue) {
          filePreviews.forEach(url => { if (url) URL.revokeObjectURL(url); });
          setPendingFiles([]);
          setFilePreviews([]);
        }
      }
      return;
    }

    const snapshotFiles = fromQueue ? [] : [...pendingFiles];
    const snapshotPreviews = fromQueue ? [] : [...filePreviews];

    if (!fromQueue) {
      setInput('');
      setPendingFiles([]);
      setFilePreviews([]);
    }
    setSending(true);
    setError(null);

    try {
      let uploadedPaths: string[] | undefined;

      if (preUploadedResults && preUploadedResults.length > 0) {
        uploadedPaths = preUploadedResults.map((r) => r.path);
      } else if (snapshotFiles.length > 0) {
        const results: UploadResult[] = await api.uploadImages(snapshotFiles);
        uploadedPaths = results.map((r) => r.path);
      }
      snapshotPreviews.forEach((url) => { if (url) URL.revokeObjectURL(url); });

      // Optimistic message for queued sends — WS may miss during rapid turn cycles.
      // For manual sends WS user_message arrives fast enough, but queue auto-sends
      // fire right after process_exit when WS may be reconnecting.
      if (fromQueue && text) {
        setMessages(prev => [...prev, {
          id: Date.now() + Math.random(), role: 'user', event_type: 'user_message',
          content: text, tool_name: null, tool_input: null, tool_output: null,
          is_error: false, loop_iteration: null, timestamp: new Date().toISOString(),
          image_urls: null, attachments: null,
        }]);
      }

      await api.sendTaskChat(task.id, text || '(files attached)', uploadedPaths, selectedSecretIds.length > 0 ? selectedSecretIds : undefined, modelOverride);
      setModelOverride(null);
    } catch (e) {
      setSending(false);
      const errMsg = String(e);
      // 409 = task still being processed, show Interrupt button
      if (errMsg.includes('409') || errMsg.toLowerCase().includes('currently being processed')) {
        setStillRunning(true);
      }
      setError(errMsg);
      // If from queue and failed, re-queue at front
      if (fromQueue && (text || preUploadedResults?.length)) {
        setMessageQueue(prev => [{ text, uploadResults: preUploadedResults }, ...prev]);
      }
    }
  };

  // Keep ref updated for auto-dequeue effect
  handleSendRef.current = (text: string, uploadResults?: UploadResult[]) => handleSend(text, true, uploadResults);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className={inline ? "flex flex-col h-full bg-gray-950" : "fixed inset-0 bg-gray-950 flex flex-col z-50"}>
      {/* Header — two rows */}
      <div className="px-3 sm:px-4 py-1.5 pt-[max(0.375rem,env(safe-area-inset-top))] border-b border-gray-800 bg-gray-900">
        {/* Row 1: back + task info + action buttons */}
        <div className="flex items-center gap-2 sm:gap-3">
          <button onClick={onBack} className="text-gray-400 hover:text-foreground shrink-0">
            <ArrowLeft size={20} />
          </button>
          <div className="flex items-center gap-1.5 min-w-0 flex-1">
            <p className="text-foreground font-medium text-sm whitespace-nowrap">Task #{task.id}</p>
            <span className={`text-xs px-1.5 rounded font-medium whitespace-nowrap ${task.provider === 'codex' ? 'bg-green-600/30 text-green-300' : 'bg-blue-600/30 text-blue-300'}`}>
              {providerLabel}
            </span>
            {projectName && (
              <span className="text-xs bg-emerald-600/30 text-emerald-300 px-1.5 rounded font-medium whitespace-nowrap truncate">{projectName}</span>
            )}
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <SubAgentIndicator
              taskId={task.id}
              count={monitorCount}
              active={monitorCount > 0}
              onNavigate={() => setShowMonitorPanel(!showMonitorPanel)}
            />
            <TaskConfigBadge task={task} onRefresh={() => onTaskUpdated?.()} align="right" />
            <button
              onClick={handleStar}
              className={`p-1.5 transition-colors ${starred ? 'text-yellow-400 hover:text-yellow-300' : 'text-gray-600 hover:text-yellow-400'}`}
              title={starred ? "Unstar" : "Star"}
            >
              <Star size={18} fill={starred ? 'currentColor' : 'none'} />
            </button>
            {(sending || stillRunning || ['in_progress', 'executing'].includes(effectiveStatus)) && (
              <button
                onClick={async () => {
                  setInterrupting(true);
                  try {
                    const resp = await api.stopTaskSession(task.id);
                    setSending(false);
                    setStillRunning(false);
                    if (resp.stopped === false) {
                      const cleared = resp.cleared_messages ?? 0;
                      setError(
                        `Interrupt: no running process found${cleared > 0 ? `, cleared ${cleared} queued message(s)` : ''}. ` +
                        'If output keeps arriving, the session may still be finishing.'
                      );
                    } else {
                      setError(null);
                    }
                  } catch (e) {
                    setSending(false);
                    setStillRunning(false);
                    setLocalStatus(null);
                  }
                  finally { setInterrupting(false); }
                }}
                disabled={interrupting}
                className="flex items-center gap-1 px-2.5 py-1.5 text-xs text-red-400 hover:text-red-300 border border-red-500/30 rounded hover:bg-red-500/10 disabled:opacity-50"
                title="Interrupt session"
              >
                <StopCircle size={14} />
                <span className="hidden sm:inline">{interrupting ? 'Interrupting...' : 'Interrupt'}</span>
              </button>
            )}
          </div>
        </div>
        {/* Row 2: title + context usage */}
        <div className="flex items-center gap-2 mt-0.5 pl-7 sm:pl-8">
          <div className="flex-1 min-w-0">
            {editingTitle ? (
              <input
                ref={titleInputRef}
                autoFocus
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onBlur={handleTitleSave}
                onKeyDown={(e) => { if (e.key === 'Enter') handleTitleSave(); if (e.key === 'Escape') { setTitleDraft(task.title || ''); setEditingTitle(false); } }}
                className="w-full bg-gray-800 text-foreground text-xs rounded px-2 py-0.5 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                placeholder="Enter title..."
              />
            ) : (
              <div className="flex items-center gap-1 min-w-0 group/title">
                <span className={`text-xs text-gray-500 ${titleExpanded ? 'whitespace-normal break-all' : 'truncate'}`}>{task.title || task.description || 'Untitled'}</span>
                <button
                  onClick={() => setTitleExpanded(!titleExpanded)}
                  className="text-[10px] text-gray-600 hover:text-gray-300 shrink-0 whitespace-nowrap"
                >{titleExpanded ? 'less' : 'more'}</button>
                <button
                  onClick={() => { setTitleDraft(task.title || ''); setEditingTitle(true); }}
                  className="text-gray-600 hover:text-gray-400 opacity-0 group-hover/title:opacity-100 transition-opacity shrink-0"
                  title="Edit title"
                >
                  <Pencil size={10} />
                </button>
              </div>
            )}
          </div>
          {contextUsage && (
            <span className="flex items-center shrink-0">
              <ContextUsageIndicator usage={contextUsage} />
            </span>
          )}
        </div>
      </div>

      {/* Monitor Panel */}
      {showMonitorPanel && (
        <div className="px-4 py-2 border-b border-gray-800">
          <MonitorPanel
            taskId={task.id}
            sessions={monitorSessions}
            onSessionsChange={setMonitorSessions}
            onClose={() => setShowMonitorPanel(false)}
          />
        </div>
      )}

      {/* Interrupting banner */}
      {interrupting && (
        <div className="flex items-center gap-2 px-4 py-2 bg-yellow-500/10 border-b border-yellow-500/30 text-yellow-400 text-xs">
          <Loader2 size={14} className="animate-spin" />
          Interrupting {providerLabel}... waiting for graceful shutdown
        </div>
      )}

      {/* Load older messages banner — fixed above scroll area */}
      {messages.length > 0 && hasMoreHistory && (
        <div className="flex justify-center py-1.5 border-b border-gray-800 bg-gray-950/80 shrink-0">
          <button
            onClick={loadMoreHistory}
            disabled={loadingMore}
            className="text-xs text-gray-400 hover:text-gray-200 px-3 py-1 rounded-full bg-gray-800 hover:bg-gray-700 transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            {loadingMore ? <Loader2 size={12} className="animate-spin" /> : <ChevronUp size={12} />}
            {loadingMore ? 'Loading...' : 'Load older messages'}
          </button>
        </div>
      )}

      {/* Messages */}
      <div ref={messagesContainerRef} className="flex-1 overflow-y-auto p-4 space-y-3 min-h-0">
        {messages.length === 0 && historyLoading && (
          <div className="flex items-center justify-center gap-2 text-gray-500 mt-20">
            <Loader2 size={16} className="animate-spin" />
            <span className="text-sm">Loading chat history...</span>
          </div>
        )}
        {messages.length === 0 && !historyLoading && (
          <div className="text-center text-gray-600 mt-20">
            <p className="text-lg mb-2">Chat with this task</p>
            <p className="text-sm">
              {task.session_id
                ? 'Send a follow-up message to continue the conversation'
                : 'This task has no session yet. Run it first via Ralph Loop or manually.'}
            </p>
          </div>
        )}
        {/* Initial prompt bubble */}
        {task.description && (
          <div data-user-msg>
            <div className="text-center text-xs text-gray-600 py-1 mb-1">— Initial Prompt —</div>
            <div className="flex justify-end">
              <div className="max-w-[85%] group">
                <div className="rounded-2xl px-4 py-2.5 text-sm bg-indigo-600 text-white rounded-br-md">
                  {task.metadata_?.attachments && task.metadata_.attachments.length > 0 && (
                    <div className="mb-2 flex flex-wrap gap-2">
                      {task.metadata_.attachments.filter((a) => a.is_image).length > 0 && (
                        <MessageImages urls={task.metadata_.attachments.filter((a) => a.is_image).map((a) => a.url)} />
                      )}
                      {task.metadata_.attachments.filter((a) => !a.is_image).map((a, i) => (
                        <a key={i} href={a.url} target="_blank" rel="noopener noreferrer"
                          className="flex items-center gap-1.5 px-3 py-1.5 bg-indigo-500/30 rounded-lg text-xs text-indigo-100 hover:bg-indigo-500/40 transition-colors max-w-[200px]"
                        >
                          <Paperclip size={12} className="shrink-0" />
                          <span className="truncate">{a.name}</span>
                        </a>
                      ))}
                    </div>
                  )}
                  <ExpandableText
                    text={task.description!}
                    collapsedLines={6}
                    className="whitespace-pre-wrap text-white"
                    expandedClassName="whitespace-pre-wrap text-white"
                  />
                </div>
                <div className="flex items-center justify-end gap-1 mt-0.5 pr-1">
                  {task.created_at && <MessageTimestamp timestamp={task.created_at} />}
                  <MessageCopyButton text={task.description} />
                </div>
              </div>
            </div>
          </div>
        )}
        {grouped.map((group, i) =>
          group.type === 'tool-group' ? (
            <ToolGroup key={i} messages={group.messages} taskId={task.id} />
          ) : (
            <MessageBubble key={group.message.id} message={group.message} taskId={task.id} />
          )
        )}
        {sending && (
          <div className="flex gap-2 items-center text-gray-500 text-sm px-3">
            <Loader2 size={14} className="animate-spin" />
            <span>{providerLabel} is thinking...</span>
          </div>
        )}
        <div ref={bottomRef} className="h-4" />
      </div>

      {/* Error */}
      {error && (
        <div className="mx-4 mb-2 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded text-sm text-red-400">
          {error}
        </div>
      )}
      {dropError && (
        <div className="mx-4 mb-2 px-3 py-2 bg-yellow-500/10 border border-yellow-500/30 rounded text-sm text-yellow-400">
          {dropError}
        </div>
      )}

      {/* Message Queue Display */}
      {messageQueue.length > 0 && (
        <div className="border-t border-gray-800 bg-gray-900/50 px-4 py-2">
          <div className="max-w-3xl mx-auto">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs text-amber-400 font-medium flex items-center gap-1.5">
                <ListPlus size={12} />
                Queued messages ({messageQueue.length})
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={mergeQueueToInput}
                  className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-amber-300 transition-colors"
                  title="Merge queued messages into input"
                >
                  <Copy size={11} />
                  Merge
                </button>
                <button
                  onClick={() => setMessageQueue([])}
                  className="text-xs text-gray-500 hover:text-red-400 transition-colors"
                >
                  Clear all
                </button>
              </div>
            </div>
            <div className="space-y-1 max-h-32 overflow-y-auto">
              {messageQueue.map((item, idx) => (
                <div key={idx} className="flex items-center gap-1.5 group/q">
                  <span className="text-[10px] text-gray-600 w-4 text-right shrink-0">{idx + 1}</span>
                  <div className="flex-1 min-w-0 bg-gray-800/60 rounded px-2.5 py-1 text-xs text-gray-300 truncate flex items-center gap-1.5">
                    {item.uploadResults && item.uploadResults.length > 0 && (
                      <span className="inline-flex items-center gap-0.5 text-amber-400 shrink-0" title={item.uploadResults.map(r => r.filename).join(', ')}>
                        <Paperclip size={10} />
                        <span className="text-[10px]">{item.uploadResults.length}</span>
                      </span>
                    )}
                    <span className="truncate">{item.text}</span>
                  </div>
                  <div className="flex items-center gap-0.5 opacity-0 group-hover/q:opacity-100 transition-opacity shrink-0">
                    <button
                      onClick={() => editQueueItem(idx)}
                      className="p-0.5 text-gray-500 hover:text-amber-300"
                      title="Edit in input"
                    >
                      <Pencil size={12} />
                    </button>
                    <button
                      onClick={() => moveQueueItem(idx, 'up')}
                      disabled={idx === 0}
                      className="p-0.5 text-gray-500 hover:text-gray-300 disabled:opacity-30"
                      title="Move up"
                    >
                      <ChevronDown size={12} className="rotate-180" />
                    </button>
                    <button
                      onClick={() => moveQueueItem(idx, 'down')}
                      disabled={idx === messageQueue.length - 1}
                      className="p-0.5 text-gray-500 hover:text-gray-300 disabled:opacity-30"
                      title="Move down"
                    >
                      <ChevronDown size={12} />
                    </button>
                    <button
                      onClick={() => removeFromQueue(idx)}
                      className="p-0.5 text-gray-500 hover:text-red-400"
                      title="Remove"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Input */}
      <div className="border-t border-gray-800 bg-gray-900 p-3">
        <div className="flex flex-col gap-2 max-w-3xl mx-auto">
          {/* File preview strip */}
          {pendingFiles.length > 0 && (
            <div className="flex gap-2 flex-wrap">
              {pendingFiles.map((file, idx) => (
                <div key={idx} className="relative rounded overflow-hidden border border-gray-600">
                  {filePreviews[idx] ? (
                    <div className="w-14 h-14">
                      <img src={filePreviews[idx]} alt="" className="w-full h-full object-cover" />
                    </div>
                  ) : (
                    <div className="flex items-center gap-1.5 px-2.5 py-1.5 bg-gray-800 text-xs text-gray-300 max-w-[150px]">
                      <Paperclip size={12} className="shrink-0" />
                      <span className="truncate">{file.name}</span>
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => removeFile(idx)}
                    className="absolute top-0 right-0 bg-gray-900/80 rounded-bl p-0.5 text-gray-300 hover:text-white"
                  >
                    <X size={10} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="space-y-1.5">
          {/* Row 1: action buttons */}
          <div className="flex gap-1 items-center">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={handleFileSelect}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={(!task.session_id && !task.shared_from_id) || pendingFiles.length >= 10}
              className="p-2 text-gray-500 hover:text-gray-300 disabled:opacity-40 disabled:cursor-not-allowed"
              title="Attach files"
            >
              <Paperclip size={18} />
            </button>
            <SecretPicker selectedIds={selectedSecretIds} onChange={setSelectedSecretIds} disabled={!task.session_id && !task.shared_from_id} />
            <QuickPhraseDropdown onSelect={(text) => handleSend(text)} disabled={!task.session_id && !task.shared_from_id} />
            {/* Temp model override (one-shot) */}
            <div className="relative" data-temp-model>
              <button
                type="button"
                onClick={() => setShowModelMenu((v) => !v)}
                disabled={!task.session_id && !task.shared_from_id}
                className={`p-2 rounded-lg transition-colors disabled:opacity-40 ${
                  modelOverride ? 'text-indigo-300 bg-indigo-600/20' : 'text-gray-500 hover:text-gray-300'
                }`}
                title={modelOverride ? `下一条消息用 ${modelOverride}（点击更换）` : '临时切换模型（仅下一条消息）'}
              >
                <ListFilter size={18} />
              </button>
              {showModelMenu && (
                <div className="absolute bottom-full mb-1 left-0 bg-gray-800 border border-gray-600 rounded shadow-lg z-30 min-w-[200px] py-1 max-h-60 overflow-y-auto">
                  <div className="px-3 py-1 text-[10px] text-gray-500 uppercase tracking-wider">下一条消息使用</div>
                  <button
                    onClick={() => { setModelOverride(null); setShowModelMenu(false); }}
                    className={`w-full px-3 py-1.5 text-xs text-left hover:bg-gray-700 ${!modelOverride ? 'text-indigo-300 bg-indigo-600/20' : 'text-gray-300'}`}
                  >
                    默认（{task.model || 'default'}）
                  </button>
                  {modelOptions.map((m) => {
                    // 上下文超过目标模型窗口时给出警告（[1m] 变体 1M，其余按 200K 估算）
                    const win = (m.includes('[1m]') || m.includes('fable')) ? 1_000_000 : 200_000;
                    const over = !!contextUsage && contextUsage.total_input_tokens > win;
                    return (
                    <button
                      key={m}
                      onClick={() => { setModelOverride(m === task.model ? null : m); setShowModelMenu(false); }}
                      className={`w-full px-3 py-1.5 text-xs text-left hover:bg-gray-700 flex items-center justify-between gap-2 ${modelOverride === m ? 'text-indigo-300 bg-indigo-600/20' : over ? 'text-amber-400/80' : 'text-gray-300'}`}
                      title={over ? `当前上下文（${Math.round(contextUsage!.total_input_tokens/1000)}K tokens）可能超出该模型 ${win/1000}K 窗口，会报 Prompt is too long` : undefined}
                    >
                      <span>{m}</span>
                      {over && <span className="shrink-0">⚠</span>}
                    </button>
                  );})}
                </div>
              )}
            </div>
            {/* PTY-only: inject mode toggle — when on, Send delivers via injection */}
            {ptyMode && (
              <button
                type="button"
                onClick={() => setInjectMode((v) => !v)}
                disabled={!task.session_id && !task.shared_from_id}
                className={`p-2 rounded-lg transition-colors disabled:opacity-40 ${
                  injectMode ? 'text-teal-300 bg-teal-600/20' : 'text-gray-500 hover:text-teal-300'
                }`}
                title={injectMode
                  ? '注入模式已开启：发送的消息将注入运行中的 turn（点击关闭）'
                  : '开启注入模式：发送改走 PTY turn 内注入（不开新 turn）'}
              >
                <Syringe size={18} />
              </button>
            )}
            {/* Message navigation — always visible, right-aligned */}
            <div className="ml-auto flex items-center gap-0.5">
              <button
                onClick={() => navigateUserMessage('up')}
                className="p-1.5 text-gray-500 hover:text-gray-300 rounded transition-colors"
                title="Previous user message"
              >
                <ChevronUp size={16} />
              </button>
              <button
                onClick={() => navigateUserMessage('down')}
                className="p-1.5 text-gray-500 hover:text-gray-300 rounded transition-colors"
                title="Next user message"
              >
                <ChevronDown size={16} />
              </button>
              <button
                onClick={() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' })}
                className="p-1.5 text-gray-500 hover:text-gray-300 rounded transition-colors"
                title="Scroll to bottom"
              >
                <ArrowDown size={16} />
              </button>
            </div>
          </div>
          {/* Row 2: full-width input */}
          <div className="flex gap-2 items-end">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                !task.session_id && !task.shared_from_id
                  ? 'Run the task first to start a session...'
                  : injectMode && ptyMode
                    ? '注入模式：消息将直接注入运行中的 turn...'
                    : isProcessing
                      ? 'Type next message to queue...'
                      : 'Type a follow-up message...'
              }
              disabled={!task.session_id && !task.shared_from_id}
              rows={1}
              className="flex-1 bg-gray-800 text-foreground rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none disabled:opacity-50 max-h-48 overflow-y-auto"
              style={{ minHeight: '40px' }}
            />
            <button
              onClick={() => handleSend()}
              disabled={(!input.trim() && pendingFiles.length === 0) || (!task.session_id && !task.shared_from_id) || (injectMode && ptyMode && !isProcessing)}
              title={injectMode && ptyMode
                ? (isProcessing ? '注入到运行中的 turn (Ctrl+Enter)' : '注入模式：仅在 turn 运行中可用，空闲时请关闭注入模式')
                : isProcessing ? 'Add to queue (Ctrl+Enter)' : 'Send (Ctrl+Enter)'}
              className={`p-2.5 text-white rounded-lg disabled:opacity-40 disabled:cursor-not-allowed ${
                injectMode && ptyMode ? 'bg-teal-600 hover:bg-teal-700'
                : isProcessing ? 'bg-amber-600 hover:bg-amber-700' : 'bg-indigo-600 hover:bg-indigo-700'
              }`}
            >
              {injectMode && ptyMode ? <Syringe size={18} /> : isProcessing ? <ListPlus size={18} /> : <Send size={18} />}
            </button>
          </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function CollapsibleContent({ content, maxLines = 5 }: { content: string; maxLines?: number }) {
  const [expanded, setExpanded] = useState(false);
  const lines = content.split('\n');
  const shouldCollapse = lines.length > maxLines;

  if (!shouldCollapse) {
    return (
      <pre className="text-gray-400 whitespace-pre-wrap text-xs overflow-x-auto">{content}</pre>
    );
  }

  return (
    <div>
      <pre className={`text-gray-400 whitespace-pre-wrap text-xs overflow-x-auto ${expanded ? 'max-h-96 overflow-y-auto' : 'max-h-28 overflow-hidden'}`}>
        {content}
      </pre>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-xs text-indigo-400 hover:text-indigo-300 mt-1"
      >
        {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {expanded ? 'Collapse' : `Show all (${lines.length} lines)`}
      </button>
    </div>
  );
}

function formatToolInput(input: string): string {
  try {
    const parsed = JSON.parse(input);
    // For common tools, show a readable format
    if (parsed.command) return parsed.command; // Bash
    if (parsed.file_path && parsed.old_string !== undefined) {
      // Edit tool
      return `File: ${parsed.file_path}\n--- old ---\n${parsed.old_string}\n+++ new +++\n${parsed.new_string}`;
    }
    if (parsed.file_path && parsed.content !== undefined) {
      // Write tool
      return `File: ${parsed.file_path}\n${parsed.content}`;
    }
    if (parsed.file_path) return `File: ${parsed.file_path}`; // Read
    if (parsed.pattern) return `Pattern: ${parsed.pattern}${parsed.path ? ` in ${parsed.path}` : ''}`; // Grep/Glob
    return JSON.stringify(parsed, null, 2);
  } catch {
    return input;
  }
}

/** Extract a short one-line summary for a tool_use message.
 *  In compact mode, tool_input is already a plain summary string from the backend.
 *  In full mode, tool_input is the original JSON. */
function toolUseSummary(msg: ChatMessage): string {
  if (!msg.tool_input) return '';
  // compact mode: backend already returns a plain-text summary (not JSON)
  if (!msg.tool_input.startsWith('{') && !msg.tool_input.startsWith('[')) {
    return msg.tool_input;
  }
  try {
    const parsed = JSON.parse(msg.tool_input);
    if (parsed.command) {
      const cmd = parsed.command as string;
      return cmd.length > 80 ? cmd.slice(0, 80) + '...' : cmd;
    }
    if (parsed.file_path) return parsed.file_path as string;
    if (parsed.pattern) return `${parsed.pattern}${parsed.path ? ` in ${parsed.path}` : ''}`;
  } catch { /* ignore */ }
  return '';
}

function ToolGroup({ messages, taskId }: { messages: ChatMessage[]; taskId: number }) {
  const [expanded, setExpanded] = useState(false);
  const hasError = messages.some((m) => m.is_error);
  const toolUseCount = messages.filter((m) => m.event_type === 'tool_use').length;

  return (
    <div className="mx-4">
      <button
        onClick={() => setExpanded(!expanded)}
        className={`flex items-center gap-1.5 text-xs py-1 hover:text-gray-400 transition-colors ${hasError ? 'text-red-400/70' : 'text-gray-600'}`}
      >
        {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span>
          {hasError ? '⚠' : '🔧'} {toolUseCount} tool call{toolUseCount !== 1 ? 's' : ''}
        </span>
      </button>
      {expanded && (
        <div className="ml-3 border-l border-gray-800 pl-3 space-y-1 mt-1">
          {messages.map((msg) => (
            <ToolItem key={msg.id} message={msg} taskId={taskId} />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolItem({ message, taskId }: { message: ChatMessage; taskId: number }) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const isToolUse = message.event_type === 'tool_use';
  const toolName = message.tool_name || (isToolUse ? 'tool' : 'result');

  // Check if we already have full content (from WebSocket live messages, not compact)
  const hasInlineDetail = isToolUse
    ? !!(message.tool_input && (message.tool_input.startsWith('{') || message.tool_input.startsWith('[')))
    : !!(message.tool_output || message.content);

  const getInlineDetail = (): string | null => {
    if (isToolUse && message.tool_input) return formatToolInput(message.tool_input);
    if (!isToolUse && (message.tool_output || message.content)) return message.tool_output || message.content;
    return message.content || null;
  };

  const handleExpand = async () => {
    if (expanded) {
      setExpanded(false);
      return;
    }
    setExpanded(true);
    if (hasInlineDetail) {
      setDetail(getInlineDetail());
      return;
    }
    // Lazy-load from backend
    if (!detail && !loading) {
      setLoading(true);
      try {
        const d = await api.getMessageDetail(taskId, message.id);
        if (isToolUse && d.tool_input) {
          setDetail(formatToolInput(d.tool_input));
        } else if (!isToolUse && (d.tool_output || d.content)) {
          setDetail(d.tool_output || d.content);
        } else {
          setDetail(d.content || '(empty)');
        }
      } catch {
        setDetail('(failed to load)');
      } finally {
        setLoading(false);
      }
    }
  };

  if (isToolUse) {
    const summary = toolUseSummary(message);
    return (
      <div>
        <button
          onClick={handleExpand}
          className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-400 py-0.5 max-w-full"
        >
          {expanded ? <ChevronDown size={10} className="shrink-0" /> : <ChevronRight size={10} className="shrink-0" />}
          <span className="text-gray-500 font-medium">{toolName}</span>
          {summary && <span className="text-gray-600 truncate">{summary}</span>}
        </button>
        {expanded && (loading
          ? <div className="ml-4 mt-1 mb-1 text-xs text-gray-600">Loading...</div>
          : detail && <div className="ml-4 mt-1 mb-1"><CollapsibleContent content={detail} /></div>
        )}
      </div>
    );
  }

  // tool_result
  const statusIcon = message.is_error ? '✗' : '✓';
  const statusColor = message.is_error ? 'text-red-400' : 'text-green-600';
  return (
    <div>
      <button
        onClick={handleExpand}
        className="flex items-center gap-1.5 text-xs text-gray-600 hover:text-gray-400 py-0.5"
      >
        {expanded ? <ChevronDown size={10} className="shrink-0" /> : <ChevronRight size={10} className="shrink-0" />}
        <span className={statusColor}>{statusIcon}</span>
        <span className="text-gray-600">{toolName}</span>
      </button>
      {expanded && (loading
        ? <div className="ml-4 mt-1 mb-1 text-xs text-gray-600">Loading...</div>
        : detail && <div className="ml-4 mt-1 mb-1"><CollapsibleContent content={detail} /></div>
      )}
    </div>
  );
}

function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  }
  return fallbackCopy(text);
}

function fallbackCopy(text: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      document.execCommand('copy') ? resolve() : reject();
    } catch {
      reject();
    } finally {
      document.body.removeChild(ta);
    }
  });
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    copyToClipboard(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };
  return (
    <button
      onClick={handleCopy}
      className="copy-btn absolute top-2 right-2 p-1 rounded bg-gray-700/80 hover:bg-gray-600 text-gray-400 hover:text-gray-200 opacity-0 group-hover:opacity-100 pointer-events-none group-hover:pointer-events-auto transition-opacity"
      title="Copy"
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}

function MessageCopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    copyToClipboard(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };
  return (
    <button
      onClick={handleCopy}
      className="copy-btn opacity-0 group-hover:opacity-100 pointer-events-none group-hover:pointer-events-auto p-1 rounded hover:bg-gray-700/60 text-gray-600 hover:text-gray-400 transition-opacity"
      title="Copy message"
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
    </button>
  );
}

const remarkPlugins = [remarkGfm];

const markdownComponents: Components = {
  pre({ children }) {
    let codeText = '';
    if (children && typeof children === 'object' && 'props' in (children as React.ReactElement)) {
      const codeEl = children as React.ReactElement<{ children?: React.ReactNode }>;
      codeText = typeof codeEl.props.children === 'string' ? codeEl.props.children : '';
    }
    return (
      <div className="relative group my-2">
        {codeText && <CopyButton text={codeText} />}
        <pre className="bg-gray-900 rounded-lg p-3 overflow-x-auto text-xs">{children}</pre>
      </div>
    );
  },
  code({ className: codeClassName, children, ...props }) {
    const isInline = !codeClassName;
    if (isInline) {
      return <code className="bg-gray-700/60 px-1.5 py-0.5 rounded text-xs" {...props}>{children}</code>;
    }
    return <code className={`${codeClassName || ''} text-xs`} {...props}>{children}</code>;
  },
  a({ href, children }) {
    return <a href={href} target="_blank" rel="noopener noreferrer" className="text-indigo-400 hover:text-indigo-300 underline">{children}</a>;
  },
  table({ children }) {
    return <div className="overflow-x-auto my-2"><table className="border-collapse text-xs w-full">{children}</table></div>;
  },
  th({ children }) {
    return <th className="border border-gray-700 px-2 py-1 bg-gray-800/50 text-left">{children}</th>;
  },
  td({ children }) {
    return <td className="border border-gray-700 px-2 py-1">{children}</td>;
  },
};

const MarkdownContent = memo(function MarkdownContent({ content, className }: { content: string; className?: string }) {
  return (
    <div className={`markdown-body ${className || ''}`}>
    <ReactMarkdown
      remarkPlugins={remarkPlugins}
      components={markdownComponents}
    >
      {content}
    </ReactMarkdown>
    </div>
  );
});

function MessageTimestamp({ timestamp, className }: { timestamp: string | null; className?: string }) {
  if (!timestamp) return null;
  return (
    <span className={`text-[10px] text-gray-600 select-none ${className || ''}`}>
      {formatMessageTime(timestamp)}
    </span>
  );
}

function ImageLightbox({ src, onClose }: { src: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-[9999] bg-black/80 flex items-center justify-center" onClick={onClose}>
      <button onClick={onClose} className="absolute top-4 right-4 text-white/70 hover:text-white text-3xl font-light">&times;</button>
      <img src={src} alt="" className="max-w-[90vw] max-h-[90vh] object-contain rounded-lg" onClick={(e) => e.stopPropagation()} />
    </div>
  );
}

function MessageImages({ urls }: { urls: string[] }) {
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null);
  return (
    <>
      <div className="flex flex-wrap gap-2">
        {urls.map((url, i) => (
          <img
            key={i}
            src={url}
            alt=""
            className="max-w-[200px] max-h-[150px] rounded-lg object-cover cursor-pointer hover:opacity-80 transition-opacity"
            onClick={() => setLightboxSrc(url)}
          />
        ))}
      </div>
      {lightboxSrc && <ImageLightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />}
    </>
  );
}

/** 权限透传卡片：CC 在 PTY 里请求权限 → 用户点 允许/拒绝 回包。
 * CC 侧最多等 120s，超时默认拒绝；过期点击会得到 410 并标记过期。
 * 历史消息没有 request_id（只入库描述），渲染为只读。 */
function PermissionCard({ message, taskId }: { message: ChatMessage; taskId?: number }) {
  const [submitting, setSubmitting] = useState(false);
  const [localStatus, setLocalStatus] = useState<string | null>(null);
  const status = localStatus || message.permission_status || (message.request_id ? 'pending' : 'expired');
  const actionable = status === 'pending' && !!message.request_id && taskId !== undefined;

  const decide = async (behavior: 'allow' | 'deny') => {
    if (!actionable || submitting) return;
    setSubmitting(true);
    try {
      await api.resolvePermission(taskId!, message.request_id!, behavior);
      setLocalStatus(behavior);
    } catch {
      setLocalStatus('expired');
    } finally {
      setSubmitting(false);
    }
  };

  const statusBadge: Record<string, { text: string; cls: string }> = {
    allow: { text: '✓ 已允许', cls: 'text-emerald-400' },
    deny: { text: '✕ 已拒绝', cls: 'text-red-400' },
    expired: { text: '⏱ 已过期（CC 侧默认拒绝）', cls: 'text-gray-500' },
  };

  return (
    <div className="mx-4">
      <div className="px-3 py-2.5 bg-amber-500/10 border border-amber-500/40 rounded-lg text-sm">
        <div className="flex items-center gap-2 text-amber-300 font-medium">
          <span>🔐</span>
          <span>权限请求{message.tool_name ? `：${message.tool_name}` : ''}</span>
          {message.timestamp && (
            <MessageTimestamp timestamp={message.timestamp} className="ml-auto" />
          )}
        </div>
        {message.content && (
          <div className="mt-1 text-gray-300">{message.content}</div>
        )}
        {message.tool_input && (
          <pre className="mt-1.5 px-2 py-1.5 bg-gray-900/60 rounded text-xs text-gray-400 whitespace-pre-wrap break-all max-h-32 overflow-y-auto">{message.tool_input}</pre>
        )}
        <div className="mt-2 flex items-center gap-2">
          {actionable ? (
            <>
              <button
                onClick={() => decide('allow')}
                disabled={submitting}
                className="px-3 py-1 text-xs rounded bg-emerald-600 hover:bg-emerald-500 text-white disabled:opacity-50"
              >
                允许
              </button>
              <button
                onClick={() => decide('deny')}
                disabled={submitting}
                className="px-3 py-1 text-xs rounded bg-red-600/80 hover:bg-red-500 text-white disabled:opacity-50"
              >
                拒绝
              </button>
              <span className="text-xs text-gray-500">120s 内有效，超时默认拒绝</span>
            </>
          ) : (
            <span className={`text-xs ${statusBadge[status]?.cls || 'text-gray-500'}`}>
              {statusBadge[status]?.text || status}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function AskUserCard({ message, taskId }: { message: ChatMessage; taskId?: number }) {
  const questions = message.ask_questions || [];
  const [submitting, setSubmitting] = useState(false);
  const [localStatus, setLocalStatus] = useState<string | null>(null);
  // 每个问题的选中 label 集合 + 自定义文本
  const [selected, setSelected] = useState<Record<number, Set<string>>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});

  const status = localStatus || message.ask_status || (message.request_id ? 'pending' : 'expired');
  const actionable = status === 'pending' && !!message.request_id && taskId !== undefined;

  const toggle = (qi: number, label: string, multi: boolean) => {
    setSelected((prev) => {
      const cur = new Set(prev[qi] || []);
      if (multi) {
        cur.has(label) ? cur.delete(label) : cur.add(label);
      } else {
        cur.clear();
        cur.add(label);
      }
      return { ...prev, [qi]: cur };
    });
  };

  const submit = async () => {
    if (!actionable || submitting) return;
    const answers: AskUserAnswer[] = questions.map((_, qi) => ({
      labels: Array.from(selected[qi] || []),
      text: (custom[qi] || '').trim() || undefined,
    }));
    // 至少一个问题要有答案（label 或自定义文本）
    if (!answers.some((a) => a.labels.length || a.text)) return;
    setSubmitting(true);
    try {
      await api.submitAskUser(taskId!, message.request_id!, answers);
      setLocalStatus('answered');
    } catch {
      setLocalStatus('expired');
    } finally {
      setSubmitting(false);
    }
  };

  const statusBadge: Record<string, { text: string; cls: string }> = {
    answered: { text: '✓ 已回答', cls: 'text-emerald-400' },
    timed_out: { text: '⏱ 已超时（已放行原生工具）', cls: 'text-gray-500' },
    expired: { text: '⏱ 已过期', cls: 'text-gray-500' },
  };

  return (
    <div className="mx-4">
      <div className="px-3 py-2.5 bg-sky-500/10 border border-sky-500/40 rounded-lg text-sm">
        <div className="flex items-center gap-2 text-sky-300 font-medium">
          <span>💬</span>
          <span>需要你的选择</span>
          {message.timestamp && (
            <MessageTimestamp timestamp={message.timestamp} className="ml-auto" />
          )}
        </div>
        {questions.map((q, qi) => {
          const multi = !!q.multiSelect;
          const sel = selected[qi] || new Set<string>();
          return (
            <div key={qi} className="mt-2">
              <div className="text-gray-200">{q.question}</div>
              <div className="mt-1.5 flex flex-col gap-1">
                {q.options.map((opt) => {
                  const checked = sel.has(opt.label);
                  return (
                    <button
                      key={opt.label}
                      onClick={() => actionable && toggle(qi, opt.label, multi)}
                      disabled={!actionable}
                      className={`text-left px-2.5 py-1.5 rounded border text-xs transition-colors disabled:opacity-60 ${
                        checked
                          ? 'bg-sky-600/30 border-sky-500 text-sky-100'
                          : 'bg-gray-900/40 border-gray-700 text-gray-300 hover:border-sky-600/60'
                      }`}
                    >
                      <span className="font-medium">{multi ? (checked ? '☑' : '☐') : (checked ? '◉' : '○')} {opt.label}</span>
                      {opt.description && <span className="text-gray-500"> — {opt.description}</span>}
                    </button>
                  );
                })}
              </div>
              {actionable && (
                <input
                  type="text"
                  value={custom[qi] || ''}
                  onChange={(e) => setCustom((p) => ({ ...p, [qi]: e.target.value }))}
                  placeholder="或自定义回答…"
                  className="mt-1 w-full px-2 py-1 text-xs bg-gray-900/60 border border-gray-700 rounded text-gray-200 placeholder-gray-600 focus:border-sky-600 outline-none"
                />
              )}
            </div>
          );
        })}
        <div className="mt-2.5 flex items-center gap-2">
          {actionable ? (
            <>
              <button
                onClick={submit}
                disabled={submitting}
                className="px-3 py-1 text-xs rounded bg-sky-600 hover:bg-sky-500 text-white disabled:opacity-50"
              >
                提交
              </button>
              <span className="text-xs text-gray-500">提交后回答会喂回给模型继续</span>
            </>
          ) : (
            <span className={`text-xs ${statusBadge[status]?.cls || 'text-gray-500'}`}>
              {statusBadge[status]?.text || status}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

const MessageBubble = memo(function MessageBubble({ message, taskId }: { message: ChatMessage; taskId?: number }) {
  const isUser = message.role === 'user';

  if (message.event_type === 'permission_request') {
    return <PermissionCard message={message} taskId={taskId} />;
  }

  if (message.event_type === 'ask_user_question') {
    return <AskUserCard message={message} taskId={taskId} />;
  }

  if (message.event_type === 'thinking') {
    const text = message.content || '';
    const isEncrypted = text.startsWith('[encrypted thinking');
    return (
      <div className="mx-4 px-3 py-2 bg-gray-800/30 rounded text-xs border border-gray-700/30">
        <div className="flex items-center gap-1.5 text-gray-500">
          <span>💭</span>
          <span className="font-medium">Thinking</span>
          {message.timestamp && (
            <MessageTimestamp timestamp={message.timestamp} className="ml-auto" />
          )}
        </div>
        <div className="mt-1.5">
          {text && !isEncrypted ? (
            <CollapsibleContent content={text} maxLines={20} />
          ) : (
            <span className="text-gray-600 italic">
              {isEncrypted
                ? text
                : '[no thinking text in stream — model may have returned encrypted thinking]'}
            </span>
          )}
        </div>
      </div>
    );
  }

  if (message.event_type === 'transient_retry') {
    return (
      <div className="mx-4">
        <div className="px-3 py-2 bg-amber-500/10 border border-amber-500/30 rounded text-sm text-amber-500 flex items-center gap-2">
          <Loader2 className="w-3.5 h-3.5 shrink-0 animate-spin" />
          <span>{message.content}</span>
        </div>
        {message.timestamp && (
          <div className="mt-0.5 px-1">
            <MessageTimestamp timestamp={message.timestamp} />
          </div>
        )}
      </div>
    );
  }

  if (message.event_type === 'system_init' || message.event_type === 'process_exit' || message.event_type === 'system_event') {
    const content = message.content || 'system';
    const isMonitor = content.startsWith('[Monitor') || content.startsWith('[Agent');
    if (isMonitor) {
      return (
        <div className="bg-gray-800/50 border border-gray-700 rounded-lg px-3 py-2 my-1 text-xs text-gray-400">
          <div className="markdown-body text-xs">
            <ReactMarkdown remarkPlugins={remarkPlugins} components={markdownComponents}>
              {content}
            </ReactMarkdown>
          </div>
          {message.timestamp && <MessageTimestamp timestamp={message.timestamp} className="mt-1" />}
        </div>
      );
    }
    const label = message.event_type === 'system_init'
      ? '— Session started —'
      : message.event_type === 'process_exit'
        ? '— Done —'
        : `— ${content} —`;
    return (
      <div className="text-center text-xs text-gray-600 py-1">
        {label}
        {message.timestamp && (
          <>
            {' '}
            <MessageTimestamp timestamp={message.timestamp} />
          </>
        )}
      </div>
    );
  }

  if (message.is_error) {
    return (
      <div className="mx-4">
        <div className="px-3 py-2 bg-red-500/10 border border-red-500/30 rounded text-sm text-red-400">
          {message.content}
        </div>
        {message.timestamp && (
          <div className="mt-0.5 px-1">
            <MessageTimestamp timestamp={message.timestamp} />
          </div>
        )}
      </div>
    );
  }

  const isMonitor = message.source === 'monitor';
  // 仅用户消息标注注入；回复不标注
  const isInjected = message.source === 'inject' && isUser;

  return (
    <div className={`flex flex-col ${isUser ? 'items-end' : 'items-start'}`} {...(isUser ? { 'data-user-msg': '' } : {})}>
      <div className="max-w-[85%] group">
        {isMonitor && !isUser && (
          <div className="flex items-center gap-1 mb-0.5 pl-1">
            <span className="text-xs bg-teal-600/30 text-teal-300 px-1.5 py-0.5 rounded">Monitor</span>
          </div>
        )}
        {isInjected && (
          <div className="flex items-center gap-1 mb-0.5 pr-1 justify-end">
            <span className="text-xs bg-teal-600/30 text-teal-300 px-1.5 py-0.5 rounded" title="通过 PTY 注入到运行中的 turn">💉 注入</span>
          </div>
        )}
        <div
          className={`rounded-2xl px-4 py-2.5 text-sm ${
            isUser
              ? 'bg-indigo-600 text-white rounded-br-md whitespace-pre-wrap'
              : isMonitor
                ? 'bg-teal-900/40 text-gray-200 rounded-bl-md border border-teal-700/30'
                : 'bg-gray-800 text-gray-200 rounded-bl-md'
          }`}
        >
          {isUser ? (
            <>
              {message.attachments && message.attachments.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-2">
                  {message.attachments.filter((a) => a.is_image).length > 0 && (
                    <MessageImages urls={message.attachments.filter((a) => a.is_image).map((a) => a.url)} />
                  )}
                  {message.attachments.filter((a) => !a.is_image).map((a, i) => (
                    <a key={i} href={a.url} target="_blank" rel="noopener noreferrer"
                      className="flex items-center gap-1.5 px-3 py-1.5 bg-indigo-500/30 rounded-lg text-xs text-indigo-100 hover:bg-indigo-500/40 transition-colors max-w-[200px]"
                    >
                      <Paperclip size={12} className="shrink-0" />
                      <span className="truncate">{a.name}</span>
                    </a>
                  ))}
                </div>
              )}
              {message.image_urls && !message.attachments && message.image_urls.length > 0 && (
                <div className="mb-2">
                  <MessageImages urls={message.image_urls} />
                </div>
              )}
              {message.content && message.content !== '(files attached)' && message.content !== '(images attached)' ? message.content : !message.attachments?.length && !message.image_urls?.length ? message.content || '' : null}
            </>
          ) : (
            <MarkdownContent content={message.content || ''} />
          )}
        </div>
        <div className={`flex items-center gap-1 mt-0.5 ${isUser ? 'justify-end pr-1' : 'pl-1'}`}>
          {message.timestamp && <MessageTimestamp timestamp={message.timestamp} />}
          {message.content && <MessageCopyButton text={message.content} />}
        </div>
      </div>
    </div>
  );
});
