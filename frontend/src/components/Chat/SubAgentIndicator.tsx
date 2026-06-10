import { useState, useEffect, useRef } from 'react';
import { Users } from 'lucide-react';

interface SubAgentIndicatorProps {
  count: number;
  active?: boolean;
  onNavigate?: () => void;
}

export function SubAgentIndicator({ count, active, onNavigate }: SubAgentIndicatorProps) {
  const [expanded, setExpanded] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!expanded) return;
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setExpanded(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [expanded]);

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        onClick={() => setExpanded(!expanded)}
        className={`text-xs bg-teal-600/30 text-teal-300 px-1.5 rounded cursor-pointer hover:bg-teal-600/40 flex items-center gap-0.5${active ? ' animate-pulse' : ''}`}
      >
        <Users size={12} />
        {count}
      </button>

      {expanded && (
        <div className="absolute top-full right-0 mt-1 bg-gray-800 border border-gray-600 rounded shadow-lg z-10 min-w-[100px]">
          <button
            className="flex items-center justify-between w-full px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 transition-colors"
            onClick={() => { onNavigate?.(); setExpanded(false); }}
          >
            <span>Monitor</span>
            <span className="text-emerald-400 font-medium">{count}</span>
          </button>
        </div>
      )}
    </div>
  );
}
