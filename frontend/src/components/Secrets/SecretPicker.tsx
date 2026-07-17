import { useState, useEffect } from 'react';
import { api } from '../../api/client';
import type { Secret } from '../../api/client';
import { KeyRound, Check } from '../icons';

interface SecretPickerProps {
  selectedIds: number[];
  onChange: (ids: number[]) => void;
  disabled?: boolean;
}

export function SecretPicker({ selectedIds, onChange, disabled }: SecretPickerProps) {
  const [secrets, setSecrets] = useState<Secret[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    api.listSecrets().then(setSecrets).catch(() => {});
  }, []);

  if (secrets.length === 0) return null;

  const toggle = (id: number) => {
    if (selectedIds.includes(id)) {
      onChange(selectedIds.filter((s) => s !== id));
    } else {
      onChange([...selectedIds, id]);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        disabled={disabled}
        className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 px-2 py-1 rounded border border-gray-600 hover:border-gray-400 disabled:opacity-40"
      >
        <KeyRound size={13} />
        {selectedIds.length > 0 ? `${selectedIds.length} secret${selectedIds.length > 1 ? 's' : ''}` : 'Secrets'}
      </button>
      {open && (
        <div className="absolute bottom-full mb-1 left-0 z-50 bg-gray-800 border border-gray-600 rounded-lg shadow-xl py-1 min-w-[200px] max-h-48 overflow-y-auto">
          {secrets.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => toggle(s.id)}
              className="flex items-center gap-2 w-full px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700 text-left"
            >
              <span className={`w-4 h-4 flex items-center justify-center rounded border ${selectedIds.includes(s.id) ? 'bg-indigo-600 border-indigo-600' : 'border-gray-500'}`}>
                {selectedIds.includes(s.id) && <Check size={12} className="text-white" />}
              </span>
              <span className="truncate">{s.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
