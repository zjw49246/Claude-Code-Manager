import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../../api/client';
import type { ChatMessage, Task, Project, UploadResult } from '../../api/client';
import { useWebSocket } from '../../hooks/useWebSocket';
import { Send, ArrowLeft, Loader2, ChevronDown, ChevronRight, Copy, Check, Paperclip, X, StopCircle } from 'lucide-react';
import { SecretPicker } from '../Secrets/SecretPicker';

interface ChatViewProps {
  task: Task;
  projects: Project[];
  onBack: () => void;
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
  const contextWindow = usage.context_window || 200_000; // default fallback
  const totalUsed = usage.total_input_tokens + usage.output_tokens;
  const percentage = Math.min((totalUsed / contextWindow) * 100, 100);

  // Color based on usage level
  let barColor = 'bg-emerald-500';
  let textColor = 'text-emerald-400';
  if (percentage > 80) {
    barColor = 'bg-red-500';
    textColor = 'text-red-400';
  } else if (percentage > 50) {
    barColor = 'bg-amber-500';
    textColor = 'text-amber-400';
  }

  return (
    <div className="flex items-center gap-2 text-xs shrink-0" title={`Input: ${formatTokenCount(usage.input_tokens)} | Cache read: ${formatTokenCount(usage.cache_read_input_tokens)} | Cache create: ${formatTokenCount(usage.cache_creation_input_tokens)} | Output: ${formatTokenCount(usage.output_tokens)}`}>
      <div className="flex items-center gap-1.5">
        <span className={`${textColor} font-medium`}>{formatTokenCount(totalUsed)}</span>
        <span className="text-gray-600">/</span>
        <span className="text-gray-500">{formatTokenCount(contextWindow)}</span>
      </div>
      <div className="w-16 h-1.5 bg-gray-700 rounded-full overflow-hidden">
        <div className={`h-full ${barColor} rounded-full transition-all duration-300`} style={{ width: `${percentage}%` }} />
      </div>
      <span className={`${textColor} w-8 text-right`}>{percentage.toFixed(0)}%</span>
    </div>
  );
}

export function ChatView({ task, projects, onBack }: ChatViewProps) {
  const projectName = useMemo(() => {
    if (!task.project_id) return null;
    const p = projects.find((p) => p.id === task.project_id);
    return p?.name ?? null;
  }, [task.project_id, projects]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [interrupting, setInterrupting] = useState(false);
  const [stillRunning, setStillRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingImages, setPendingImages] = useState<File[]>([]);
  const [imagePreviews, setImagePreviews] = useState<string[]>([]);
  const [selectedSecretIds, setSelectedSecretIds] = useState<number[]>([]);
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(task.context_window_usage ?? null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Handle real-time WebSocket messages via callback (not state) to avoid
  // losing messages when React batches rapid state updates.
  const handleWsMessage = useCallback((raw: Record<string, unknown>) => {
    const msg = raw as { channel?: string; data?: Record<string, unknown> };
    if (msg.channel !== `task:${task.id}` || !msg.data) return;

    const eventType = msg.data.event_type as string;

    if (eventType === 'process_exit') {
      setSending(false);
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

    // Only show meaningful events in chat (skip user_message - already added optimistically)
    const showTypes = ['message', 'result', 'tool_use', 'tool_result', 'system_init', 'system_event', 'thinking'];
    if (!showTypes.includes(eventType)) return;

    // Skip system heartbeat events (task_progress floods the chat)
    if (eventType === 'system_event' && msg.data.content === 'task_progress') return;

    const content = (msg.data.content as string) || null;
    // Skip empty assistant messages (partial streaming chunks with no text)
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
      loop_iteration: (msg.data.loop_iteration as number) || null,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, entry]);
  }, [task.id]);

  useWebSocket([`task:${task.id}`], handleWsMessage);

  // Reset sending state when task reaches a terminal status
  // (catches cases where process_exit WebSocket event is missed)
  useEffect(() => {
    if (['completed', 'failed', 'cancelled', 'pending'].includes(task.status)) {
      setSending(false);
    }
  }, [task.status]);

  // Load chat history
  useEffect(() => {
    api.getTaskChatHistory(task.id).then((msgs) => {
      // Filter out empty text messages (partial streaming chunks), keep tool/thinking/system events
      setMessages(msgs.filter((m) =>
        !((m.event_type === 'message' || m.event_type === 'result') && !m.content)
      ));
    }).catch(() => {});
  }, [task.id]);

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

  // Auto-scroll only on initial history load
  useEffect(() => {
    if (messages.length > 0 && !hasScrolledRef.current) {
      hasScrolledRef.current = true;
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages]);

  const handleImageSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const combined = [...pendingImages, ...files].slice(0, 5);
    setPendingImages(combined);
    setImagePreviews(combined.map((f) => URL.createObjectURL(f)));
    e.target.value = '';
  };

  const removeImage = (idx: number) => {
    URL.revokeObjectURL(imagePreviews[idx]);
    setPendingImages((prev) => prev.filter((_, i) => i !== idx));
    setImagePreviews((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleSend = async () => {
    const text = input.trim();
    if ((!text && pendingImages.length === 0) || sending) return;

    const snapshotImages = [...pendingImages];
    const snapshotPreviews = [...imagePreviews];

    setInput('');
    snapshotPreviews.forEach((url) => URL.revokeObjectURL(url));
    setPendingImages([]);
    setImagePreviews([]);
    setSending(true);
    setError(null);

    // Optimistically add user message
    const userMsg: ChatMessage = {
      id: Date.now(),
      role: 'user',
      event_type: 'user_message',
      content: text || '(images attached)',
      tool_name: null,
      tool_input: null,
      tool_output: null,
      is_error: false,
      loop_iteration: null,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);

    try {
      let uploadedPaths: string[] | undefined;
      if (snapshotImages.length > 0) {
        const results: UploadResult[] = await api.uploadImages(snapshotImages);
        uploadedPaths = results.map((r) => r.path);
      }
      await api.sendTaskChat(task.id, text || '(images attached)', uploadedPaths, selectedSecretIds.length > 0 ? selectedSecretIds : undefined);
    } catch (e) {
      setSending(false);
      const errMsg = String(e);
      // 409 = task still being processed, show Interrupt button
      if (errMsg.includes('409') || errMsg.toLowerCase().includes('currently being processed')) {
        setStillRunning(true);
      }
      setError(errMsg);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="fixed inset-0 bg-gray-950 flex flex-col z-50">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-2 pt-[max(0.5rem,env(safe-area-inset-top))] border-b border-gray-800 bg-gray-900">
        <button onClick={onBack} className="text-gray-400 hover:text-foreground">
          <ArrowLeft size={20} />
        </button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <p className="text-foreground font-medium text-sm whitespace-nowrap">Task #{task.id}</p>
            {projectName && (
              <span className="text-xs bg-emerald-600/30 text-emerald-300 px-1.5 rounded font-medium whitespace-nowrap">{projectName}</span>
            )}
            <p className="text-xs text-gray-500 whitespace-nowrap">
              {task.session_id ? 'Session active' : 'No session yet'}
            </p>
            {contextUsage && <><span className="flex-1" /><ContextUsageIndicator usage={contextUsage} /></>}
          </div>
          {task.description && (
            <p className="text-sm text-gray-400 truncate">{task.description}</p>
          )}
        </div>
        {(sending || stillRunning || ['in_progress', 'executing'].includes(task.status)) && (
          <button
            onClick={async () => {
              setInterrupting(true);
              try {
                await api.stopTaskSession(task.id);
                setSending(false);
                setStillRunning(false);
                setError(null);
              } catch { /* ignore */ }
              finally { setInterrupting(false); }
            }}
            disabled={interrupting}
            className="flex items-center gap-1 px-2.5 py-1.5 text-xs text-red-400 hover:text-red-300 border border-red-500/30 rounded hover:bg-red-500/10 disabled:opacity-50"
            title="Interrupt session"
          >
            <StopCircle size={14} />
            {interrupting ? 'Interrupting...' : 'Interrupt'}
          </button>
        )}
      </div>

      {/* Interrupting banner */}
      {interrupting && (
        <div className="flex items-center gap-2 px-4 py-2 bg-yellow-500/10 border-b border-yellow-500/30 text-yellow-400 text-xs">
          <Loader2 size={14} className="animate-spin" />
          Interrupting Claude... waiting for graceful shutdown
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <div className="text-center text-gray-600 mt-20">
            <p className="text-lg mb-2">Chat with this task</p>
            <p className="text-sm">
              {task.session_id
                ? 'Send a follow-up message to continue the conversation'
                : 'This task has no session yet. Run it first via Ralph Loop or manually.'}
            </p>
          </div>
        )}
        {grouped.map((group, i) =>
          group.type === 'tool-group' ? (
            <ToolGroup key={i} messages={group.messages} />
          ) : (
            <MessageBubble key={group.message.id} message={group.message} />
          )
        )}
        {sending && (
          <div className="flex gap-2 items-center text-gray-500 text-sm px-3">
            <Loader2 size={14} className="animate-spin" />
            <span>Claude is thinking...</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Error */}
      {error && (
        <div className="mx-4 mb-2 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Input */}
      <div className="border-t border-gray-800 bg-gray-900 p-3">
        <div className="flex flex-col gap-2 max-w-3xl mx-auto">
          {/* Image preview strip */}
          {imagePreviews.length > 0 && (
            <div className="flex gap-2 flex-wrap">
              {imagePreviews.map((src, idx) => (
                <div key={idx} className="relative w-14 h-14 rounded overflow-hidden border border-gray-600">
                  <img src={src} alt="" className="w-full h-full object-cover" />
                  <button
                    type="button"
                    onClick={() => removeImage(idx)}
                    className="absolute top-0 right-0 bg-gray-900/80 rounded-bl p-0.5 text-gray-300 hover:text-white"
                  >
                    <X size={10} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex gap-2 items-end">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              multiple
              className="hidden"
              onChange={handleImageSelect}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={sending || !task.session_id || pendingImages.length >= 5}
              className="p-2.5 text-gray-500 hover:text-gray-300 disabled:opacity-40 disabled:cursor-not-allowed"
              title="Attach images"
            >
              <Paperclip size={18} />
            </button>
            <SecretPicker selectedIds={selectedSecretIds} onChange={setSelectedSecretIds} disabled={sending || !task.session_id} />
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                !task.session_id
                  ? 'Run the task first to start a session...'
                  : sending
                    ? 'Waiting for response...'
                    : 'Type a follow-up message...'
              }
              disabled={sending || !task.session_id}
              rows={1}
              className="flex-1 bg-gray-800 text-foreground rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none disabled:opacity-50 max-h-32"
              style={{ minHeight: '40px' }}
            />
            <button
              onClick={handleSend}
              disabled={(!input.trim() && pendingImages.length === 0) || sending || !task.session_id}
              className="p-2.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Send size={18} />
            </button>
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

/** Extract a short one-line summary for a tool_use message */
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
        <span>
          {hasError ? '⚠' : '🔧'} {toolUseCount} tool call{toolUseCount !== 1 ? 's' : ''}
        </span>
      </button>
      {expanded && (
        <div className="ml-3 border-l border-gray-800 pl-3 space-y-1 mt-1">
          {messages.map((msg) => (
            <ToolItem key={msg.id} message={msg} />
          ))}
        </div>
      )}
    </div>
  );
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
        {expanded && detail && (
          <div className="ml-4 mt-1 mb-1">
            <CollapsibleContent content={detail} />
          </div>
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
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-gray-600 hover:text-gray-400 py-0.5"
      >
        {expanded ? <ChevronDown size={10} className="shrink-0" /> : <ChevronRight size={10} className="shrink-0" />}
        <span className={statusColor}>{statusIcon}</span>
        <span className="text-gray-600">{toolName}</span>
      </button>
      {expanded && detail && (
        <div className="ml-4 mt-1 mb-1">
          <CollapsibleContent content={detail} />
        </div>
      )}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };
  return (
    <button
      onClick={handleCopy}
      className="absolute top-2 right-2 p-1 rounded bg-gray-700/80 hover:bg-gray-600 text-gray-400 hover:text-gray-200 opacity-0 group-hover:opacity-100 transition-opacity"
      title="Copy"
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}

function MarkdownContent({ content, className }: { content: string; className?: string }) {
  return (
    <div className={`markdown-body ${className || ''}`}>
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        pre({ children }) {
          // Extract code string for copy button
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
      }}
    >
      {content}
    </ReactMarkdown>
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';

  if (message.event_type === 'thinking') {
    return (
      <div className="mx-4 px-3 py-2 bg-gray-800/30 rounded text-xs border border-gray-700/30">
        <div className="flex items-center gap-1.5 text-gray-500">
          <span>💭</span>
          <span className="font-medium">Thinking</span>
        </div>
        {message.content && (
          <div className="mt-1.5">
            <CollapsibleContent content={message.content} maxLines={3} />
          </div>
        )}
      </div>
    );
  }

  if (message.event_type === 'system_init' || message.event_type === 'process_exit' || message.event_type === 'system_event') {
    const label = message.event_type === 'system_init'
      ? '— Session started —'
      : message.event_type === 'process_exit'
        ? '— Done —'
        : `— ${message.content || 'system'} —`;
    return (
      <div className="text-center text-xs text-gray-600 py-1">
        {label}
      </div>
    );
  }

  if (message.is_error) {
    return (
      <div className="mx-4 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded text-sm text-red-400">
        {message.content}
      </div>
    );
  }

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm ${
          isUser
            ? 'bg-indigo-600 text-white rounded-br-md whitespace-pre-wrap'
            : 'bg-gray-800 text-gray-200 rounded-bl-md'
        }`}
      >
        {isUser ? (
          message.content || ''
        ) : (
          <MarkdownContent content={message.content || ''} />
        )}
      </div>
    </div>
  );
}
