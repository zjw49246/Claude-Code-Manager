import { useState, useEffect } from 'react';
import { X, Check } from 'lucide-react';
import { api } from '../api/client';
import type { TeamUser } from '../api/client';

interface TeamShareModalProps {
  type: 'project' | 'task';
  itemId: number;
  itemTitle: string;
  onClose: () => void;
}

export function TeamShareModal({ type, itemId, itemTitle, onClose }: TeamShareModalProps) {
  const [users, setUsers] = useState<TeamUser[]>([]);
  const [shares, setShares] = useState<{ target_id: number; target_type: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<number | null>(null);

  useEffect(() => {
    Promise.all([
      api.getTeamUsers(),
      type === 'project' ? api.teamGetProjectShares(itemId) : api.getTaskSharesTeam(itemId),
    ]).then(([u, s]) => {
      setUsers(u);
      setShares(s.map((x: any) => ({ target_id: x.target_id, target_type: x.target_type })));
    }).catch(() => {}).finally(() => setLoading(false));
  }, [type, itemId]);

  const isShared = (userId: number) => shares.some(s => s.target_type === 'user' && s.target_id === userId);

  const toggle = async (userId: number) => {
    setSaving(userId);
    try {
      if (isShared(userId)) {
        if (type === 'project') await api.teamUnshareProject(itemId, 'user', userId);
        else await api.unshareTaskTeam(itemId, 'user', userId);
        setShares(shares.filter(s => !(s.target_type === 'user' && s.target_id === userId)));
      } else {
        if (type === 'project') await api.teamShareProject(itemId, 'user', userId);
        else await api.shareTaskTeam(itemId, 'user', userId);
        setShares([...shares, { target_type: 'user', target_id: userId }]);
      }
    } catch (e) {
      alert(String(e));
    } finally {
      setSaving(null);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-xl shadow-2xl w-full max-w-sm">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
          <h3 className="text-foreground font-semibold text-sm">
            Share {type === 'project' ? 'Project' : 'Task'}: {itemTitle}
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={18} /></button>
        </div>
        <div className="p-4 space-y-1 max-h-[50vh] overflow-y-auto">
          {loading ? (
            <p className="text-gray-400 text-sm">Loading...</p>
          ) : users.length === 0 ? (
            <p className="text-gray-500 text-sm">No users to share with.</p>
          ) : (
            users.filter(u => {
              const me = JSON.parse(localStorage.getItem('cc_user') || '{}');
              return u.role !== 'admin' && u.role !== 'super_admin' && u.id !== me.id;
            }).map(u => (
              <div
                key={u.id}
                className="flex items-center justify-between p-2 rounded hover:bg-gray-700 cursor-pointer"
                onClick={() => saving === null && toggle(u.id)}
              >
                <div className="flex items-center gap-2 min-w-0">
                  {u.avatar_url ? (
                    <img src={u.avatar_url} className="w-7 h-7 rounded-full" alt="" />
                  ) : (
                    <div className="w-7 h-7 rounded-full bg-gray-600 flex items-center justify-center text-xs text-gray-300">
                      {u.name.charAt(0).toUpperCase()}
                    </div>
                  )}
                  <div className="min-w-0">
                    <div className="text-sm text-foreground truncate">{u.name}</div>
                    <div className="text-xs text-gray-500 truncate">{u.email}</div>
                  </div>
                </div>
                <div className="shrink-0 ml-2">
                  {saving === u.id ? (
                    <div className="w-5 h-5 border-2 border-gray-500 border-t-transparent rounded-full animate-spin" />
                  ) : isShared(u.id) ? (
                    <div className="w-5 h-5 bg-green-500 rounded flex items-center justify-center">
                      <Check size={12} className="text-white" />
                    </div>
                  ) : (
                    <div className="w-5 h-5 border border-gray-600 rounded" />
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
