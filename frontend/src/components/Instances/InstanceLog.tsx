import { useState, useEffect, useRef } from 'react';
import { api } from '../../api/client';
import type { LogEntry } from '../../api/client';
import { useWebSocket } from '../../hooks/useWebSocket';
import { X } from '../icons';

interface InstanceLogProps {
  instanceId: number;
  onClose: () => void;
}

export function InstanceLog({ instanceId, onClose }: InstanceLogProps) {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const { lastMessage } = useWebSocket([`instance:${instanceId}`]);

  useEffect(() => {
    api.getInstanceLogs(instanceId, 200).then((entries) => {
      setLogs(entries.reverse());
    });
  }, [instanceId]);

  useEffect(() => {
    if (lastMessage) {
      const msg = lastMessage as { channel?: string; data?: Record<string, unknown> };
      if (msg.channel === `instance:${instanceId}` && msg.data) {
        const entry: LogEntry = {
          id: Date.now(),
          instance_id: instanceId,
          task_id: null,
          event_type: (msg.data.event_type as string) || 'unknown',
          role: (msg.data.role as string) || null,
          content: (msg.data.content as string) || null,
          tool_name: (msg.data.tool_name as string) || null,
          is_error: (msg.data.is_error as boolean) || false,
          timestamp: new Date().toISOString(),
        };
        setLogs((prev) => [...prev, entry]);
      }
    }
  }, [lastMessage, instanceId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  const typeColors: Record<string, string> = {
    message: 'text-gray-300',
    tool_use: 'text-blue-400',
    tool_result: 'text-green-400',
    result: 'text-indigo-400',
    process_exit: 'text-yellow-400',
    parse_error: 'text-red-400',
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-900 rounded-lg w-full max-w-3xl max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700">
          <h3 className="text-foreground font-medium text-sm">Instance #{instanceId} Logs</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-foreground">
            <X size={18} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-4 font-mono text-xs space-y-1">
          {logs.length === 0 && <p className="text-gray-500">No logs yet</p>}
          {logs.map((log) => (
            <div key={log.id} className={`${log.is_error ? 'text-red-400' : typeColors[log.event_type] || 'text-gray-400'}`}>
              <span className="text-gray-600 mr-2">{new Date(log.timestamp).toLocaleTimeString()}</span>
              <span className="text-gray-500 mr-2">[{log.event_type}]</span>
              {log.tool_name && <span className="text-blue-300 mr-2">{log.tool_name}</span>}
              <span>{log.content || ''}</span>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
}
