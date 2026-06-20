import { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from '../api/client';
import type { OrgMember, OrgTeam, SharedTaskReceived } from '../api/client';
import { Plus, X, Trash2, UserPlus, Users, MessageCircle, Search, RefreshCw } from 'lucide-react';
import { SharedChatView } from '../components/Chat/SharedChatView';

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

/* ── Transfer Registry Modal ──────────────────────────────────── */
function TransferModal({
  members,
  onClose,
  onTransferred,
}: {
  members: OrgMember[];
  onClose: () => void;
  onTransferred: () => void;
}) {
  const [selected, setSelected] = useState<OrgMember | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleTransfer = async () => {
    if (!selected) return;
    if (!confirm(`Transfer org registry to ${selected.name} (${selected.ccm_url})? This action cannot be undone.`)) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.transferRegistry(selected.ccm_url);
      onTransferred();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-xl shadow-2xl w-full max-w-md max-h-[70vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
          <h3 className="text-foreground font-semibold">Transfer Registry</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-3 overflow-y-auto flex-1">
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <p className="text-sm text-gray-400">Select a member to transfer the org registry ownership to:</p>
          {members.length === 0 ? (
            <p className="text-gray-500 text-sm">No other members available.</p>
          ) : (
            members.map(m => (
              <button
                key={m.feishu_open_id}
                onClick={() => setSelected(m)}
                className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-colors ${
                  selected?.feishu_open_id === m.feishu_open_id
                    ? 'bg-blue-600/20 border border-blue-500/30'
                    : 'bg-gray-700/50 hover:bg-gray-700 border border-transparent'
                }`}
              >
                {m.avatar_url ? (
                  <img src={m.avatar_url} className="w-7 h-7 rounded-full" alt="" />
                ) : (
                  <div className="w-7 h-7 rounded-full bg-gray-600 flex items-center justify-center text-xs text-gray-300">
                    {m.name.charAt(0).toUpperCase()}
                  </div>
                )}
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-foreground">{m.name}</div>
                  <div className="text-xs text-gray-500 truncate">{m.ccm_url}</div>
                </div>
              </button>
            ))
          )}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-700">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200">Cancel</button>
          <button
            onClick={handleTransfer}
            disabled={!selected || submitting}
            className="px-4 py-2 text-sm bg-red-600 text-white rounded hover:bg-red-500 disabled:opacity-50"
          >
            {submitting ? 'Transferring...' : 'Transfer'}
          </button>
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
  const [isRegistry, setIsRegistry] = useState(false);
  const [myOpenId, setMyOpenId] = useState('');
  const [showTransfer, setShowTransfer] = useState(false);

  // Shared tasks state
  const [sharedTasks, setSharedTasks] = useState<SharedTaskReceived[]>([]);
  const [openSharedTask, setOpenSharedTask] = useState<SharedTaskReceived | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [ownerFilter, setOwnerFilter] = useState('');

  const fetchAll = useCallback(async () => {
    try {
      const [m, t, s, fs] = await Promise.all([
        api.getOrgMembers(),
        api.getOrgTeams(),
        api.getSharedTasks(),
        api.getFeishuStatus(),
      ]);
      setMembers(m);
      setTeams(t);
      setSharedTasks(s.tasks);
      setIsRegistry(fs.is_registry || false);
      setMyOpenId(fs.open_id || '');
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const selectedTeam = teams.find((t) => t.id === selectedTeamId) ?? null;

  const handleDeleteTeam = async (id: number) => {
    if (!confirm('Delete this team?')) return;
    try {
      await api.deleteOrgTeam(id);
      if (selectedTeamId === id) setSelectedTeamId(null);
      await fetchAll();
    } catch { /* ignore */ }
  };

  const handleRemoveMember = async (openId: string) => {
    if (!selectedTeamId) return;
    try {
      await api.removeTeamMember(selectedTeamId, openId);
      await fetchAll();
    } catch { /* ignore */ }
  };

  const handleLeaveShared = async (id: number) => {
    if (!confirm('Leave this shared task?')) return;
    try {
      await api.leaveSharedTask(id);
      setSharedTasks(prev => prev.filter(t => t.id !== id));
      if (openSharedTask?.id === id) setOpenSharedTask(null);
    } catch { /* ignore */ }
  };

  const owners = useMemo(() => {
    const set = new Set<string>();
    sharedTasks.forEach(t => { if (t.owner_name) set.add(t.owner_name); });
    return Array.from(set).sort();
  }, [sharedTasks]);

  const filteredShared = useMemo(() => {
    let result = sharedTasks;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      result = result.filter(t =>
        (t.task_title || '').toLowerCase().includes(q) ||
        (t.task_description || '').toLowerCase().includes(q) ||
        (t.project_name || '').toLowerCase().includes(q)
      );
    }
    if (ownerFilter) {
      result = result.filter(t => t.owner_name === ownerFilter);
    }
    return result;
  }, [sharedTasks, searchQuery, ownerFilter]);

  if (openSharedTask) {
    return (
      <div className="h-[calc(100vh-8rem)]">
        <SharedChatView shared={openSharedTask} onBack={() => setOpenSharedTask(null)} />
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <p className="text-gray-400">Loading...</p>
      </div>
    );
  }

  return (
    <div className="p-4 space-y-6">
      {/* ── Groups Section ── */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
            <Users size={20} /> Groups
          </h2>
          <div className="flex items-center gap-2">
            {isRegistry && (
              <button
                onClick={() => setShowTransfer(true)}
                className="flex items-center gap-1 px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200 border border-gray-600 rounded hover:bg-gray-700"
              >
                Transfer Registry
              </button>
            )}
            <button
              onClick={() => { setEditingTeam(null); setShowTeamModal(true); }}
              className="flex items-center gap-1 px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500"
            >
              <Plus size={14} /> New Group
            </button>
          </div>
        </div>

        {teams.length === 0 ? (
          <div className="text-center py-8 bg-gray-800 rounded-lg text-gray-500">
            <Users size={32} className="mx-auto mb-2 opacity-40" />
            <p className="text-sm">No groups yet. Click "New Group" to create one.</p>
          </div>
        ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Left: Group list */}
          <div className="md:col-span-1 space-y-2">
            {teams.map((team) => (
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
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDeleteTeam(team.id); }}
                        className="text-gray-400 hover:text-red-400 p-1"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>
                  {team.description && <div className="text-xs text-gray-400 mt-0.5">{team.description}</div>}
                  <div className="text-xs text-gray-500 mt-1">{team.members?.length || 0} members</div>
                </div>
              ))
            }
          </div>

          {/* Right: Selected group members */}
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
                  <p className="text-gray-500 text-sm">No members in this group yet.</p>
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
                        </div>
                        <button
                          onClick={() => handleRemoveMember(m.feishu_open_id)}
                          className="text-gray-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity p-1"
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
                <p className="text-sm">Select a group to view its members</p>
              </div>
            )}
          </div>
        </div>
        )}
      </div>

      {/* ── Shared Tasks Section ── */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-foreground">Shared with me</h2>
          <button
            onClick={fetchAll}
            disabled={loading}
            className="flex items-center gap-2 px-3 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 text-gray-200 rounded-lg disabled:opacity-50"
          >
            <RefreshCw size={14} /> Refresh
          </button>
        </div>

        {sharedTasks.length > 0 && (
          <div className="flex items-center gap-3 mb-3">
            <div className="relative flex-1">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
              <input
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search shared tasks..."
                className="w-full bg-gray-800 text-foreground rounded-lg pl-9 pr-3 py-2 text-sm border border-gray-700 focus:outline-none focus:border-blue-500"
              />
            </div>
            {owners.length > 1 && (
              <select
                value={ownerFilter}
                onChange={(e) => setOwnerFilter(e.target.value)}
                className="bg-gray-800 text-foreground rounded-lg px-3 py-2 text-sm border border-gray-700 focus:outline-none focus:border-blue-500"
              >
                <option value="">All owners</option>
                {owners.map(o => <option key={o} value={o}>{o}</option>)}
              </select>
            )}
          </div>
        )}

        {sharedTasks.length === 0 ? (
          <div className="text-center py-10 text-gray-500 bg-gray-800 rounded-lg">
            <p className="text-sm">No shared tasks yet. When someone shares a task with you, it will appear here.</p>
          </div>
        ) : filteredShared.length === 0 ? (
          <p className="text-gray-500 text-sm text-center py-8">No tasks match your search.</p>
        ) : (
          <div className="grid gap-3">
            {filteredShared.map(task => (
              <div key={task.id} className="bg-gray-800 rounded-xl border border-gray-700 p-4 hover:border-gray-600 transition-colors">
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0 cursor-pointer" onClick={() => setOpenSharedTask(task)}>
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs text-gray-500 bg-gray-700 px-2 py-0.5 rounded">
                        from {task.owner_name || 'Unknown'}
                      </span>
                      {task.project_name && (
                        <span className="text-xs text-blue-400 bg-blue-900/30 px-2 py-0.5 rounded">
                          {task.project_name}
                        </span>
                      )}
                    </div>
                    <h3 className="text-foreground font-medium truncate">
                      {task.task_title || `Task #${task.remote_task_id}`}
                    </h3>
                    {task.task_description && (
                      <p className="text-gray-400 text-sm mt-1 line-clamp-2">{task.task_description}</p>
                    )}
                    {task.received_at && (
                      <p className="text-gray-500 text-xs mt-2">
                        Received {new Date(task.received_at).toLocaleString()}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-2 ml-4 flex-shrink-0">
                    <button
                      onClick={() => setOpenSharedTask(task)}
                      className="flex items-center gap-1 px-3 py-1.5 text-sm bg-blue-600 hover:bg-blue-500 text-white rounded-lg"
                    >
                      <MessageCircle size={14} /> Open
                    </button>
                    <button
                      onClick={() => handleLeaveShared(task.id)}
                      className="p-1.5 text-gray-400 hover:text-red-400 rounded-lg hover:bg-gray-700"
                    >
                      <X size={16} />
                    </button>
                  </div>
                </div>
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
      {showTransfer && (
        <TransferModal
          members={members.filter(m => m.feishu_open_id !== myOpenId)}
          onClose={() => setShowTransfer(false)}
          onTransferred={fetchAll}
        />
      )}
    </div>
  );
}
