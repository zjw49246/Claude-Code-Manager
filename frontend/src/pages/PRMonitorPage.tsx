import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';
import type { MonitoredRepo, PRReview } from '../api/client';
import { Plus, ArrowLeft, X, Copy, RefreshCw, ToggleLeft, ToggleRight, Trash2, GitPullRequest, Check } from 'lucide-react';

const WEBHOOK_URL = 'https://youchengsong.claude-code-manager.com/api/github/webhook';

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-yellow-500/20 text-yellow-400',
  reviewing: 'bg-blue-500/20 text-blue-400',
  merged: 'bg-green-500/20 text-green-400',
  approved: 'bg-green-500/20 text-green-400',
  commented: 'bg-orange-500/20 text-orange-400',
  error: 'bg-red-500/20 text-red-400',
  superseded: 'bg-gray-500/20 text-gray-400',
};

function copyToClipboard(text: string) {
  navigator.clipboard.writeText(text);
}

function AddRepoModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [repoName, setRepoName] = useState('');
  const [autoMerge, setAutoMerge] = useState(false);
  const [reviewModel, setReviewModel] = useState('');
  const [defaultBranch, setDefaultBranch] = useState('main');
  const [allowedAuthors, setAllowedAuthors] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const authors = allowedAuthors.trim() ? allowedAuthors.split(',').map(a => a.trim()).filter(Boolean) : [];
      await api.createMonitoredRepo({
        repo_full_name: repoName.trim(),
        auto_merge: autoMerge,
        review_model: reviewModel.trim() || undefined,
        default_branch: defaultBranch.trim() || 'main',
        allowed_authors: authors,
      });
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
          <h3 className="text-foreground font-semibold">Add Repository</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200"><X size={18} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <div>
            <label className="block text-xs text-gray-400 mb-1">Repository (owner/repo)</label>
            <input
              className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={repoName} onChange={(e) => setRepoName(e.target.value)}
              placeholder="owner/repo" required
            />
          </div>
          <div className="flex items-center gap-2">
            <input type="checkbox" id="autoMerge" checked={autoMerge} onChange={(e) => setAutoMerge(e.target.checked)}
              className="rounded bg-gray-700 border-gray-600" />
            <label htmlFor="autoMerge" className="text-sm text-gray-300">Auto-merge approved PRs</label>
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Review Model (optional)</label>
            <input
              className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={reviewModel} onChange={(e) => setReviewModel(e.target.value)}
              placeholder="e.g. claude-sonnet-4-6"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Default Branch</label>
            <input
              className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={defaultBranch} onChange={(e) => setDefaultBranch(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Allowed Authors (comma-separated, empty = all)</label>
            <input
              className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={allowedAuthors} onChange={(e) => setAllowedAuthors(e.target.value)}
              placeholder="user1, user2"
            />
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-gray-300 hover:text-white">Cancel</button>
            <button type="submit" disabled={submitting || !repoName.trim()}
              className="px-4 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500 disabled:opacity-50">
              {submitting ? 'Adding...' : 'Add'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function RepoDetail({ repo, onBack, onRefresh }: { repo: MonitoredRepo; onBack: () => void; onRefresh: () => void }) {
  const [detail, setDetail] = useState<MonitoredRepo>(repo);
  const [reviews, setReviews] = useState<PRReview[]>([]);
  const [page, setPage] = useState(1);
  const [autoMerge, setAutoMerge] = useState(repo.auto_merge);
  const [reviewModel, setReviewModel] = useState(repo.review_model || '');
  const [defaultBranch, setDefaultBranch] = useState(repo.default_branch);
  const [authorsInput, setAuthorsInput] = useState((repo.allowed_authors || []).join(', '));
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  const loadDetail = useCallback(async () => {
    try {
      const d = await api.updateMonitoredRepo(repo.id, {});
      setDetail(d);
    } catch { /* ignore */ }
  }, [repo.id]);

  const loadReviews = useCallback(async () => {
    try {
      const r = await api.getRepoReviews(repo.id, page);
      setReviews(r);
    } catch { /* ignore */ }
  }, [repo.id, page]);

  useEffect(() => { loadDetail(); loadReviews(); }, [loadDetail, loadReviews]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const authors = authorsInput.trim() ? authorsInput.split(',').map(a => a.trim()).filter(Boolean) : [];
      const updated = await api.updateMonitoredRepo(repo.id, {
        auto_merge: autoMerge,
        review_model: reviewModel.trim() || undefined,
        default_branch: defaultBranch.trim() || 'main',
        allowed_authors: authors,
      });
      setDetail(updated);
      onRefresh();
    } catch { /* ignore */ }
    setSaving(false);
  };

  const handleRegenerate = async () => {
    if (!confirm('Regenerate webhook secret? You will need to update the GitHub webhook config.')) return;
    try {
      const updated = await api.regenerateSecret(repo.id);
      setDetail(updated);
    } catch { /* ignore */ }
  };

  const handleCopy = (text: string, label: string) => {
    copyToClipboard(text);
    setCopied(label);
    setTimeout(() => setCopied(null), 2000);
  };

  return (
    <div className="space-y-6">
      <button onClick={onBack} className="flex items-center gap-1 text-sm text-gray-400 hover:text-white">
        <ArrowLeft size={16} /> Back to repositories
      </button>

      <div className="bg-gray-800 rounded-lg p-5 space-y-4">
        <h3 className="text-foreground font-semibold text-lg">{detail.repo_full_name}</h3>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="flex items-center gap-2">
            <input type="checkbox" id="detailAutoMerge" checked={autoMerge} onChange={(e) => setAutoMerge(e.target.checked)}
              className="rounded bg-gray-700 border-gray-600" />
            <label htmlFor="detailAutoMerge" className="text-sm text-gray-300">Auto-merge approved PRs</label>
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Review Model</label>
            <input className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={reviewModel} onChange={(e) => setReviewModel(e.target.value)} placeholder="Default" />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Default Branch</label>
            <input className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={defaultBranch} onChange={(e) => setDefaultBranch(e.target.value)} />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Allowed Authors (comma-separated)</label>
            <input className="w-full bg-gray-700 text-foreground text-sm rounded px-3 py-2 outline-none focus:ring-1 focus:ring-indigo-500"
              value={authorsInput} onChange={(e) => setAuthorsInput(e.target.value)} placeholder="All authors" />
          </div>
        </div>

        <button onClick={handleSave} disabled={saving}
          className="px-4 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500 disabled:opacity-50">
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>

      <div className="bg-gray-800 rounded-lg p-5 space-y-3">
        <h4 className="text-foreground font-semibold">Webhook Configuration</h4>
        <div className="space-y-2">
          <div>
            <label className="block text-xs text-gray-400 mb-1">Payload URL</label>
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-gray-700 text-foreground text-xs rounded px-3 py-2 overflow-x-auto">{WEBHOOK_URL}</code>
              <button onClick={() => handleCopy(WEBHOOK_URL, 'url')}
                className="p-2 text-gray-400 hover:text-white">
                {copied === 'url' ? <Check size={16} className="text-green-400" /> : <Copy size={16} />}
              </button>
            </div>
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Secret</label>
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-gray-700 text-foreground text-xs rounded px-3 py-2 overflow-x-auto">{detail.webhook_secret}</code>
              <button onClick={() => handleCopy(detail.webhook_secret, 'secret')}
                className="p-2 text-gray-400 hover:text-white">
                {copied === 'secret' ? <Check size={16} className="text-green-400" /> : <Copy size={16} />}
              </button>
              <button onClick={handleRegenerate} className="p-2 text-gray-400 hover:text-white" title="Regenerate secret">
                <RefreshCw size={16} />
              </button>
            </div>
          </div>
          <p className="text-xs text-gray-500">
            Content type: application/json. Events: Pull requests only.
          </p>
        </div>
      </div>

      <div className="bg-gray-800 rounded-lg p-5 space-y-3">
        <h4 className="text-foreground font-semibold">Review History</h4>
        {reviews.length === 0 ? (
          <p className="text-gray-500 text-sm">No reviews yet</p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-400 text-left border-b border-gray-700">
                    <th className="pb-2 pr-4">PR</th>
                    <th className="pb-2 pr-4">Title</th>
                    <th className="pb-2 pr-4">Author</th>
                    <th className="pb-2 pr-4">Status</th>
                    <th className="pb-2 pr-4">Action</th>
                    <th className="pb-2 pr-4">Task</th>
                    <th className="pb-2">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {reviews.map(r => (
                    <tr key={r.id} className="border-b border-gray-700/50 text-gray-300">
                      <td className="py-2 pr-4">
                        <a href={r.pr_url} target="_blank" rel="noopener noreferrer"
                          className="text-indigo-400 hover:text-indigo-300">#{r.pr_number}</a>
                      </td>
                      <td className="py-2 pr-4 max-w-xs truncate">{r.pr_title}</td>
                      <td className="py-2 pr-4">{r.pr_author}</td>
                      <td className="py-2 pr-4">
                        <span className={`px-2 py-0.5 rounded text-xs ${STATUS_COLORS[r.status] || 'bg-gray-600 text-gray-300'}`}>
                          {r.status}
                        </span>
                      </td>
                      <td className="py-2 pr-4 text-xs">{r.action_taken || '-'}</td>
                      <td className="py-2 pr-4">
                        {r.task_id ? <span className="text-indigo-400">#{r.task_id}</span> : '-'}
                      </td>
                      <td className="py-2 text-xs text-gray-500">{new Date(r.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex gap-2 pt-2">
              <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}
                className="px-3 py-1 text-xs bg-gray-700 text-gray-300 rounded disabled:opacity-50">Prev</button>
              <span className="text-xs text-gray-400 py-1">Page {page}</span>
              <button onClick={() => setPage(p => p + 1)} disabled={reviews.length < 20}
                className="px-3 py-1 text-xs bg-gray-700 text-gray-300 rounded disabled:opacity-50">Next</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function PRMonitorPage() {
  const [repos, setRepos] = useState<MonitoredRepo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [selectedRepo, setSelectedRepo] = useState<MonitoredRepo | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.getMonitoredRepos();
      setRepos(data);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleToggle = async (repo: MonitoredRepo) => {
    try {
      await api.toggleMonitoredRepo(repo.id);
      refresh();
    } catch { /* ignore */ }
  };

  const handleDelete = async (repo: MonitoredRepo) => {
    if (!confirm(`Delete monitoring for ${repo.repo_full_name}? This will also delete all review history.`)) return;
    try {
      await api.deleteMonitoredRepo(repo.id);
      refresh();
    } catch { /* ignore */ }
  };

  if (selectedRepo) {
    return (
      <div className="p-4 md:p-6 max-w-6xl mx-auto">
        <RepoDetail repo={selectedRepo} onBack={() => { setSelectedRepo(null); refresh(); }} onRefresh={refresh} />
      </div>
    );
  }

  return (
    <div className="p-4 md:p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2">
          <GitPullRequest size={22} className="text-indigo-400" />
          <h2 className="text-xl font-bold text-foreground">PR Monitor</h2>
        </div>
        <button onClick={() => setShowModal(true)}
          className="flex items-center gap-1 px-4 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500">
          <Plus size={16} /> Add Repository
        </button>
      </div>

      {error && <p className="text-red-400 text-sm mb-4">{error}</p>}

      {repos.length === 0 ? (
        <div className="text-center py-16 text-gray-500">
          <GitPullRequest size={48} className="mx-auto mb-4 opacity-30" />
          <p>No repositories monitored yet</p>
          <p className="text-sm mt-1">Add a repository to start auto-reviewing PRs</p>
        </div>
      ) : (
        <div className="bg-gray-800 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 text-left border-b border-gray-700">
                <th className="px-4 py-3">Repository</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Auto Merge</th>
                <th className="px-4 py-3">Enabled</th>
                <th className="px-4 py-3">Created</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {repos.map(repo => (
                <tr key={repo.id} className="border-b border-gray-700/50 hover:bg-gray-700/30 cursor-pointer text-gray-300"
                  onClick={() => setSelectedRepo(repo)}>
                  <td className="px-4 py-3 font-medium text-foreground">{repo.repo_full_name}</td>
                  <td className="px-4 py-3">
                    <span className={`inline-block w-2 h-2 rounded-full mr-2 ${repo.status === 'active' ? 'bg-green-400' : 'bg-red-400'}`} />
                    {repo.status}
                  </td>
                  <td className="px-4 py-3">
                    {repo.auto_merge ? (
                      <span className="px-2 py-0.5 bg-green-500/20 text-green-400 rounded text-xs">ON</span>
                    ) : (
                      <span className="px-2 py-0.5 bg-gray-600/50 text-gray-400 rounded text-xs">OFF</span>
                    )}
                  </td>
                  <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                    <button onClick={() => handleToggle(repo)} className="text-gray-400 hover:text-white">
                      {repo.enabled ? <ToggleRight size={22} className="text-green-400" /> : <ToggleLeft size={22} />}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">{new Date(repo.created_at).toLocaleDateString()}</td>
                  <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                    <button onClick={() => handleDelete(repo)} className="text-gray-400 hover:text-red-400">
                      <Trash2 size={16} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showModal && <AddRepoModal onClose={() => setShowModal(false)} onSaved={refresh} />}
    </div>
  );
}
