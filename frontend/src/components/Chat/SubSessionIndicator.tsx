import { useState, useMemo } from 'react';
import { ChevronDown, ChevronUp, Activity } from 'lucide-react';

interface SubSessionCounts {
  monitor: number;
}

interface SubSessionIndicatorProps {
  counts: SubSessionCounts;
  onNavigate?: (skill: keyof SubSessionCounts) => void;
}

export function SubSessionIndicator({ counts, onNavigate }: SubSessionIndicatorProps) {
  const [expanded, setExpanded] = useState(false);

  const total = useMemo(
    () => Object.values(counts).reduce((sum, n) => sum + n, 0),
    [counts]
  );

  if (total === 0) return null;

  return (
    <div className="relative inline-block">
      <button
        className="flex items-center gap-1.5 px-2 py-1 text-xs text-gray-400 hover:text-gray-200 bg-gray-700/50 hover:bg-gray-700 rounded transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <Activity size={14} className="text-emerald-400" />
        <span>{total} sub-session{total !== 1 ? 's' : ''}</span>
        {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {expanded && (
        <div className="absolute top-full left-0 mt-1 bg-gray-800 border border-gray-600 rounded shadow-lg z-10 min-w-[140px]">
          {counts.monitor > 0 && (
            <button
              className="flex items-center justify-between w-full px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 transition-colors"
              onClick={() => onNavigate?.('monitor')}
            >
              <span>Monitor</span>
              <span className="text-emerald-400 font-medium">{counts.monitor}</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}
