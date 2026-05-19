import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TaskForm } from './TaskForm';

vi.mock('../../api/client', () => ({
  api: {
    listProjects: vi.fn().mockResolvedValue([
      { id: 1, name: 'test-project', git_url: '', has_remote: false, local_path: '/tmp/test', status: 'ready', show_in_selector: true, tags: [], sort_order: 0, badge_color: null, env_files: [] },
    ]),
    listTags: vi.fn().mockResolvedValue([]),
    listSecrets: vi.fn().mockResolvedValue([]),
    config: vi.fn().mockResolvedValue({
      default_model: 'claude-opus-4-6',
      model_options: ['claude-opus-4-6', 'claude-sonnet-4-6'],
      default_effort: 'medium',
      effort_options: ['low', 'medium', 'high'],
    }),
    createTask: vi.fn().mockResolvedValue({ id: 1 }),
  },
}));

import { api } from '../../api/client';

async function selectLoopMode() {
  const modeSelect = screen.getByDisplayValue('Auto (direct execute)');
  await userEvent.selectOptions(modeSelect, 'loop');
}

async function selectGoalMode() {
  const modeSelect = screen.getByDisplayValue('Auto (direct execute)');
  await userEvent.selectOptions(modeSelect, 'goal');
}

async function selectProject() {
  // ProjectSelect is a custom dropdown — click the button to open, then click the project
  const projectBtn = await waitFor(() => screen.getByText('Select project...'));
  await userEvent.click(projectBtn);
  const projectOption = await waitFor(() => screen.getByText('test-project'));
  await userEvent.click(projectOption);
}

describe('TaskForm number input fields', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('maxIterations (loop mode)', () => {
    it('allows clearing the input field completely', async () => {
      render(<TaskForm onCreated={vi.fn()} />);
      await selectLoopMode();

      const input = screen.getByDisplayValue('50');
      await userEvent.clear(input);

      expect(input).toHaveValue('');
    });

    it('normalizes empty input to 1 on blur', async () => {
      render(<TaskForm onCreated={vi.fn()} />);
      await selectLoopMode();

      const input = screen.getByDisplayValue('50');
      await userEvent.clear(input);
      fireEvent.blur(input);

      expect(input).toHaveValue('1');
    });

    it('allows typing a new value after clearing', async () => {
      render(<TaskForm onCreated={vi.fn()} />);
      await selectLoopMode();

      const input = screen.getByDisplayValue('50');
      await userEvent.clear(input);
      await userEvent.type(input, '5');

      expect(input).toHaveValue('5');
    });

    it('rejects non-numeric characters', async () => {
      render(<TaskForm onCreated={vi.fn()} />);
      await selectLoopMode();

      const input = screen.getByDisplayValue('50');
      await userEvent.clear(input);
      await userEvent.type(input, 'abc12.3xyz');

      expect(input).toHaveValue('123');
    });

    it('submits the displayed value correctly', async () => {
      const onCreated = vi.fn();
      render(<TaskForm onCreated={onCreated} />);
      await selectLoopMode();
      await selectProject();

      const input = screen.getByDisplayValue('50');
      await userEvent.clear(input);
      await userEvent.type(input, '5');

      const todoInput = screen.getByPlaceholderText('Todo file path (e.g. TODO.md)');
      await userEvent.type(todoInput, 'TODO.md');

      const submitBtn = screen.getByText('Create Task');
      await userEvent.click(submitBtn);

      await waitFor(() => {
        expect(api.createTask).toHaveBeenCalledWith(
          expect.objectContaining({ max_iterations: 5 }),
        );
      });
    });

    it('submits 1 when input was cleared (onBlur normalizes before submit)', async () => {
      const onCreated = vi.fn();
      render(<TaskForm onCreated={onCreated} />);
      await selectLoopMode();
      await selectProject();

      const input = screen.getByDisplayValue('50');
      await userEvent.clear(input);

      const todoInput = screen.getByPlaceholderText('Todo file path (e.g. TODO.md)');
      await userEvent.type(todoInput, 'TODO.md');

      const submitBtn = screen.getByText('Create Task');
      await userEvent.click(submitBtn);

      await waitFor(() => {
        expect(api.createTask).toHaveBeenCalledWith(
          expect.objectContaining({ max_iterations: 1 }),
        );
      });
    });

    it('normalizes 0 to 1 on blur', async () => {
      render(<TaskForm onCreated={vi.fn()} />);
      await selectLoopMode();

      const input = screen.getByDisplayValue('50');
      await userEvent.clear(input);
      await userEvent.type(input, '0');
      fireEvent.blur(input);

      expect(input).toHaveValue('1');
    });
  });

  describe('goalMaxTurns (goal mode)', () => {
    it('allows clearing the input field completely', async () => {
      render(<TaskForm onCreated={vi.fn()} />);
      await selectGoalMode();

      const input = screen.getByDisplayValue('30');
      await userEvent.clear(input);

      expect(input).toHaveValue('');
    });

    it('normalizes empty input to 1 on blur', async () => {
      render(<TaskForm onCreated={vi.fn()} />);
      await selectGoalMode();

      const input = screen.getByDisplayValue('30');
      await userEvent.clear(input);
      fireEvent.blur(input);

      expect(input).toHaveValue('1');
    });

    it('allows typing a new value after clearing', async () => {
      render(<TaskForm onCreated={vi.fn()} />);
      await selectGoalMode();

      const input = screen.getByDisplayValue('30');
      await userEvent.clear(input);
      await userEvent.type(input, '10');

      expect(input).toHaveValue('10');
    });

    it('submits the displayed value correctly', async () => {
      const onCreated = vi.fn();
      render(<TaskForm onCreated={onCreated} />);
      await selectGoalMode();
      await selectProject();

      const input = screen.getByDisplayValue('30');
      await userEvent.clear(input);
      await userEvent.type(input, '10');

      const descInput = screen.getByPlaceholderText('Prompt / Description (this will be sent to Claude Code)');
      await userEvent.type(descInput, 'test task');

      const goalInput = screen.getByPlaceholderText('Goal condition (e.g. all tests pass and lint is clean)');
      await userEvent.type(goalInput, 'all tests pass');

      const submitBtn = screen.getByText('Create Task');
      await userEvent.click(submitBtn);

      await waitFor(() => {
        expect(api.createTask).toHaveBeenCalledWith(
          expect.objectContaining({ goal_max_turns: 10 }),
        );
      });
    });
  });
});
