import { useState, useEffect, useRef, useCallback } from 'react';
import { api } from '../../api/client';
import type { SharedTaskReceived } from '../../api/client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ArrowLeft, Send, RefreshCw, Wifi, WifiOff } from 'lucide-react';

interface SharedChatViewProps {
  shared: SharedTaskReceived;
  onBack: () => void;
}

interface ChatMsg {
  id: number;
  role: string;
  event_type: string;
  content: string | null;
  tool_name?: string;
  tool_input?: string;
  tool_output?: string;
  is_error?: boolean;
  timestamp?: string;
}

export function SharedChatView({ shared, onBack }: SharedChatViewProps) {
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [config, setConfig] = useState<any>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  // Load history and config
  useEffect(() => {
    (async () => {
      try {
        const [history, cfg] = await Promise.all([
          api.getSharedHistory(shared.id),
          api.getSharedConfig(shared.id),
        ]);
        setMessages(history);
        setConfig(cfg);
        setError(null);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, [shared.id]);

  // Connect WebSocket directly to the sharer's CCM
  useEffect(() => {
    if (!shared.owner_ccm_url) return;

    const wsUrl = shared.owner_ccm_url
      .replace(/^http/, 'ws')
      + `/ws/shared?token=${encodeURIComponent(shared.share_token)}&task_id=${shared.remote_task_id}`;

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => setWsConnected(true);
    ws.onclose = () => setWsConnected(false);
    ws.onerror = () => setWsConnected(false);

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.action === 'subscribed') return;

        if (data.event_type) {
          const msg: ChatMsg = {
            id: Date.now(),
            role: data.role || 'assistant',
            event_type: data.event_type,
            content: data.content || null,
            tool_name: data.tool_name,
            is_error: data.is_error,
          };
          setMessages(prev => [...prev, msg]);
        }
      } catch { /* ignore */ }
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [shared.owner_ccm_url, shared.remote_task_id]);

  // Fallback polling when WS is not connected
  useEffect(() => {
    if (wsConnected) return;
    const interval = setInterval(async () => {
      try {
        const history = await api.getSharedHistory(shared.id);
        setMessages(history);
      } catch { /* ignore */ }
    }, 3000);
    return () => clearInterval(interval);
  }, [wsConnected, shared.id]);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;

    setSending(true);
    setInput('');

    // Optimistic local message
    setMessages(prev => [...prev, {
      id: Date.now(),
      role: 'user',
      event_type: 'user_message',
      content: text,
    }]);

    try {
      await api.sendSharedChat(shared.id, text);
    } catch (e) {
      setError(String(e));
    } finally {
      setSending(false);
    }
  };

  const refresh = async () => {
    setLoading(true);
    try {
      const history = await api.getSharedHistory(shared.id);
      setMessages(history);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  const status = config?.status || 'unknown';
  const statusColor = status === 'executing' ? 'text-blue-400' : status === 'completed' ? 'text-green-400' : 'text-gray-400';

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-700 bg-gray-800/50">
        <button onClick={onBack} className="text-gray-400 hover:text-gray-200">
          <ArrowLeft size={20} />
        </button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-foreground font-medium truncate">
              {shared.task_title || `Task #${shared.remote_task_id}`}
            </h2>
            <span className={`text-xs ${statusColor}`}>{status}</span>
          </div>
          <p className="text-xs text-gray-500 truncate">
            Shared by {shared.owner_name || 'Unknown'}
            {shared.project_name && ` · ${shared.project_name}`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {wsConnected ? (
            <Wifi size={14} className="text-green-400" />
          ) : (
            <WifiOff size={14} className="text-gray-500" />
          )}
          <button onClick={refresh} disabled={loading} className="text-gray-400 hover:text-gray-200">
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {error && <p className="px-4 py-2 text-red-400 text-sm bg-red-900/20">{error}</p>}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
        {loading && messages.length === 0 && (
          <p className="text-gray-500 text-sm text-center py-8">Loading history...</p>
        )}
        {messages.map((msg) => (
          <MessageRow key={msg.id} msg={msg} />
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="px-4 py-3 border-t border-gray-700 bg-gray-800/50">
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && handleSend()}
            placeholder="Send a message..."
            className="flex-1 bg-gray-700 text-foreground rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
            disabled={sending}
          />
          <button
            onClick={handleSend}
            disabled={sending || !input.trim()}
            className="px-3 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}

function MessageRow({ msg }: { msg: ChatMsg }) {
  if (msg.event_type === 'user_message') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] bg-blue-600/20 border border-blue-500/20 rounded-xl px-4 py-2">
          <p className="text-sm text-foreground whitespace-pre-wrap">{msg.content}</p>
        </div>
      </div>
    );
  }

  if (msg.event_type === 'tool_use' || msg.event_type === 'tool_result') {
    return (
      <div className="text-xs text-gray-500 px-2 py-1 bg-gray-800/50 rounded font-mono">
        {msg.event_type === 'tool_use' ? `🔧 ${msg.tool_name || 'tool'}` : '📋 result'}
        {msg.tool_output && <span className="ml-2 text-gray-600">{msg.tool_output.slice(0, 100)}</span>}
      </div>
    );
  }

  if (msg.event_type === 'system_event' || msg.event_type === 'system_init') {
    return (
      <div className="text-xs text-gray-600 text-center py-1">
        {msg.content}
      </div>
    );
  }

  // Assistant message
  if (msg.role === 'assistant' && msg.content) {
    return (
      <div className="max-w-[90%]">
        <div className="bg-gray-800 border border-gray-700 rounded-xl px-4 py-2">
          <div className="markdown-body text-sm">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
          </div>
        </div>
      </div>
    );
  }

  if (msg.content) {
    return (
      <div className="text-sm text-gray-300 px-2 py-1">
        {msg.content}
      </div>
    );
  }

  return null;
}
