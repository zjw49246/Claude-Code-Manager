import { useState, useRef, useEffect } from 'react';

interface ExpandableTextProps {
  text: string;
  collapsedLines?: number;
  className?: string;
  expandedClassName?: string;
}

export function ExpandableText({
  text,
  collapsedLines = 2,
  className = '',
  expandedClassName,
}: ExpandableTextProps) {
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);
  const ref = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setClamped(el.scrollHeight > el.clientHeight + 1);
  }, [text, collapsedLines]);

  const lineClampStyle = expanded
    ? undefined
    : {
        display: '-webkit-box' as const,
        WebkitLineClamp: collapsedLines,
        WebkitBoxOrient: 'vertical' as const,
        overflow: 'hidden' as const,
      };

  return (
    <div>
      <p
        ref={ref}
        className={expanded ? (expandedClassName ?? className) : className}
        style={lineClampStyle}
        onClick={clamped || expanded ? () => setExpanded(!expanded) : undefined}
        role={clamped || expanded ? 'button' : undefined}
        tabIndex={clamped || expanded ? 0 : undefined}
        onKeyDown={
          clamped || expanded
            ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded); } }
            : undefined
        }
        data-testid="expandable-text"
      >
        {text}
      </p>
      {(clamped || expanded) && (
        <button
          className="text-xs text-indigo-400 hover:text-indigo-300 mt-0.5"
          onClick={() => setExpanded(!expanded)}
          data-testid="expand-toggle"
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  );
}
