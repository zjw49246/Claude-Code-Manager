import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';
import type { OrgMember, OrgTeam } from '../api/client';
import { Plus, X, Trash2, UserPlus, Users } from 'lucide-react';

/* ── Create / Edit Team Modal ─────────────────────────────────── */
function TeamModal({
  team,
  onClose,
  onSaved,
}: {
  team?: OrgTeam | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(team?.name ?? '');
  const [description, setDescription] = useState(team?.description ?? '');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      if (team) {
        await api.updateOrgTeam(team.id, name.trim(), description.trim() || undefined);
      } else {
        await api.createOrgTeam(name.trim(), description.trim() || undefined);
      }
      onSaved();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-xl shadow-2xl w-full max-w-md">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
          <h3 className="text-foreground font-semibold">{team ? 'Edit Team' : 'New Team'}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={18} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <div>
            <label className="block text-xs text-gray-400 mb-1">Team Name *</label>
            <input
              className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Backend Team" required autoFocus
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Description</label>
            <input
              className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional description"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200">Cancel</button>
            <button
              type="submit" disabled={submitting || !name.trim()}
              className="px-4 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500 disabled:opacity-50"
            >
              {submitting ? 'Saving...' : team ? 'Save' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ── Add Member Modal ─────────────────────────────────────────── */
function AddMemberModal({
  teamId,
  allMembers,
  existingIds,
  onClose,
  onSaved,
}: {
  teamId: number;
  allMembers: OrgMember[];
  existingIds: Set<string>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const available = allMembers.filter((m) => !existingIds.has(m.feishu_open_id));
  const [adding, setAdding] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleAdd = async (openId: string) => {
    setAdding(openId);
    setError(null);
    try {
      await api.addTeamMember(teamId, openId);
      onSaved();
    } catch (e) {
      setError(String(e));
    } finally {
      setAdding(null);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-xl shadow-2xl w-full max-w-md max-h-[70vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
          <h3 className="text-foreground font-semibold">Add Member</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-2 overflow-y-auto flex-1">
          {error && <p className="text-red-400 text-sm">{error}</p>}
          {available.length === 0 ? (
            <p className="text-gray-500 text-sm">All organization members are already in this team.</p>
          ) : (
            available.map((m) => (
              <div key={m.feishu_open_id} className="flex items-center justify-between p-2 rounded bg-gray-700/50 hover:bg-gray-700">
                <div className="flex items-center gap-2">
                  {m.avatar_url && <img src={m.avatar_url} className="w-6 h-6 rounded-full" alt="" />}
                  <span className="text-sm text-foreground">{m.name}</span>
                  <span className="text-xs text-gray-500">{m.ccm_url}</span>
                </div>
                <button
                  onClick={() => handleAdd(m.feishu_open_id)}
                  disabled={adding === m.feishu_open_id}
                  className="text-xs px-2 py-1 rounded bg-indigo-600/20 text-indigo-300 hover:bg-indigo-600/30 disabled:opacity-50"
                >
                  {adding === m.feishu_open_id ? 'Adding...' : 'Add'}
                </button>
              </div>
            ))
          )}
        </div>
        <div className="px-5 py-3 border-t border-gray-700 flex justify-end">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200">Close</button>
        </div>
      </div>
    </div>
  );
}

/* ── Team Page ────────────────────────────────────────────────── */
export default function TeamPage() {
  const [members, setMembers] = useState<OrgMember[]>([]);
  const [teams, setTeams] = useState<OrgTeam[]>([]);
  const [selectedTeamId, setSelectedTeamId] = useState<number | null>(null);
  const [showTeamModal, setShowTeamModal] = useState(false);
  const [editingTeam, setEditingTeam] = useState<OrgTeam | null>(null);
  const [showAddMember, setShowAddMember] = useState(false);
  const [loading, setLoading] = useState(true);

  const fetchAll = useCallback(async () => {
    try {
      const [m, t] = await Promise.all([api.getOrgMembers(), api.getOrgTeams()]);
      setMembers(m);
      setTeams(t);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const selectedTeam = teams.find((t) => t.id === selectedTeamId) ?? null;

  const handleDeleteTeam = async (id: number) => {
    if (!confirm('Delete this team?')) return;
    try {
      await api.deleteOrgTeam(id);
      if (selectedTeamId === id) setSelectedTeamId(null);
      await fetchAll();
    } catch {
      // ignore
    }
  };

  const handleRemoveMember = async (openId: string) => {
    if (!selectedTeamId) return;
    try {
      await api.removeTeamMember(selectedTeamId, openId);
      await fetchAll();
    } catch {
      // ignore
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <p className="text-gray-400">Loading...</p>
      </div>
    );
  }

  return (
    <div className="p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
          <Users size={20} /> Team
        </h2>
        <button
          onClick={() => { setEditingTeam(null); setShowTeamModal(true); }}
          className="flex items-center gap-1 px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500"
        >
          <Plus size={14} /> New Team
        </button>
      </div>

      {/* Main grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Left: Team list */}
        <div className="md:col-span-1 space-y-2">
          {teams.length === 0 ? (
            <div className="text-gray-500 text-sm p-4 bg-gray-800 rounded-lg text-center">
              No teams yet. Create one to get started.
            </div>
          ) : (
            teams.map((team) => (
              <div
                key={team.id}
                onClick={() => setSelectedTeamId(team.id)}
                className={`p-3 rounded-lg cursor-pointer transition-colors group ${
                  selectedTeamId === team.id
                    ? 'bg-blue-600/20 border border-blue-500/30'
                    : 'bg-gray-800 hover:bg-gray-700 border border-transparent'
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="font-medium text-sm text-foreground">{team.name}</div>
                  <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={(e) => { e.stopPropagation(); setEditingTeam(team); setShowTeamModal(true); }}
                      className="text-gray-400 hover:text-gray-200 p-1"
                      title="Edit team"
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDeleteTeam(team.id); }}
                      className="text-gray-400 hover:text-red-400 p-1"
                      title="Delete team"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
                {team.description && <div className="text-xs text-gray-400 mt-0.5">{team.description}</div>}
                <div className="text-xs text-gray-500 mt-1">{team.members?.length || 0} members</div>
              </div>
            ))
          )}
        </div>

        {/* Right: Selected team details */}
        <div className="md:col-span-2 bg-gray-800 rounded-lg p-4 min-h-[200px]">
          {selectedTeam ? (
            <>
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h3 className="text-foreground font-medium">{selectedTeam.name}</h3>
                  {selectedTeam.description && (
                    <p className="text-xs text-gray-400 mt-0.5">{selectedTeam.description}</p>
                  )}
                </div>
                <button
                  onClick={() => setShowAddMember(true)}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm bg-indigo-600/20 text-indigo-300 rounded hover:bg-indigo-600/30"
                >
                  <UserPlus size={14} /> Add Member
                </button>
              </div>
              {(!selectedTeam.members || selectedTeam.members.length === 0) ? (
                <p className="text-gray-500 text-sm">No members in this team yet.</p>
              ) : (
                <div className="space-y-1">
                  {selectedTeam.members.map((m) => (
                    <div key={m.feishu_open_id} className="flex items-center justify-between p-2 rounded bg-gray-700/50 hover:bg-gray-700 group">
                      <div className="flex items-center gap-2">
                        {m.avatar_url ? (
                          <img src={m.avatar_url} className="w-7 h-7 rounded-full" alt="" />
                        ) : (
                          <div className="w-7 h-7 rounded-full bg-gray-600 flex items-center justify-center text-xs text-gray-300">
                            {m.name.charAt(0).toUpperCase()}
                          </div>
                        )}
                        <span className="text-sm text-foreground">{m.name}</span>
                        <span className="text-xs text-gray-500">{m.ccm_url}</span>
                      </div>
                      <button
                        onClick={() => handleRemoveMember(m.feishu_open_id)}
                        className="text-gray-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity p-1"
                        title="Remove from team"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="flex flex-col items-center justify-center h-full text-gray-500 py-10">
              <Users size={32} className="mb-2 opacity-50" />
              <p className="text-sm">Select a team to view its members</p>
            </div>
          )}
        </div>
      </div>

      {/* All org members */}
      <div className="bg-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-300 mb-3">Organization Members</h3>
        {members.length === 0 ? (
          <p className="text-gray-500 text-sm">No members found. Members appear after binding Feishu accounts.</p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {members.map((m) => (
              <div key={m.feishu_open_id} className="flex items-center gap-2 p-2 rounded bg-gray-700/50">
                <div className="relative flex-shrink-0">
                  {m.avatar_url ? (
                    <img src={m.avatar_url} className="w-6 h-6 rounded-full" alt="" />
                  ) : (
                    <div className="w-6 h-6 rounded-full bg-gray-600 flex items-center justify-center text-xs text-gray-300">
                      {m.name.charAt(0).toUpperCase()}
                    </div>
                  )}
                  {(m as any).is_online !== undefined && (
                    <span className={`absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full border-2 border-gray-800 ${(m as any).is_online ? 'bg-green-500' : 'bg-gray-500'}`} />
                  )}
                </div>
                <span className="text-sm text-foreground">{m.name}</span>
                <span className="text-xs text-gray-500 truncate">{m.ccm_url}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Modals */}
      {showTeamModal && (
        <TeamModal
          team={editingTeam}
          onClose={() => { setShowTeamModal(false); setEditingTeam(null); }}
          onSaved={fetchAll}
        />
      )}
      {showAddMember && selectedTeamId && (
        <AddMemberModal
          teamId={selectedTeamId}
          allMembers={members}
          existingIds={new Set((selectedTeam?.members ?? []).map((m) => m.feishu_open_id))}
          onClose={() => setShowAddMember(false)}
          onSaved={fetchAll}
        />
      )}
    </div>
  );
}
