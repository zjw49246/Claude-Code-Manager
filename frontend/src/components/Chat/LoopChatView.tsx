import { useState, useEffect, useRef, useMemo, useCallback, memo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../../api/client';
import type { ChatMessage, Task } from '../../api/client';
import { useWebSocket } from '../../hooks/useWebSocket';
import { ArrowLeft, ChevronDown, ChevronRight, Copy, Check, XCircle, ArrowDown } from 'lucide-react';

interface LoopChatViewProps {
  task: Task;
  onBack: () => void;
}

// Iteration metadata received from loop_iteration_end WebSocket events
interface IterationMeta {
  action: 'continue' | 'done' | 'abort';
  reason: string;
  progress: string | null;
}

function groupByIteration(messages: ChatMessage[]): Map<number, ChatMessage[]> {
  const map = new Map<number, ChatMessage[]>();
  for (const msg of messages) {
    const iter = msg.loop_iteration ?? 0;
    if (!map.has(iter)) map.set(iter, []);
    map.get(iter)!.push(msg);
  }
  return map;
}

type MessageGroup =
  | { type: 'tool-group'; messages: ChatMessage[] }
  | { type: 'single'; message: ChatMessage };

function groupMessages(messages: ChatMessage[]): MessageGroup[] {
  const groups: MessageGroup[] = [];
  let toolBuf: ChatMessage[] = [];

  const flush = () => {
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
      flush();
      groups.push({ type: 'single', message: msg });
    }
  }
  flush();
  return groups;
}

// ─── Small reusable rendering components ─────────────────────────────────────

function CollapsibleContent({ content, maxLines = 5 }: { content: string; maxLines?: number }) {
  const [expanded, setExpanded] = useState(false);
  const lines = content.split('\n');
  if (lines.length <= maxLines) {
    return <pre className="text-gray-400 whitespace-pre-wrap text-xs overflow-x-auto">{content}</pre>;
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
    if (parsed.command) return parsed.command;
    if (parsed.file_path && parsed.old_string !== undefined)
      return `File: ${parsed.file_path}\n--- old ---\n${parsed.old_string}\n+++ new +++\n${parsed.new_string}`;
    if (parsed.file_path && parsed.content !== undefined)
      return `File: ${parsed.file_path}\n${parsed.content}`;
    if (parsed.file_path) return `File: ${parsed.file_path}`;
    if (parsed.pattern) return `Pattern: ${parsed.pattern}${parsed.path ? ` in ${parsed.path}` : ''}`;
    return JSON.stringify(parsed, null, 2);
  } catch {
    return input;
  }
}

function toolUseSummary(msg: ChatMessage): string {
  if (!msg.tool_input) return '';
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

function ToolItem({ message }: { message: ChatMessage }) {
  const [expanded, setExpanded] = useState(false);
  const isToolUse = message.event_type === 'tool_use';
  const toolName = message.tool_name || (isToolUse ? 'tool' : 'result');

  let detail: string | null = null;
  if (isToolUse && message.tool_input) {
    detail = formatToolInput(message.tool_input);
  } else if (!isToolUse && (message.tool_output || message.content)) {
    detail = message.tool_output || message.content;
  } else if (message.content) {
    detail = message.content;
  }

  if (isToolUse) {
    const summary = toolUseSummary(message);
    return (
      <div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-400 py-0.5 max-w-full"
        >
          {expanded ? <ChevronDown size={10} className="shrink-0" /> : <ChevronRight size={10} className="shrink-0" />}
          <span className="text-gray-500 font-medium">{toolName}</span>
          {summary && <span className="text-gray-600 truncate">{summary}</span>}
        </button>
        {expanded && detail && <div className="ml-4 mt-1 mb-1"><CollapsibleContent content={detail} /></div>}
      </div>
    );
  }

  const statusColor = message.is_error ? 'text-red-400' : 'text-green-600';
  return (
    <div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-gray-600 hover:text-gray-400 py-0.5"
      >
        {expanded ? <ChevronDown size={10} className="shrink-0" /> : <ChevronRight size={10} className="shrink-0" />}
        <span className={statusColor}>{message.is_error ? '✗' : '✓'}</span>
        <span className="text-gray-600">{toolName}</span>
      </button>
      {expanded && detail && <div className="ml-4 mt-1 mb-1"><CollapsibleContent content={detail} /></div>}
    </div>
  );
}

function ToolGroup({ messages }: { messages: ChatMessage[] }) {
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
        {hasError ? '⚠' : '🔧'} {toolUseCount} tool call{toolUseCount !== 1 ? 's' : ''}
      </button>
      {expanded && (
        <div className="ml-3 border-l border-gray-800 pl-3 space-y-1 mt-1">
          {messages.map((msg) => <ToolItem key={msg.id} message={msg} />)}
        </div>
      )}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => { navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); }); }}
      className="absolute top-2 right-2 p-1 rounded bg-gray-700/80 hover:bg-gray-600 text-gray-400 hover:text-gray-200 opacity-0 group-hover:opacity-100 transition-opacity"
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}

const remarkPlugins = [remarkGfm];

const markdownComponents: Components = {
  pre({ children }) {
    let codeText = '';
    if (children && typeof children === 'object' && 'props' in (children as React.ReactElement)) {
      const el = children as React.ReactElement<{ children?: React.ReactNode }>;
      codeText = typeof el.props.children === 'string' ? el.props.children : '';
    }
    return (
      <div className="relative group my-2">
        {codeText && <CopyButton text={codeText} />}
        <pre className="bg-gray-900 rounded-lg p-3 overflow-x-auto text-xs">{children}</pre>
      </div>
    );
  },
  code({ className: codeClassName, children, ...props }) {
    if (!codeClassName) return <code className="bg-gray-700/60 px-1.5 py-0.5 rounded text-xs" {...props}>{children}</code>;
    return <code className={`${codeClassName} text-xs`} {...props}>{children}</code>;
  },
  a({ href, children }) {
    return <a href={href} target="_blank" rel="noopener noreferrer" className="text-indigo-400 hover:text-indigo-300 underline">{children}</a>;
  },
};

const MarkdownContent = memo(function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});

const MessageBubble = memo(function MessageBubble({ message }: { message: ChatMessage }) {
  if (message.event_type === 'thinking') {
    return (
      <div className="mx-4 px-3 py-2 bg-gray-800/30 rounded text-xs border border-gray-700/30">
        <div className="flex items-center gap-1.5 text-gray-500"><span>💭</span><span className="font-medium">Thinking</span></div>
        {message.content && <div className="mt-1.5"><CollapsibleContent content={message.content} maxLines={3} /></div>}
      </div>
    );
  }
  if (['system_init', 'process_exit', 'system_event'].includes(message.event_type)) {
    const label = message.event_type === 'system_init' ? '— Session started —'
      : message.event_type === 'process_exit' ? '— Done —'
      : `— ${message.content || 'system'} —`;
    return <div className="text-center text-xs text-gray-600 py-1">{label}</div>;
  }
  if (message.is_error) {
    return (
      <div className="mx-4 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded text-sm text-red-400">
        {message.content}
      </div>
    );
  }
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] rounded-2xl rounded-bl-md px-4 py-2.5 text-sm bg-gray-800 text-gray-200">
        <MarkdownContent content={message.content || ''} />
      </div>
    </div>
  );
});

// ─── Iteration panel ──────────────────────────────────────────────────────────

interface IterationPanelProps {
  iteration: number;
  messages: ChatMessage[];
  meta: IterationMeta | null;
  isActive: boolean;       // currently running — default open, live scroll
  defaultOpen: boolean;
}

function IterationPanel({ iteration, messages, meta, isActive, defaultOpen }: IterationPanelProps) {
  const [open, setOpen] = useState(defaultOpen || isActive);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isActive) bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isActive]);

  const statusIcon = isActive ? '⟳' : meta?.action === 'done' ? '✓' : meta?.action === 'abort' ? '✗' : '✓';
  const statusColor = isActive ? 'text-blue-400' : meta?.action === 'abort' ? 'text-red-400' : 'text-green-500';

  const grouped = useMemo(() => groupMessages(messages), [messages]);

  return (
    <div className="border border-gray-700/50 rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-4 py-2.5 bg-gray-800/60 hover:bg-gray-800 text-left transition-colors"
      >
        {open ? <ChevronDown size={14} className="text-gray-500 shrink-0" /> : <ChevronRight size={14} className="text-gray-500 shrink-0" />}
        <span className="text-xs font-semibold text-gray-400">Iteration {iteration + 1}</span>
        <span className={`text-xs font-medium ${statusColor}`}>{statusIcon} {isActive ? 'running' : (meta?.action ?? '')}</span>
        {(meta?.progress || meta?.reason) && (
          <span className="text-xs text-gray-500 truncate ml-auto">
            {meta.progress && <span className="mr-1.5">{meta.progress}</span>}
            {meta.reason}
          </span>
        )}
      </button>
      {open && (
        <div className="p-3 space-y-2 bg-gray-900/30">
          {grouped.map((group, i) =>
            group.type === 'tool-group' ? (
              <ToolGroup key={i} messages={group.messages} />
            ) : (
              <MessageBubble key={group.message.id} message={group.message} />
            )
          )}
          {isActive && (
            <div className="text-xs text-gray-500 px-4 animate-pulse">Claude is working...</div>
          )}
          <div ref={bottomRef} />
        </div>
      )}
    </div>
  );
}

// ─── Main view ────────────────────────────────────────────────────────────────

export function LoopChatView({ task, onBack }: LoopChatViewProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [iterMeta, setIterMeta] = useState<Map<number, IterationMeta>>(new Map());
  const [activeIteration, setActiveIteration] = useState<number | null>(null);
  const [showScrollBottom, setShowScrollBottom] = useState(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const scrollBottomRef = useRef<HTMLDivElement>(null);
  const historyLoadedRef = useRef(false);
  const pendingWsRef = useRef<ChatMessage[]>([]);

  const handleWsMessage = useCallback((raw: Record<string, unknown>) => {
    const msg = raw as { channel?: string; data?: Record<string, unknown> };
    if (msg.channel !== `task:${task.id}` || !msg.data) return;

    const eventType = msg.data.event_type as string;

    // Capture loop_iteration_end to update panel headers
    if (eventType === undefined && (msg.data as Record<string, unknown>).event === 'loop_iteration_end') {
      const d = msg.data as { iteration: number; action: string; reason: string; progress: string | null };
      setIterMeta((prev) => {
        const next = new Map(prev);
        next.set(d.iteration, { action: d.action as IterationMeta['action'], reason: d.reason, progress: d.progress });
        return next;
      });
      if (d.action === 'continue') setActiveIteration(d.iteration + 1);
      else setActiveIteration(null);
      return;
    }

    if (eventType === 'process_exit') {
      return;
    }

    const showTypes = ['message', 'result', 'tool_use', 'tool_result', 'system_init', 'system_event', 'thinking'];
    if (!showTypes.includes(eventType)) return;
    if (eventType === 'system_event' && msg.data.content === 'task_progress') return;
    const content = (msg.data.content as string) || null;
    if ((eventType === 'message' || eventType === 'result') && !content) return;

    const entry: ChatMessage = {
      id: Date.now() + Math.random(),
      role: (msg.data.role as string) || 'assistant',
      event_type: eventType,
      content,
      tool_name: (msg.data.tool_name as string) || null,
      tool_input: (msg.data.tool_input as string) || null,
      tool_output: (msg.data.tool_output as string) || null,
      is_error: (msg.data.is_error as boolean) || false,
      loop_iteration: (msg.data.loop_iteration as number) ?? 0,
      timestamp: new Date().toISOString(),
      image_urls: null,
      attachments: null,
    };

    if (!historyLoadedRef.current) {
      pendingWsRef.current.push(entry);
      return;
    }
    setMessages((prev) => [...prev, entry]);
  }, [task.id]);

  useWebSocket([`task:${task.id}`], handleWsMessage);

  useEffect(() => {
    historyLoadedRef.current = false;
    pendingWsRef.current = [];
    api.getTaskChatHistory(task.id).then((msgs) => {
      const filtered = msgs.filter((m) =>
        !((m.event_type === 'message' || m.event_type === 'result') && !m.content)
      );
      const maxHistoryId = filtered.length > 0
        ? Math.max(...filtered.map((m) => m.id))
        : 0;
      const fresh = pendingWsRef.current.filter((m) => m.id > maxHistoryId);
      setMessages([...filtered, ...fresh]);
      pendingWsRef.current = [];
      historyLoadedRef.current = true;
      if (['executing', 'in_progress'].includes(task.status)) {
        const allMsgs = [...filtered, ...fresh];
        const maxIter = allMsgs.reduce((acc, m) => Math.max(acc, m.loop_iteration ?? 0), 0);
        setActiveIteration(maxIter);
      }
    }).catch(() => {});
  }, [task.id, task.status]);

  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const handleScroll = () => {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      setShowScrollBottom(distanceFromBottom > 300);
    };
    el.addEventListener('scroll', handleScroll, { passive: true });
    return () => el.removeEventListener('scroll', handleScroll);
  }, []);

  const iterationGroups = useMemo(() => {
    const map = groupByIteration(messages);
    return [...map.entries()].sort(([a], [b]) => a - b);
  }, [messages]);

  const maxIteration = iterationGroups.length > 0 ? iterationGroups[iterationGroups.length - 1][0] : 0;

  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const handleCancel = async () => {
    setCancelling(true);
    setCancelError(null);
    try {
      await api.cancelTask(task.id);
    } catch (e) {
      setCancelError(`Cancel failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setCancelling(false);
    }
  };

  const isRunning = ['executing', 'in_progress'].includes(task.status);

  return (
    <div className="fixed inset-0 bg-gray-950 flex flex-col z-50">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 pt-[max(0.75rem,env(safe-area-inset-top))] border-b border-gray-800 bg-gray-900">
        <button onClick={onBack} className="text-gray-400 hover:text-foreground">
          <ArrowLeft size={20} />
        </button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <p className="text-foreground font-medium text-sm">Task #{task.id}</p>
            <span className="text-xs bg-indigo-600/20 text-indigo-400 px-1.5 rounded">Loop</span>
          </div>
          <p className="text-xs text-gray-500 truncate">
            {task.todo_file_path}
            {task.loop_progress && <span className="ml-2 text-indigo-400">{task.loop_progress}</span>}
          </p>
        </div>
      </div>

      {/* Iteration panels */}
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {iterationGroups.length === 0 && (
          <div className="text-center text-gray-600 mt-20">
            <p className="text-lg mb-2">Loop task</p>
            <p className="text-sm">Iterations will appear here as Claude works through the todo list.</p>
          </div>
        )}
        {iterationGroups.map(([iter, msgs]) => (
          <IterationPanel
            key={iter}
            iteration={iter}
            messages={msgs}
            meta={iterMeta.get(iter) ?? null}
            isActive={isRunning && iter === (activeIteration ?? maxIteration)}
            defaultOpen={iter === maxIteration}
          />
        ))}
        <div ref={scrollBottomRef} />
      </div>
      {showScrollBottom && (
        <button
          onClick={() => scrollBottomRef.current?.scrollIntoView({ behavior: 'smooth' })}
          className="absolute bottom-28 right-6 z-10 p-2.5 bg-gray-700 hover:bg-gray-600 text-gray-300 hover:text-white rounded-full shadow-lg transition-all"
          title="Scroll to bottom"
        >
          <ArrowDown size={18} />
        </button>
      )}

      {/* Footer */}
      {isRunning && (
        <div className="border-t border-gray-800 bg-gray-900 p-3 flex flex-col items-center gap-2">
          {cancelError && (
            <p className="text-xs text-red-400">{cancelError}</p>
          )}
          <button
            onClick={handleCancel}
            disabled={cancelling}
            className="flex items-center gap-2 px-4 py-2 rounded text-sm font-medium bg-red-600/20 text-red-400 hover:bg-red-600/30 disabled:opacity-50"
          >
            <XCircle size={16} /> {cancelling ? 'Cancelling...' : 'Cancel Loop'}
          </button>
        </div>
      )}
    </div>
  );
}
