import { useState, useEffect } from 'react';
import {
  ChevronRight, ChevronDown, Folder, FolderOpen, FileText,
  AlertCircle, Loader2, Plus, Trash2, Server, HardDrive, Download,
} from 'lucide-react';
import { api, getToken } from '../api/client';
import type { Project } from '../api/client';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface DirEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
}

interface SSHProfile {
  id: string;
  label: string;
  host: string;
  port: number;
  username: string;
  password: string;
  key_path: string;
}

type Mode = 'local' | 'ssh';

const SSH_PROFILES_KEY = 'cc_ssh_profiles';

// ---------------------------------------------------------------------------
// SSH profile storage helpers
// ---------------------------------------------------------------------------

function loadProfiles(): SSHProfile[] {
  try {
    return JSON.parse(localStorage.getItem(SSH_PROFILES_KEY) || '[]');
  } catch {
    return [];
  }
}

function saveProfiles(profiles: SSHProfile[]) {
  localStorage.setItem(SSH_PROFILES_KEY, JSON.stringify(profiles));
}

function newProfile(): SSHProfile {
  return { id: crypto.randomUUID(), label: '', host: '', port: 22, username: '', password: '', key_path: '' };
}

// ---------------------------------------------------------------------------
// Auto-inject Worker SSH profiles
// ---------------------------------------------------------------------------

function useWorkerProfiles(): SSHProfile[] {
  const [wps, setWps] = useState<SSHProfile[]>([]);
  useEffect(() => {
    api.listWorkers()
      .then((workers) => {
        setWps(
          workers
            .filter((w) => w.status === 'ready' && w.private_ip)
            .map((w) => ({
              id: `worker-${w.id}`,
              label: w.name,
              host: w.private_ip!,
              port: 22,
              username: w.ssh_user || 'ubuntu',
              password: '',
              key_path: w.ssh_key_path || '',
            })),
        );
      })
      .catch(() => {});
  }, []);
  return wps;
}

// ---------------------------------------------------------------------------
// Shared file tree node (works for both local and SSH)
// ---------------------------------------------------------------------------

interface TreeNodeProps {
  entry: DirEntry;
  selectedPath: string | null;
  onSelect: (path: string, isDir: boolean) => void;
  fetchChildren: (path: string) => Promise<DirEntry[]>;
}

function TreeNode({ entry, selectedPath, onSelect, fetchChildren }: TreeNodeProps) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<DirEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClick = async () => {
    if (!entry.is_dir) {
      onSelect(entry.path, false);
      return;
    }
    if (!open && children === null) {
      setLoading(true);
      setError(null);
      try {
        const result = await fetchChildren(entry.path);
        setChildren(result);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setLoading(false);
      }
    }
    setOpen((v) => !v);
    onSelect(entry.path, true);
  };

  const isSelected = selectedPath === entry.path;

  return (
    <div>
      <div
        onClick={handleClick}
        className={`flex items-center gap-1 px-2 py-0.5 rounded cursor-pointer text-sm select-none hover:bg-gray-700 ${
          isSelected ? 'bg-gray-700 text-indigo-400' : 'text-gray-300'
        }`}
      >
        <span className="w-4 flex-shrink-0 text-gray-500">
          {entry.is_dir ? (
            loading ? (
              <Loader2 size={14} className="animate-spin" />
            ) : open ? (
              <ChevronDown size={14} />
            ) : (
              <ChevronRight size={14} />
            )
          ) : null}
        </span>
        {entry.is_dir
          ? open ? <FolderOpen size={14} className="text-yellow-400 flex-shrink-0" /> : <Folder size={14} className="text-yellow-400 flex-shrink-0" />
          : <FileText size={14} className="text-gray-400 flex-shrink-0" />}
        <span className="truncate">{entry.name}</span>
        {entry.size !== null && (
          <span className="ml-auto text-xs text-gray-600 flex-shrink-0">{formatSize(entry.size)}</span>
        )}
      </div>
      {error && <div className="ml-8 text-xs text-red-400 py-0.5">{error}</div>}
      {open && children && (
        <div className="ml-4 border-l border-gray-700">
          {children.length === 0 && <div className="ml-4 text-xs text-gray-600 py-0.5">empty</div>}
          {children.map((child) => (
            <TreeNode key={child.path} entry={child} selectedPath={selectedPath} onSelect={onSelect} fetchChildren={fetchChildren} />
          ))}
        </div>
      )}
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`;
  return `${(bytes / 1024 / 1024).toFixed(1)}M`;
}

// ---------------------------------------------------------------------------
// SSH profile editor panel
// ---------------------------------------------------------------------------

interface SSHPanelProps {
  profiles: SSHProfile[];
  active: SSHProfile | null;
  onActivate: (p: SSHProfile) => void;
  onSave: (profiles: SSHProfile[]) => void;
}

function SSHPanel({ profiles, active, onActivate, onSave }: SSHPanelProps) {
  const [editing, setEditing] = useState<SSHProfile | null>(null);

  const startNew = () => setEditing(newProfile());
  const startEdit = (p: SSHProfile) => setEditing({ ...p });

  const handleSave = () => {
    if (!editing) return;
    const exists = profiles.find((p) => p.id === editing.id);
    const next = exists ? profiles.map((p) => (p.id === editing.id ? editing : p)) : [...profiles, editing];
    onSave(next);
    setEditing(null);
  };

  const handleDelete = (id: string) => {
    onSave(profiles.filter((p) => p.id !== id));
  };

  return (
    <div className="space-y-3">
      {/* Saved profiles */}
      <div className="space-y-1">
        {profiles.map((p) => (
          <div
            key={p.id}
            className={`flex items-center gap-2 px-3 py-2 rounded cursor-pointer text-sm ${
              active?.id === p.id ? 'bg-indigo-700 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
            }`}
          >
            <Server size={14} className="flex-shrink-0" />
            <span className="flex-1 truncate" onClick={() => onActivate(p)}>
              {p.label || `${p.username}@${p.host}`}
            </span>
            <button onClick={() => startEdit(p)} className="text-gray-400 hover:text-gray-200 text-xs px-1">edit</button>
            <button onClick={() => handleDelete(p.id)} className="text-red-400 hover:text-red-300"><Trash2 size={12} /></button>
          </div>
        ))}
      </div>

      <button
        onClick={startNew}
        className="flex items-center gap-1 text-xs text-indigo-400 hover:text-indigo-300"
      >
        <Plus size={12} /> Add server
      </button>

      {/* Inline editor */}
      {editing && (
        <div className="bg-gray-700 rounded p-3 space-y-2 text-sm">
          {[
            { label: 'Label', key: 'label', placeholder: 'My Server' },
            { label: 'Host', key: 'host', placeholder: '192.168.1.1' },
            { label: 'Port', key: 'port', placeholder: '22' },
            { label: 'Username', key: 'username', placeholder: 'ubuntu' },
            { label: 'Password', key: 'password', placeholder: '(optional if key is set)', type: 'password' },
            { label: 'Key File', key: 'key_path', placeholder: '~/.ssh/id_rsa  (optional, leave empty to use password)' },
          ].map(({ label, key, placeholder, type }) => (
            <div key={key} className="flex items-center gap-2">
              <span className="w-20 text-gray-400 flex-shrink-0">{label}</span>
              <input
                type={type || 'text'}
                value={String((editing as unknown as Record<string, unknown>)[key] ?? '')}
                onChange={(e) => setEditing({ ...editing, [key]: key === 'port' ? Number(e.target.value) : e.target.value })}
                placeholder={placeholder}
                className="flex-1 bg-gray-600 text-gray-200 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>
          ))}
          <div className="flex gap-2 pt-1">
            <button onClick={handleSave} className="px-3 py-1 bg-indigo-600 text-white rounded text-xs hover:bg-indigo-700">Save</button>
            <button onClick={() => setEditing(null)} className="px-3 py-1 bg-gray-600 text-gray-300 rounded text-xs hover:bg-gray-500">Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main FilesPage
// ---------------------------------------------------------------------------

export function FilesPage() {
  const [mode, setMode] = useState<Mode>('local');
  const [projects, setProjects] = useState<Project[]>([]);

  // Local state
  const [inputPath, setInputPath] = useState('');
  const [rootPath, setRootPath] = useState('');
  const [rootEntries, setRootEntries] = useState<DirEntry[] | null>(null);
  const [rootLoading, setRootLoading] = useState(false);
  const [rootError, setRootError] = useState<string | null>(null);

  // SSH state
  const [profiles, setProfiles] = useState<SSHProfile[]>(loadProfiles);
  const workerProfiles = useWorkerProfiles();
  const allProfiles = [...workerProfiles, ...profiles];
  const [activeProfile, setActiveProfile] = useState<SSHProfile | null>(null);
  const [sshPath, setSshPath] = useState('/');
  const [sshEntries, setSshEntries] = useState<DirEntry[] | null>(null);
  const [sshLoading, setSshLoading] = useState(false);
  const [sshError, setSshError] = useState<string | null>(null);

  // Shared viewer state
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => {});
  }, []);

  // --- local helpers ---

  const loadLocalRoot = async (path: string) => {
    if (!path.trim()) return;
    setRootLoading(true);
    setRootError(null);
    setRootEntries(null);
    setSelectedFile(null);
    setFileContent(null);
    try {
      const res = await api.listDir(path.trim());
      setRootPath(res.path);
      setRootEntries(res.entries);
    } catch (e) {
      setRootError((e as Error).message);
    } finally {
      setRootLoading(false);
    }
  };

  const localFetchChildren = async (path: string): Promise<DirEntry[]> => {
    const res = await api.listDir(path);
    return res.entries;
  };

  const handleLocalSelect = async (path: string, isDir: boolean) => {
    if (isDir) return;
    openFile(path, false, null);
  };

  // --- SSH helpers ---

  const loadSshRoot = async (profile: SSHProfile, path: string) => {
    setSshLoading(true);
    setSshError(null);
    setSshEntries(null);
    setSelectedFile(null);
    setFileContent(null);
    try {
      const creds = profileToCreds(profile);
      const res = await api.sshListDir(creds, path);
      setSshEntries(res.entries);
    } catch (e) {
      setSshError((e as Error).message);
    } finally {
      setSshLoading(false);
    }
  };

  const sshFetchChildren = async (path: string): Promise<DirEntry[]> => {
    if (!activeProfile) return [];
    const res = await api.sshListDir(profileToCreds(activeProfile), path);
    return res.entries;
  };

  const handleSshSelect = async (path: string, isDir: boolean) => {
    if (isDir) return;
    openFile(path, true, activeProfile);
  };

  const handleActivateProfile = (p: SSHProfile) => {
    setActiveProfile(p);
    setSshEntries(null);
    setSelectedFile(null);
    setFileContent(null);
    loadSshRoot(p, sshPath);
  };

  // --- shared file opener ---

  const openFile = async (path: string, isSSH: boolean, profile: SSHProfile | null) => {
    setSelectedFile(path);
    setFileContent(null);
    setFileError(null);
    setFileLoading(true);
    try {
      const res = isSSH && profile
        ? await api.sshReadFile(profileToCreds(profile), path)
        : await api.readFile(path);
      setFileContent(res.content);
    } catch (e) {
      setFileError((e as Error).message);
    } finally {
      setFileLoading(false);
    }
  };

  // --- profile persistence ---

  const handleSaveProfiles = (next: SSHProfile[]) => {
    setProfiles(next);
    saveProfiles(next);
  };

  const handleDownload = async () => {
    if (!selectedFile) return;
    const filename = selectedFile.split('/').pop() || 'download';
    if (mode === 'local') {
      let url = api.downloadFileUrl(selectedFile);
      const token = getToken();
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const iframe = document.createElement('iframe');
      iframe.style.display = 'none';
      iframe.src = url;
      document.body.appendChild(iframe);
      setTimeout(() => document.body.removeChild(iframe), 10000);
    } else if (activeProfile) {
      try {
        const res = await api.sshDownloadFile(profileToCreds(activeProfile), selectedFile);
        if (!res.ok) { setFileError('Download failed'); return; }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      } catch {
        setFileError('Download failed');
      }
    }
  };

  const currentEntries = mode === 'local' ? rootEntries : sshEntries;
  const currentRootLabel = mode === 'local' ? rootPath : (activeProfile ? `${activeProfile.username}@${activeProfile.host}:${sshPath}` : '');

  return (
    <div className="space-y-4">
      {/* Mode toggle + path bar */}
      <div className="bg-gray-800 rounded-lg p-4 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <h2 className="text-sm font-semibold text-foreground">File Browser</h2>
          <div className="flex rounded overflow-hidden border border-gray-600 text-xs">
            <button
              onClick={() => setMode('local')}
              className={`flex items-center gap-1 px-3 py-1.5 ${mode === 'local' ? 'bg-indigo-600 text-white' : 'bg-gray-700 text-gray-400 hover:bg-gray-600'}`}
            >
              <HardDrive size={12} /> Local
            </button>
            <button
              onClick={() => setMode('ssh')}
              className={`flex items-center gap-1 px-3 py-1.5 ${mode === 'ssh' ? 'bg-indigo-600 text-white' : 'bg-gray-700 text-gray-400 hover:bg-gray-600'}`}
            >
              <Server size={12} /> SSH
            </button>
          </div>
        </div>

        {mode === 'local' && (
          <div className="flex gap-2 flex-wrap">
            {projects.filter((p) => p.local_path).length > 0 && (
              <select
                onChange={(e) => {
                  const proj = projects.find((p) => String(p.id) === e.target.value);
                  if (proj?.local_path) { setInputPath(proj.local_path); loadLocalRoot(proj.local_path); }
                }}
                defaultValue=""
                className="bg-gray-700 text-gray-300 text-sm rounded px-2 py-1.5 border border-gray-600 focus:outline-none focus:border-indigo-500"
              >
                <option value="" disabled>Select project...</option>
                {projects.filter((p) => p.local_path).map((p) => (
                  <option key={p.id} value={String(p.id)}>{p.name}</option>
                ))}
              </select>
            )}
            <input
              type="text"
              value={inputPath}
              onChange={(e) => setInputPath(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && loadLocalRoot(inputPath)}
              placeholder="/path/to/directory"
              className="flex-1 bg-gray-700 text-gray-300 text-sm rounded px-3 py-1.5 border border-gray-600 focus:outline-none focus:border-indigo-500 min-w-48"
            />
            <button
              onClick={() => loadLocalRoot(inputPath)}
              disabled={rootLoading || !inputPath.trim()}
              className="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Browse
            </button>
          </div>
        )}

        {mode === 'ssh' && (
          <div className="space-y-3">
            <SSHPanel
              profiles={allProfiles}
              active={activeProfile}
              onActivate={handleActivateProfile}
              onSave={handleSaveProfiles}
            />
            {activeProfile && (
              <div className="flex gap-2">
                <input
                  type="text"
                  value={sshPath}
                  onChange={(e) => setSshPath(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && loadSshRoot(activeProfile, sshPath)}
                  placeholder="/home/user"
                  className="flex-1 bg-gray-700 text-gray-300 text-sm rounded px-3 py-1.5 border border-gray-600 focus:outline-none focus:border-indigo-500"
                />
                <button
                  onClick={() => loadSshRoot(activeProfile, sshPath)}
                  disabled={sshLoading}
                  className="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700 disabled:opacity-50"
                >
                  Browse
                </button>
              </div>
            )}
          </div>
        )}

        {(rootError || sshError) && (
          <div className="flex items-center gap-2 text-red-400 text-sm">
            <AlertCircle size={14} /> {mode === 'local' ? rootError : sshError}
          </div>
        )}
      </div>

      {/* Main browser area */}
      {currentEntries !== null && (
        <div className="flex flex-col md:flex-row gap-4 h-auto md:h-[calc(100vh-260px)] min-h-80">
          {/* File tree */}
          <div className="w-full md:w-64 md:flex-shrink-0 max-h-64 md:max-h-none bg-gray-800 rounded-lg overflow-y-auto p-2">
            <div className="text-xs text-gray-500 px-2 pb-1 truncate" title={currentRootLabel}>{currentRootLabel}</div>
            {(rootLoading || sshLoading) && (
              <div className="flex items-center gap-2 px-2 py-4 text-gray-400 text-sm">
                <Loader2 size={14} className="animate-spin" /> Loading...
              </div>
            )}
            {currentEntries.length === 0 && !rootLoading && !sshLoading && (
              <div className="text-xs text-gray-600 px-2 py-2">empty directory</div>
            )}
            {currentEntries.map((entry) => (
              <TreeNode
                key={entry.path}
                entry={entry}
                selectedPath={selectedFile}
                onSelect={mode === 'local' ? handleLocalSelect : handleSshSelect}
                fetchChildren={mode === 'local' ? localFetchChildren : sshFetchChildren}
              />
            ))}
          </div>

          {/* File viewer */}
          <div className="flex-1 min-h-80 bg-gray-800 rounded-lg overflow-hidden flex flex-col">
            {!selectedFile && (
              <div className="flex-1 flex items-center justify-center text-gray-600 text-sm">
                Select a file to preview
              </div>
            )}
            {selectedFile && (
              <>
                <div className="px-4 py-2 border-b border-gray-700 text-xs text-gray-400 flex items-center gap-2">
                  <span className="truncate flex-1" title={selectedFile}>{selectedFile}</span>
                  <button
                    onClick={handleDownload}
                    className="flex items-center gap-1 px-2 py-1 bg-indigo-600 text-white rounded text-xs hover:bg-indigo-700 flex-shrink-0"
                    title="Download file"
                  >
                    <Download size={12} /> Download
                  </button>
                </div>
                <div className="flex-1 overflow-auto">
                  {fileLoading && (
                    <div className="flex items-center gap-2 p-4 text-gray-400 text-sm">
                      <Loader2 size={14} className="animate-spin" /> Loading...
                    </div>
                  )}
                  {fileError && (
                    <div className="flex items-center gap-2 p-4 text-red-400 text-sm">
                      <AlertCircle size={14} /> {fileError}
                    </div>
                  )}
                  {fileContent !== null && (
                    <pre className="p-4 text-xs text-gray-300 font-mono whitespace-pre-wrap break-all leading-relaxed">
                      {fileContent}
                    </pre>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function profileToCreds(p: SSHProfile) {
  return {
    host: p.host,
    port: p.port,
    username: p.username,
    ...(p.password ? { password: p.password } : {}),
    ...(p.key_path ? { key_path: p.key_path } : {}),
  };
}
