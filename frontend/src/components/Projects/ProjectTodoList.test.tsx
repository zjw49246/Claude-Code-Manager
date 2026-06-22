import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProjectTodoList } from './ProjectTodoList';

vi.mock('../../api/client', () => ({
  api: {
    listProjectTodos: vi.fn(),
    createProjectTodo: vi.fn(),
    updateProjectTodo: vi.fn(),
    deleteProjectTodo: vi.fn(),
    createTask: vi.fn(),
  },
}));

import { api } from '../../api/client';

const todo = {
  id: 5,
  project_id: 7,
  title: 'Refactor auth',
  prompt: 'Inspect auth module first.',
  status: 'open' as const,
  sort_order: 100,
  created_at: '2026-06-22T00:00:00Z',
  updated_at: '2026-06-22T00:00:00Z',
};

describe('ProjectTodoList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listProjectTodos).mockResolvedValue([]);
    vi.mocked(api.createProjectTodo).mockResolvedValue(todo);
    vi.mocked(api.updateProjectTodo).mockResolvedValue({ ...todo, status: 'done' });
    vi.mocked(api.deleteProjectTodo).mockResolvedValue({ ok: true });
    vi.mocked(api.createTask).mockResolvedValue({ id: 42 } as Awaited<ReturnType<typeof api.createTask>>);
    window.location.hash = '';
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('creates a project todo from the add modal', async () => {
    render(<ProjectTodoList projectId={7} />);

    await userEvent.click(screen.getByTitle('Add todo'));
    const dialog = screen.getByRole('dialog', { name: 'New todo' });
    await userEvent.type(within(dialog).getByLabelText('Title'), 'Refactor auth');
    await userEvent.type(within(dialog).getByLabelText('Prompt'), 'Inspect auth module first.');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create todo' }));

    await waitFor(() => {
      expect(api.createProjectTodo).toHaveBeenCalledWith(7, {
        title: 'Refactor auth',
        prompt: 'Inspect auth module first.',
      });
    });
  });

  it('creates a task from a todo after allowing prompt edits', async () => {
    vi.mocked(api.listProjectTodos).mockResolvedValue([todo]);
    render(<ProjectTodoList projectId={7} />);

    await userEvent.click(screen.getByTitle('Expand todos'));
    expect(await screen.findByText('Refactor auth')).toBeInTheDocument();

    await userEvent.click(screen.getByTitle('Create task'));
    const dialog = screen.getByRole('dialog', { name: 'Create task' });
    const prompt = within(dialog).getByLabelText('Prompt');
    await userEvent.clear(prompt);
    await userEvent.type(prompt, 'Write a focused patch.');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create task' }));

    await waitFor(() => {
      expect(api.createTask).toHaveBeenCalledWith({
        title: 'Refactor auth',
        description: 'Write a focused patch.',
        project_id: 7,
      });
    });
    expect(window.location.hash).toBe('#/tasks/chat/42');
  });

  it('archives todos without keeping them in the visible list', async () => {
    vi.mocked(api.listProjectTodos).mockResolvedValue([todo]);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<ProjectTodoList projectId={7} />);

    await userEvent.click(screen.getByTitle('Expand todos'));
    expect(await screen.findByText('Refactor auth')).toBeInTheDocument();
    await userEvent.click(screen.getByTitle('Archive todo'));

    await waitFor(() => {
      expect(api.deleteProjectTodo).toHaveBeenCalledWith(7, 5);
      expect(screen.queryByText('Refactor auth')).not.toBeInTheDocument();
    });
  });
});
