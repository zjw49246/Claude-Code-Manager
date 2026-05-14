import { useState, useEffect, useRef, useMemo, useCallback, memo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../../api/client';
import type { ChatMessage, FileAttachment, Task, Project, UploadResult } from '../../api/client';
import { useWebSocket } from '../../hooks/useWebSocket';
import { Send, ArrowLeft, Loader2, ChevronDown, ChevronRight, Copy, Check, Paperclip, X, StopCircle, Pencil, ArrowDown } from 'lucide-react';
import { SecretPicker } from '../Secrets/SecretPicker';
import { ExpandableText } from '../ExpandableText';
import { formatMessageTime } from '../../config/timezone';
import { useFileDrop } from '../../hooks/useFileDrop';

interface ChatViewProps {
  task: Task;
  projects: Project[];
  onBack: () => void;
  onTaskUpdated?: () => void;
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

export function ChatView({ task, projects, onBack, onTaskUpdated }: ChatViewProps) {
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
  const [dropError, setDropError] = useState<string | null>(null);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [filePreviews, setFilePreviews] = useState<string[]>([]);
  const [selectedSecretIds, setSelectedSecretIds] = useState<number[]>([]);
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(task.context_window_usage ?? null);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(task.title || '');
  const titleInputRef = useRef<HTMLInputElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [showScrollBottom, setShowScrollBottom] = useState(false);

  // Handle real-time WebSocket messages via callback (not state) to avoid
  // losing messages when React batches rapid state updates.
  const handleWsMessage = useCallback((raw: Record<string, unknown>) => {
    const msg = raw as { channel?: string; data?: Record<string, unknown> };
    if (msg.channel !== `task:${task.id}` || !msg.data) return;

    const eventType = msg.data.event_type as string;

    if (eventType === 'process_exit') {
      // Small delay so any final output messages queued just before
      // process_exit are rendered before the "thinking" indicator hides.
      setTimeout(() => setSending(false), 500);
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
      image_urls: (msg.data.image_urls as string[]) || null,
      attachments: (msg.data.attachments as FileAttachment[]) || null,
    };
    setMessages((prev) => [...prev, entry]);
  }, [task.id]);

  const fetchHistory = useCallback(() => {
    api.getTaskChatHistory(task.id).then((msgs) => {
      // Filter out empty text messages (partial streaming chunks), keep tool/thinking/system events
      setMessages(msgs.filter((m) =>
        !((m.event_type === 'message' || m.event_type === 'result') && !m.content)
      ));
    }).catch(() => {});
  }, [task.id]);

  // Re-fetch history when WebSocket reconnects to pick up any messages
  // that arrived during the disconnection gap
  const handleReconnect = useCallback(() => {
    fetchHistory();
  }, [fetchHistory]);

  useWebSocket([`task:${task.id}`], handleWsMessage, handleReconnect);

  // Reset sending state when task reaches a terminal status
  // (catches cases where process_exit WebSocket event is missed)
  useEffect(() => {
    if (['completed', 'failed', 'cancelled', 'pending'].includes(task.status)) {
      setSending(false);
    }
  }, [task.status]);

  // Load chat history
  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

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

  useEffect(() => {
    const el = messagesContainerRef.current;
    if (!el) return;
    const handleScroll = () => {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      setShowScrollBottom(distanceFromBottom > 300);
    };
    el.addEventListener('scroll', handleScroll, { passive: true });
    return () => el.removeEventListener('scroll', handleScroll);
  }, []);

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

  useFileDrop({
    pendingFiles,
    setPendingFiles,
    setFilePreviews,
    disabled: sending || !task.session_id,
    onError: (msg) => setDropError(msg),
  });

  useEffect(() => {
    if (dropError) {
      const t = setTimeout(() => setDropError(null), 2000);
      return () => clearTimeout(t);
    }
  }, [dropError]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const combined = [...pendingFiles, ...files].slice(0, 5);
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

  const handleSend = async () => {
    const text = input.trim();
    if ((!text && pendingFiles.length === 0) || sending) return;

    const snapshotFiles = [...pendingFiles];
    const snapshotPreviews = [...filePreviews];

    setInput('');
    setPendingFiles([]);
    setFilePreviews([]);
    setSending(true);
    setError(null);

    try {
      let uploadedPaths: string[] | undefined;
      let attachments: FileAttachment[] | undefined;
      if (snapshotFiles.length > 0) {
        const results: UploadResult[] = await api.uploadImages(snapshotFiles);
        uploadedPaths = results.map((r) => r.path);
        attachments = results.map((r) => ({ url: r.url, name: r.filename || r.url.split('/').pop() || 'file', is_image: r.is_image }));
      }
      snapshotPreviews.forEach((url) => { if (url) URL.revokeObjectURL(url); });

      const userMsg: ChatMessage = {
        id: Date.now(),
        role: 'user',
        event_type: 'user_message',
        content: text || '(files attached)',
        tool_name: null,
        tool_input: null,
        tool_output: null,
        is_error: false,
        loop_iteration: null,
        timestamp: new Date().toISOString(),
        image_urls: attachments?.filter((a) => a.is_image).map((a) => a.url) || null,
        attachments: attachments || null,
      };
      setMessages((prev) => [...prev, userMsg]);

      await api.sendTaskChat(task.id, text || '(files attached)', uploadedPaths, selectedSecretIds.length > 0 ? selectedSecretIds : undefined);
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
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) {
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
            {contextUsage && <><span className="flex-1" /><span className="hidden sm:flex"><ContextUsageIndicator usage={contextUsage} /></span></>}
          </div>
          {editingTitle ? (
            <input
              ref={titleInputRef}
              autoFocus
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onBlur={handleTitleSave}
              onKeyDown={(e) => { if (e.key === 'Enter') handleTitleSave(); if (e.key === 'Escape') { setTitleDraft(task.title || ''); setEditingTitle(false); } }}
              className="w-full bg-gray-800 text-foreground text-sm rounded px-2 py-0.5 mt-0.5 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              placeholder="Enter title..."
            />
          ) : (
            <div className="flex items-center gap-1.5 mt-0.5 group/title">
              <ExpandableText
                text={task.title || task.description || 'Untitled'}
                collapsedLines={1}
                className="text-sm text-gray-400"
              />
              <button
                onClick={() => { setTitleDraft(task.title || ''); setEditingTitle(true); }}
                className="text-gray-600 hover:text-gray-400 opacity-0 group-hover/title:opacity-100 transition-opacity shrink-0"
                title="Edit title"
              >
                <Pencil size={12} />
              </button>
            </div>
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
              } catch (e) {
                setError(`Interrupt failed: ${e instanceof Error ? e.message : String(e)}`);
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

      {/* Interrupting banner */}
      {interrupting && (
        <div className="flex items-center gap-2 px-4 py-2 bg-yellow-500/10 border-b border-yellow-500/30 text-yellow-400 text-xs">
          <Loader2 size={14} className="animate-spin" />
          Interrupting Claude... waiting for graceful shutdown
        </div>
      )}

      {/* Messages */}
      <div ref={messagesContainerRef} className="flex-1 overflow-y-auto p-4 space-y-3 min-h-0">
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
        {/* Initial prompt bubble */}
        {task.description && (
          <div>
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
                  <MessageCopyButton text={task.description} />
                </div>
              </div>
            </div>
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
      {showScrollBottom && (
        <button
          onClick={() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' })}
          className="absolute bottom-28 right-6 z-10 p-2.5 bg-gray-700 hover:bg-gray-600 text-gray-300 hover:text-white rounded-full shadow-lg transition-all"
          title="Scroll to bottom"
        >
          <ArrowDown size={18} />
        </button>
      )}

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
          <div className="flex gap-2 items-end">
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
              disabled={sending || !task.session_id || pendingFiles.length >= 5}
              className="p-2.5 text-gray-500 hover:text-gray-300 disabled:opacity-40 disabled:cursor-not-allowed"
              title="Attach files"
            >
              <Paperclip size={18} />
            </button>
            <SecretPicker selectedIds={selectedSecretIds} onChange={setSelectedSecretIds} disabled={sending || !task.session_id} />
            <textarea
              ref={textareaRef}
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
              className="flex-1 bg-gray-800 text-foreground rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 resize-none disabled:opacity-50 max-h-48 overflow-y-auto"
              style={{ minHeight: '40px' }}
            />
            <button
              onClick={handleSend}
              disabled={(!input.trim() && pendingFiles.length === 0) || sending || !task.session_id}
              title="Send (Ctrl+Enter)"
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

const MessageBubble = memo(function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';

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

  if (message.event_type === 'system_init' || message.event_type === 'process_exit' || message.event_type === 'system_event') {
    const label = message.event_type === 'system_init'
      ? '— Session started —'
      : message.event_type === 'process_exit'
        ? '— Done —'
        : `— ${message.content || 'system'} —`;
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

  return (
    <div className={`flex flex-col ${isUser ? 'items-end' : 'items-start'}`}>
      <div className="max-w-[85%] group">
        <div
          className={`rounded-2xl px-4 py-2.5 text-sm ${
            isUser
              ? 'bg-indigo-600 text-white rounded-br-md whitespace-pre-wrap'
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
