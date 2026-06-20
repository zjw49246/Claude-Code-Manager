import { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../api/client';
import type { OrgMember, OrgTeam } from '../api/client';
import { X, Users, User, Check } from 'lucide-react';

interface ShareModalProps {
  type: 'task' | 'project';
  itemId: number;
  itemTitle: string;
  onClose: () => void;
}

export function ShareModal({ type, itemId, itemTitle, onClose }: ShareModalProps) {
  const [members, setMembers] = useState<OrgMember[]>([]);
  const [teams, setTeams] = useState<OrgTeam[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [existing, setExisting] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feishuBound, setFeishuBound] = useState<boolean | null>(null);
  const [myOpenId, setMyOpenId] = useState('');

  useEffect(() => {
    (async () => {
      try {
        const feishuStatus = await api.getFeishuStatus();
        setFeishuBound(feishuStatus.bound);
        if (!feishuStatus.bound) {
          setLoading(false);
          return;
        }
        const [membersData, teamsData, sharesData] = await Promise.all([
          api.getOrgMembers(),
          api.getOrgTeams(),
          type === 'task' ? api.getTaskShares(itemId) : api.getProjectShares(itemId),
        ]);
        setMyOpenId(feishuStatus.open_id || '');
        setMembers(membersData);
        setTeams(teamsData);
        const alreadyShared = new Set(sharesData.shares.map((s: any) => s.shared_to_open_id));
        setExisting(alreadyShared);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, [type, itemId]);

  const isSelf = (openId: string) => openId === myOpenId;

  const toggleMember = (openId: string) => {
    if (isSelf(openId)) return;
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(openId)) next.delete(openId);
      else next.add(openId);
      return next;
    });
  };

  const toggleTeam = (team: OrgTeam) => {
    const teamMembers = (team.members || []).filter(m => !isSelf(m.feishu_open_id));
    const allSelected = teamMembers.length > 0 && teamMembers.every(m => selected.has(m.feishu_open_id));
    setSelected(prev => {
      const next = new Set(prev);
      teamMembers.forEach(m => {
        if (allSelected) next.delete(m.feishu_open_id);
        else next.add(m.feishu_open_id);
      });
      return next;
    });
  };

  const handleShare = async () => {
    const targets = members
      .filter(m => selected.has(m.feishu_open_id) && !existing.has(m.feishu_open_id))
      .map(m => ({ open_id: m.feishu_open_id, name: m.name, ccm_url: m.ccm_url }));

    if (targets.length === 0) {
      onClose();
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      if (type === 'task') {
        await api.shareTask(itemId, targets);
      } else {
        await api.shareProject(itemId, targets);
      }
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-[9999] p-4">
      <div className="bg-gray-800 rounded-xl shadow-2xl w-full max-w-lg max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
          <h3 className="text-foreground font-semibold">
            Share {type === 'task' ? 'Task' : 'Project'}: {itemTitle}
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={18} /></button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {error && <p className="text-red-400 text-sm">{error}</p>}
          {loading ? (
            <p className="text-gray-400 text-sm">Loading members...</p>
          ) : feishuBound === false ? (
            <div className="text-center py-8">
              <p className="text-gray-400 mb-3">Please bind your Feishu account first to use sharing.</p>
              <button
                onClick={async () => {
                  try {
                    const { url } = await api.getFeishuAuthUrl();
                    window.location.href = url;
                  } catch { /* ignore */ }
                }}
                className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-500"
              >Bind Feishu</button>
            </div>
          ) : (
            <>
              {teams.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium text-gray-400 mb-2">Teams</h4>
                  <div className="space-y-1">
                    {teams.map(team => {
                      const teamMembers = team.members || [];
                      const allSelected = teamMembers.length > 0 && teamMembers.every(m => selected.has(m.feishu_open_id) || existing.has(m.feishu_open_id));
                      return (
                        <button
                          key={team.id}
                          onClick={() => toggleTeam(team)}
                          className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-colors ${
                            allSelected ? 'bg-blue-600/20 border border-blue-500/30' : 'bg-gray-700/50 hover:bg-gray-700 border border-transparent'
                          }`}
                        >
                          <Users size={16} className="text-gray-400 flex-shrink-0" />
                          <span className="flex-1 text-sm text-foreground">{team.name}</span>
                          <span className="text-xs text-gray-500">{teamMembers.length} members</span>
                          {allSelected && <Check size={14} className="text-blue-400" />}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              <div>
                <h4 className="text-sm font-medium text-gray-400 mb-2">Members</h4>
                <div className="space-y-1">
                  {members.map(m => {
                    const isMe = isSelf(m.feishu_open_id);
                    const isExisting = existing.has(m.feishu_open_id);
                    const isDisabled = isMe || isExisting;
                    const isSelected = selected.has(m.feishu_open_id);
                    return (
                      <button
                        key={m.feishu_open_id}
                        onClick={() => !isDisabled && toggleMember(m.feishu_open_id)}
                        disabled={isDisabled}
                        className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-colors ${
                          isDisabled
                            ? 'bg-gray-700/30 opacity-60 cursor-not-allowed'
                            : isSelected
                            ? 'bg-blue-600/20 border border-blue-500/30'
                            : 'bg-gray-700/50 hover:bg-gray-700 border border-transparent'
                        }`}
                      >
                        {m.avatar_url ? (
                          <img src={m.avatar_url} alt="" className="w-6 h-6 rounded-full" />
                        ) : (
                          <User size={16} className="text-gray-400" />
                        )}
                        <span className="flex-1 text-sm text-foreground">{m.name}</span>
                        {isExisting && <span className="text-xs text-gray-500">Already shared</span>}
                        {isSelected && !isDisabled && <Check size={14} className="text-blue-400" />}
                      </button>
                    );
                  })}
                  {members.length === 0 && (
                    <p className="text-gray-500 text-sm">No org members found. Bind Feishu first.</p>
                  )}
                </div>
              </div>
            </>
          )}
        </div>

        <div className="flex justify-end gap-3 px-5 py-4 border-t border-gray-700">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200">
            Cancel
          </button>
          <button
            onClick={handleShare}
            disabled={submitting || selected.size === 0}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? 'Sharing...' : `Share (${selected.size})`}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
