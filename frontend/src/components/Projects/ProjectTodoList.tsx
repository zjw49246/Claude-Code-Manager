import { useCallback, useEffect, useMemo, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import {
  Archive,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Pencil,
  Play,
  Plus,
  RotateCcw,
  Save,
  Trash2,
  X,
} from '../icons';
import { api } from '../../api/client';
import type { ProjectTodo } from '../../api/client';

interface ProjectTodoListProps {
  projectId: number;
}

interface TodoDraft {
  title: string;
  prompt: string;
}

const emptyDraft: TodoDraft = { title: '', prompt: '' };

export function ProjectTodoList({ projectId }: ProjectTodoListProps) {
  const [expanded, setExpanded] = useState(false);
  const [todos, setTodos] = useState<ProjectTodo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [createDraft, setCreateDraft] = useState<TodoDraft>(emptyDraft);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<TodoDraft>(emptyDraft);
  const [taskTodo, setTaskTodo] = useState<ProjectTodo | null>(null);
  const [taskDraft, setTaskDraft] = useState<TodoDraft>(emptyDraft);
  const [taskProvider, setTaskProvider] = useState('claude');
  const [providerOptions, setProviderOptions] = useState<string[]>(['claude', 'codex']);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [updatingIds, setUpdatingIds] = useState<Set<number>>(() => new Set());

  const openCount = useMemo(() => todos.filter((todo) => todo.status === 'open').length, [todos]);

  // Track in-flight mutations per row. A single scalar would let one row's
  // completion clear another row's busy flag mid-flight and allow double-submits.
  const startBusy = (id: number) => setUpdatingIds((prev) => new Set(prev).add(id));
  const endBusy = (id: number) =>
    setUpdatingIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });

  const loadTodos = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setTodos(await api.listProjectTodos(projectId, showArchived));
      setHasLoaded(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [projectId, showArchived]);

  useEffect(() => {
    if (expanded) {
      loadTodos();
    }
  }, [expanded, loadTodos]);

  const openCreateModal = () => {
    setError('');
    setCreateDraft(emptyDraft);
    setExpanded(true);
    setShowCreate(true);
  };

  const createTodo = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    try {
      await api.createProjectTodo(projectId, createDraft);
      setShowCreate(false);
      setCreateDraft(emptyDraft);
      await loadTodos();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const startEdit = (todo: ProjectTodo) => {
    setEditingId(todo.id);
    setEditDraft({ title: todo.title, prompt: todo.prompt });
  };

  const saveEdit = async (todoId: number) => {
    if (!editDraft.title.trim() || !editDraft.prompt.trim()) {
      setError('Title and prompt are required.');
      return;
    }
    startBusy(todoId);
    setError('');
    try {
      const updated = await api.updateProjectTodo(projectId, todoId, editDraft);
      setTodos((prev) => prev.map((todo) => (todo.id === todoId ? updated : todo)));
      setEditingId(null);
    } catch (e) {
      setError(String(e));
    } finally {
      endBusy(todoId);
    }
  };

  const setStatus = async (todo: ProjectTodo, status: ProjectTodo['status']) => {
    startBusy(todo.id);
    setError('');
    try {
      const updated = await api.updateProjectTodo(projectId, todo.id, { status });
      // If the new status falls outside the current view, drop it; else update in place.
      setTodos((prev) =>
        status === 'archived' && !showArchived
          ? prev.filter((item) => item.id !== todo.id)
          : prev.map((item) => (item.id === todo.id ? updated : item)),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      endBusy(todo.id);
    }
  };

  const toggleDone = (todo: ProjectTodo) => setStatus(todo, todo.status === 'done' ? 'open' : 'done');

  const archiveTodo = (todo: ProjectTodo) => {
    if (!confirm('Archive this todo? You can restore it from "Show archived".')) return;
    setStatus(todo, 'archived');
  };

  const restoreTodo = (todo: ProjectTodo) => setStatus(todo, 'open');

  const deleteTodo = async (todo: ProjectTodo) => {
    if (!confirm('Permanently delete this todo? This cannot be undone.')) return;
    startBusy(todo.id);
    setError('');
    try {
      await api.deleteProjectTodo(projectId, todo.id);
      setTodos((prev) => prev.filter((item) => item.id !== todo.id));
    } catch (e) {
      setError(String(e));
    } finally {
      endBusy(todo.id);
    }
  };

  const openTaskModal = (todo: ProjectTodo) => {
    setError('');
    setTaskTodo(todo);
    setTaskDraft({ title: todo.title, prompt: todo.prompt });
    api.config().then((c) => {
      if (c.provider_options?.length) setProviderOptions(c.provider_options);
      if (c.default_provider) setTaskProvider(c.default_provider);
    }).catch(() => {});
  };

  const createTask = async (event: FormEvent) => {
    event.preventDefault();
    if (!taskTodo || running) return;
    setRunning(true);
    setError('');
    try {
      const task = await api.createTask({
        title: taskDraft.title,
        description: taskDraft.prompt,
        project_id: projectId,
        provider: taskProvider,
      });
      if (!task?.id) {
        throw new Error('Task was created but returned no id');
      }
      // Close the loop: mark the source todo done and record which task it spawned.
      // Best-effort — a failure here must not block navigating to the new chat.
      try {
        await api.updateProjectTodo(projectId, taskTodo.id, { status: 'done', created_task_id: task.id });
      } catch {
        /* ignore — the task was created; provenance is a nice-to-have */
      }
      window.location.hash = `#/tasks/chat/${task.id}`;
    } catch (e) {
      setError(String(e));
      setRunning(false);
    }
  };

  return (
    <div className="mt-3 border-t border-gray-700 pt-3">
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="flex h-8 min-w-0 items-center gap-2 rounded px-2 text-sm text-gray-300 hover:bg-gray-700 hover:text-foreground"
          title={expanded ? 'Collapse todos' : 'Expand todos'}
        >
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          <span className="font-medium">To-dos</span>
          {hasLoaded && (
            <span className="rounded bg-gray-700 px-1.5 py-0.5 text-xs text-gray-300">{openCount}</span>
          )}
        </button>
        <div className="flex items-center gap-2">
          {expanded && (
            <button
              type="button"
              onClick={() => setShowArchived((value) => !value)}
              className={`flex h-8 items-center gap-1.5 rounded px-2.5 text-xs ${
                showArchived ? 'bg-gray-700 text-gray-300' : 'text-gray-500 hover:bg-gray-700 hover:text-gray-300'
              }`}
              title={showArchived ? 'Hide archived todos' : 'Show archived todos'}
            >
              <Archive size={13} /> {showArchived ? 'Hide archived' : 'Show archived'}
            </button>
          )}
          <button
            type="button"
            onClick={openCreateModal}
            className="flex h-8 items-center gap-1.5 rounded bg-gray-700 px-2.5 text-sm text-gray-300 hover:bg-gray-700 hover:text-foreground"
            title="Add todo"
          >
            <Plus size={14} /> Add
          </button>
        </div>
      </div>

      {expanded && (
        <div className="mt-2 space-y-2">
          {error && <div className="rounded bg-red-500/15 px-3 py-2 text-xs text-red-400">{error}</div>}

          {loading ? (
            <div className="px-2 py-3 text-sm text-gray-500">Loading...</div>
          ) : todos.length === 0 ? (
            !error && <div className="px-2 py-3 text-sm text-gray-500">No to-dos.</div>
          ) : (
            <div className="space-y-1.5">
              {todos.map((todo) => {
                const isEditing = editingId === todo.id;
                const isBusy = updatingIds.has(todo.id);

                if (todo.status === 'archived') {
                  return (
                    <div
                      key={todo.id}
                      className="flex items-center gap-2 rounded border border-gray-700 bg-gray-900/30 px-2.5 py-1.5"
                    >
                      <span className="min-w-0 flex-1 truncate text-xs text-gray-500 line-through">{todo.title}</span>
                      <span className="shrink-0 rounded bg-gray-700 px-1.5 py-0.5 text-[10px] text-gray-500">
                        archived
                      </span>
                      <button
                        type="button"
                        onClick={() => restoreTodo(todo)}
                        disabled={isBusy}
                        className="h-7 w-7 rounded text-gray-500 hover:bg-gray-700 hover:text-blue-400 disabled:opacity-60"
                        title="Restore todo"
                      >
                        <RotateCcw size={14} />
                      </button>
                      <button
                        type="button"
                        onClick={() => deleteTodo(todo)}
                        disabled={isBusy}
                        className="h-7 w-7 rounded text-gray-500 hover:bg-gray-700 hover:text-red-400 disabled:opacity-60"
                        title="Delete permanently"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  );
                }

                return (
                  <div key={todo.id} className="rounded border border-gray-700 bg-gray-900/40 px-2.5 py-2">
                    {isEditing ? (
                      <div className="space-y-2">
                        <input
                          value={editDraft.title}
                          onChange={(e) => setEditDraft((prev) => ({ ...prev, title: e.target.value }))}
                          className="w-full rounded border border-gray-600 bg-gray-700 px-2 py-1.5 text-sm text-foreground outline-none focus:border-indigo-500"
                          placeholder="Title"
                        />
                        <textarea
                          value={editDraft.prompt}
                          onChange={(e) => setEditDraft((prev) => ({ ...prev, prompt: e.target.value }))}
                          className="min-h-24 w-full resize-y rounded border border-gray-600 bg-gray-700 px-2 py-1.5 text-sm text-foreground outline-none focus:border-indigo-500"
                          placeholder="Prompt"
                        />
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => setEditingId(null)}
                            className="flex h-8 items-center gap-1.5 rounded px-2.5 text-sm text-gray-300 hover:bg-gray-700"
                          >
                            <X size={14} /> Cancel
                          </button>
                          <button
                            type="button"
                            onClick={() => saveEdit(todo.id)}
                            disabled={isBusy}
                            className="flex h-8 items-center gap-1.5 rounded bg-indigo-600 px-2.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-60"
                          >
                            <Save size={14} /> Save
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="flex items-start gap-2">
                        <button
                          type="button"
                          onClick={() => toggleDone(todo)}
                          disabled={isBusy}
                          className="mt-0.5 h-7 w-7 shrink-0 rounded text-gray-500 hover:bg-gray-700 hover:text-green-400 disabled:opacity-60"
                          title={todo.status === 'done' ? 'Mark open' : 'Mark done'}
                        >
                          {todo.status === 'done' ? <CheckCircle2 size={17} /> : <Circle size={17} />}
                        </button>
                        <div className="min-w-0 flex-1">
                          <div className={`truncate text-sm font-medium ${todo.status === 'done' ? 'text-gray-500 line-through' : 'text-foreground'}`}>
                            {todo.title}
                          </div>
                          <div className="mt-0.5 line-clamp-2 whitespace-pre-wrap text-xs text-gray-500">{todo.prompt}</div>
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                          <button
                            type="button"
                            onClick={() => openTaskModal(todo)}
                            className="h-8 w-8 rounded text-gray-500 hover:bg-gray-700 hover:text-green-400"
                            title={todo.created_task_id ? 'Run again (already spawned a task)' : 'Create task'}
                          >
                            <Play size={15} />
                          </button>
                          <button
                            type="button"
                            onClick={() => startEdit(todo)}
                            className="h-8 w-8 rounded text-gray-500 hover:bg-gray-700 hover:text-blue-400"
                            title="Edit todo"
                          >
                            <Pencil size={15} />
                          </button>
                          <button
                            type="button"
                            onClick={() => archiveTodo(todo)}
                            disabled={isBusy}
                            className="h-8 w-8 rounded text-gray-500 hover:bg-gray-700 hover:text-yellow-400 disabled:opacity-60"
                            title="Archive todo"
                          >
                            <Archive size={15} />
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {showCreate && (
        <TodoModal
          title="New todo"
          draft={createDraft}
          setDraft={setCreateDraft}
          submitLabel="Create todo"
          saving={saving}
          error={error}
          onClose={() => setShowCreate(false)}
          onSubmit={createTodo}
        />
      )}

      {taskTodo && (
        <TodoModal
          title="Create task"
          draft={taskDraft}
          setDraft={setTaskDraft}
          submitLabel="Create task"
          saving={running}
          error={error}
          onClose={() => setTaskTodo(null)}
          onSubmit={createTask}
          extraFields={
            <label className="block space-y-1.5">
              <span className="text-sm text-gray-300">Provider</span>
              <select
                value={taskProvider}
                onChange={(e) => setTaskProvider(e.target.value)}
                className="w-full rounded border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-foreground outline-none focus:border-indigo-500"
              >
                {providerOptions.map((p) => (
                  <option key={p} value={p}>{p === 'codex' ? 'Codex' : 'Claude Code'}</option>
                ))}
              </select>
            </label>
          }
        />
      )}
    </div>
  );
}

function TodoModal({
  title,
  draft,
  setDraft,
  submitLabel,
  saving,
  error,
  onClose,
  onSubmit,
  extraFields,
}: {
  title: string;
  draft: TodoDraft;
  setDraft: (draft: TodoDraft) => void;
  submitLabel: string;
  saving: boolean;
  error?: string;
  onClose: () => void;
  onSubmit: (event: FormEvent) => void;
  extraFields?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <form
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onSubmit={onSubmit}
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl bg-gray-800 shadow-2xl"
      >
        <div className="flex items-center justify-between border-b border-gray-700 px-5 py-4">
          <h3 className="font-semibold text-foreground">{title}</h3>
          <button type="button" onClick={onClose} className="text-gray-500 hover:text-foreground">
            <X size={18} />
          </button>
        </div>
        <div className="space-y-4 overflow-y-auto p-5">
          <label className="block space-y-1.5">
            <span className="text-sm text-gray-300">Title</span>
            <input
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              className="w-full rounded border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-foreground outline-none focus:border-indigo-500"
              autoFocus
              required
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm text-gray-300">Prompt</span>
            <textarea
              value={draft.prompt}
              onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
              className="min-h-44 w-full resize-y rounded border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-foreground outline-none focus:border-indigo-500"
              required
            />
          </label>
          {extraFields}
          {error && (
            <div className="rounded bg-red-500/15 px-3 py-2 text-xs text-red-400">{error}</div>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-gray-700 px-5 py-4">
          <button
            type="button"
            onClick={onClose}
            className="rounded px-3 py-2 text-sm text-gray-300 hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving}
            className="rounded bg-indigo-600 px-3 py-2 text-sm text-white hover:bg-indigo-500 disabled:opacity-60"
          >
            {saving ? 'Saving...' : submitLabel}
          </button>
        </div>
      </form>
    </div>
  );
}
