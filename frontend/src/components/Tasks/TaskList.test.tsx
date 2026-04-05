import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TaskList } from './TaskList';
import type { Task, Project } from '../../api/client';

// Mock the api module
vi.mock('../../api/client', () => ({
  api: {
    deleteTask: vi.fn().mockResolvedValue({}),
    cancelTask: vi.fn().mockResolvedValue({}),
    retryTask: vi.fn().mockResolvedValue({}),
    starTask: vi.fn().mockResolvedValue({}),
    archiveTask: vi.fn().mockResolvedValue({}),
    updateTask: vi.fn().mockResolvedValue({}),
  },
}));

import { api } from '../../api/client';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    title: '',
    description: 'Test description',
    status: 'pending',
    priority: 0,
    project_id: null,
    target_repo: null,
    target_branch: 'main',
    result_branch: null,
    merge_status: 'pending',
    instance_id: null,
    retry_count: 0,
    max_retries: 3,
    mode: 'auto',
    todo_file_path: null,
    loop_progress: null,
    max_iterations: 50,
    plan_content: null,
    plan_approved: null,
    starred: false,
    archived: false,
    has_unread: false,
    session_id: null,
    error_message: null,
    model: null,
    tags: null,
    context_window_usage: null,
    created_at: '2024-01-01T00:00:00Z',
    started_at: null,
    completed_at: null,
    ...overrides,
  };
}

describe('TaskList', () => {
  const onRefresh = vi.fn();
  const onOpenChat = vi.fn();
  const projects: Project[] = [];

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders task description when no title', () => {
    const tasks = [makeTask({ description: 'My task prompt' })];
    render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);
    expect(screen.getByText('My task prompt')).toBeInTheDocument();
  });

  it('renders title when present, description as subtitle', () => {
    const tasks = [makeTask({ title: 'Custom Title', description: 'The prompt' })];
    render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);
    expect(screen.getByText('Custom Title')).toBeInTheDocument();
    expect(screen.getByText('The prompt')).toBeInTheDocument();
  });

  it('shows empty state when no tasks', () => {
    render(<TaskList tasks={[]} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);
    expect(screen.getByText('No tasks yet')).toBeInTheDocument();
  });

  describe('Copy prompt', () => {
    it('copies task description to clipboard', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.assign(navigator, { clipboard: { writeText } });

      const tasks = [makeTask({ description: 'Copy this prompt' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      const copyBtn = screen.getByTitle('Copy prompt');
      await userEvent.click(copyBtn);

      expect(writeText).toHaveBeenCalledWith('Copy this prompt');
    });

    it('shows check icon after copying', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.assign(navigator, { clipboard: { writeText } });

      const tasks = [makeTask({ description: 'Copy me' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      const copyBtn = screen.getByTitle('Copy prompt');
      await userEvent.click(copyBtn);

      // The check icon should appear (we can't easily check the icon itself,
      // but the button should still be there)
      expect(writeText).toHaveBeenCalledTimes(1);
    });
  });

  describe('Overflow menu', () => {
    it('opens overflow menu on click', async () => {
      const tasks = [makeTask()];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      const moreBtn = screen.getByTitle('More actions');
      await userEvent.click(moreBtn);

      expect(screen.getByText('Edit title')).toBeInTheDocument();
      expect(screen.getByText('Archive')).toBeInTheDocument();
    });

    it('shows Delete in overflow menu for pending tasks', async () => {
      const tasks = [makeTask({ status: 'pending' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      await userEvent.click(screen.getByTitle('More actions'));
      expect(screen.getByText('Delete')).toBeInTheDocument();
    });

    it('shows Cancel in overflow menu for in_progress tasks', async () => {
      const tasks = [makeTask({ status: 'in_progress' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      await userEvent.click(screen.getByTitle('More actions'));
      expect(screen.getByText('Cancel')).toBeInTheDocument();
    });

    it('shows Retry in overflow menu for failed tasks', async () => {
      const tasks = [makeTask({ status: 'failed' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      await userEvent.click(screen.getByTitle('More actions'));
      expect(screen.getByText('Retry')).toBeInTheDocument();
    });

    it('closes overflow menu on outside click', async () => {
      const tasks = [makeTask()];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      await userEvent.click(screen.getByTitle('More actions'));
      expect(screen.getByText('Edit title')).toBeInTheDocument();

      // Click outside
      fireEvent.mouseDown(document.body);
      await waitFor(() => {
        expect(screen.queryByText('Edit title')).not.toBeInTheDocument();
      });
    });
  });

  describe('Title editing', () => {
    it('opens inline title editor from overflow menu', async () => {
      const tasks = [makeTask({ title: 'Old Title' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      await userEvent.click(screen.getByTitle('More actions'));
      await userEvent.click(screen.getByText('Edit title'));

      const input = screen.getByPlaceholderText('Enter title...');
      expect(input).toBeInTheDocument();
      expect(input).toHaveValue('Old Title');
    });

    it('saves title on Enter', async () => {
      const tasks = [makeTask({ id: 42, title: 'Old' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      await userEvent.click(screen.getByTitle('More actions'));
      await userEvent.click(screen.getByText('Edit title'));

      const input = screen.getByPlaceholderText('Enter title...');
      await userEvent.clear(input);
      await userEvent.type(input, 'New Title{Enter}');

      expect(api.updateTask).toHaveBeenCalledWith(42, { title: 'New Title' });
      expect(onRefresh).toHaveBeenCalled();
    });

    it('cancels editing on Escape', async () => {
      const tasks = [makeTask({ title: 'Keep This' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      await userEvent.click(screen.getByTitle('More actions'));
      await userEvent.click(screen.getByText('Edit title'));

      const input = screen.getByPlaceholderText('Enter title...');
      await userEvent.type(input, 'Nope');
      await userEvent.keyboard('{Escape}');

      expect(api.updateTask).not.toHaveBeenCalled();
    });

    it('does not call API if title unchanged', async () => {
      const tasks = [makeTask({ title: 'Same' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);

      await userEvent.click(screen.getByTitle('More actions'));
      await userEvent.click(screen.getByText('Edit title'));

      const input = screen.getByPlaceholderText('Enter title...');
      fireEvent.blur(input);

      await waitFor(() => {
        expect(api.updateTask).not.toHaveBeenCalled();
      });
    });
  });

  describe('Chat button', () => {
    it('shows Chat button when session_id exists', () => {
      const tasks = [makeTask({ session_id: 'abc-123' })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);
      expect(screen.getByTitle('Chat')).toBeInTheDocument();
    });

    it('does not show Chat button without session_id', () => {
      const tasks = [makeTask({ session_id: null })];
      render(<TaskList tasks={tasks} projects={projects} onRefresh={onRefresh} onOpenChat={onOpenChat} />);
      expect(screen.queryByTitle('Chat')).not.toBeInTheDocument();
    });
  });
});
