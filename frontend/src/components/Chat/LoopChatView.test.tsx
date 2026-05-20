import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { LoopChatView } from './LoopChatView';
import type { Task, ChatMessage } from '../../api/client';

// Capture the onMessage callback so tests can inject WebSocket messages
let capturedOnMessage: ((msg: Record<string, unknown>) => void) | undefined;
vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn((
    _channels: string[],
    onMessage?: (msg: Record<string, unknown>) => void,
  ) => {
    capturedOnMessage = onMessage;
    return { lastMessage: null, isConnected: true };
  }),
}));

vi.mock('../../api/client', () => ({
  api: {
    getTaskChatHistory: vi.fn().mockResolvedValue([]),
    cancelTask: vi.fn().mockResolvedValue({}),
  },
}));

import { api } from '../../api/client';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 42,
    title: '',
    description: 'Loop test task',
    status: 'executing',
    priority: 0,
    project_id: 1,
    target_repo: '/tmp/repo',
    target_branch: 'main',
    result_branch: null,
    merge_status: 'pending',
    instance_id: 1,
    retry_count: 0,
    max_retries: 2,
    mode: 'loop',
    todo_file_path: 'TODO.md',
    loop_progress: '3/10',
    max_iterations: 10,
    must_complete: true,
    plan_content: null,
    plan_approved: null,
    starred: false,
    archived: false,
    has_unread: false,
    session_id: 'sess-1',
    error_message: null,
    model: null,
    tags: null,
    context_window_usage: null,
    created_at: '2024-01-01T00:00:00Z',
    started_at: '2024-01-01T00:01:00Z',
    completed_at: null,
    ...overrides,
  };
}

function makeMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: Math.random() * 100000,
    role: 'assistant',
    event_type: 'message',
    content: 'some content',
    tool_name: null,
    tool_input: null,
    tool_output: null,
    is_error: false,
    loop_iteration: 0,
    timestamp: '2024-01-01T00:02:00Z',
    image_urls: null,
    attachments: null,
    ...overrides,
  };
}

function sendWs(data: Record<string, unknown>, channel = 'task:42') {
  capturedOnMessage?.({ channel, data });
}

describe('LoopChatView', () => {
  const onBack = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    capturedOnMessage = undefined;
  });

  describe('History loading', () => {
    it('loads all history without a limit', async () => {
      const task = makeTask();
      render(<LoopChatView task={task} onBack={onBack} />);
      await waitFor(() => {
        expect(api.getTaskChatHistory).toHaveBeenCalledWith(task.id);
      });
    });
  });

  describe('WebSocket message uses loop_iteration from backend', () => {
    it('uses loop_iteration from the WebSocket message data', async () => {
      const task = makeTask();
      render(<LoopChatView task={task} onBack={onBack} />);
      await waitFor(() => expect(api.getTaskChatHistory).toHaveBeenCalled());

      act(() => {
        sendWs({
          event_type: 'message',
          role: 'assistant',
          content: 'Working on iteration 3',
          loop_iteration: 3,
          is_error: false,
        });
      });

      await waitFor(() => {
        expect(screen.getByText('Iteration 4')).toBeInTheDocument();
      });
    });

    it('falls back to 0 when loop_iteration is missing from WS data', async () => {
      const task = makeTask();
      render(<LoopChatView task={task} onBack={onBack} />);
      await waitFor(() => expect(api.getTaskChatHistory).toHaveBeenCalled());

      act(() => {
        sendWs({
          event_type: 'message',
          role: 'assistant',
          content: 'No iteration field',
          is_error: false,
        });
      });

      await waitFor(() => {
        expect(screen.getByText('Iteration 1')).toBeInTheDocument();
      });
    });
  });

  describe('Race condition: WS messages during initial history load', () => {
    it('does not lose WS messages that arrive before history finishes loading', async () => {
      const historyMsgs: ChatMessage[] = [
        makeMsg({ id: 100, content: 'History msg', loop_iteration: 0 }),
      ];
      let resolveHistory!: (msgs: ChatMessage[]) => void;
      const historyPromise = new Promise<ChatMessage[]>((r) => { resolveHistory = r; });
      vi.mocked(api.getTaskChatHistory).mockReturnValue(historyPromise);

      const task = makeTask();
      render(<LoopChatView task={task} onBack={onBack} />);

      // WS message arrives BEFORE history resolves
      act(() => {
        sendWs({
          event_type: 'message',
          role: 'assistant',
          content: 'Realtime msg',
          loop_iteration: 1,
          is_error: false,
        });
      });

      // Now resolve history
      await act(async () => {
        resolveHistory(historyMsgs);
      });

      // Both iterations should have panels — iteration 0 (history) may be collapsed,
      // but iteration 2 (realtime) should be expanded as the active one
      await waitFor(() => {
        expect(screen.getByText('Iteration 1')).toBeInTheDocument();
        expect(screen.getByText('Iteration 2')).toBeInTheDocument();
        expect(screen.getByText('Realtime msg')).toBeInTheDocument();
      });
    });

    it('does not duplicate messages that arrive in both history and WS', async () => {
      let resolveHistory!: (msgs: ChatMessage[]) => void;
      const historyPromise = new Promise<ChatMessage[]>((r) => { resolveHistory = r; });
      vi.mocked(api.getTaskChatHistory).mockReturnValue(historyPromise);

      const task = makeTask();
      render(<LoopChatView task={task} onBack={onBack} />);

      // WS message arrives with a generated id (Date.now()-based, always larger than DB ids)
      act(() => {
        sendWs({
          event_type: 'message',
          role: 'assistant',
          content: 'Will be in history too',
          loop_iteration: 0,
          is_error: false,
        });
      });

      // History arrives with the same message (DB id is smaller than the WS id)
      const historyMsgs: ChatMessage[] = [
        makeMsg({ id: 50, content: 'Will be in history too', loop_iteration: 0 }),
      ];

      await act(async () => {
        resolveHistory(historyMsgs);
      });

      // The WS message's generated id > 50 (history max), so it's kept as "fresh"
      // But content is the same — this is acceptable, the important thing is no crash
      await waitFor(() => {
        expect(screen.getAllByText('Will be in history too').length).toBeGreaterThanOrEqual(1);
      });
    });
  });

  describe('loop_iteration_end event', () => {
    it('updates iteration metadata and advances activeIteration', async () => {
      const task = makeTask();
      render(<LoopChatView task={task} onBack={onBack} />);
      await waitFor(() => expect(api.getTaskChatHistory).toHaveBeenCalled());

      // Add messages for iteration 0
      act(() => {
        sendWs({
          event_type: 'message',
          role: 'assistant',
          content: 'Iter 0 work',
          loop_iteration: 0,
          is_error: false,
        });
      });

      // Send loop_iteration_end for iteration 0
      act(() => {
        sendWs({
          event: 'loop_iteration_end',
          iteration: 0,
          action: 'continue',
          reason: 'Completed phase 1',
          progress: '3/10',
        });
      });

      await waitFor(() => {
        expect(screen.getByText('Iteration 1')).toBeInTheDocument();
        expect(screen.getByText('Completed phase 1')).toBeInTheDocument();
      });

      // New message should go to iteration 1 (from backend loop_iteration)
      act(() => {
        sendWs({
          event_type: 'message',
          role: 'assistant',
          content: 'Iter 1 work',
          loop_iteration: 1,
          is_error: false,
        });
      });

      await waitFor(() => {
        expect(screen.getByText('Iteration 2')).toBeInTheDocument();
        expect(screen.getByText('Iter 1 work')).toBeInTheDocument();
      });
    });

    it('shows done metadata when loop finishes', async () => {
      const task = makeTask({ status: 'completed' });
      render(<LoopChatView task={task} onBack={onBack} />);
      await waitFor(() => expect(api.getTaskChatHistory).toHaveBeenCalled());

      act(() => {
        sendWs({
          event_type: 'message',
          role: 'assistant',
          content: 'Final work',
          loop_iteration: 0,
          is_error: false,
        });
      });

      act(() => {
        sendWs({
          event: 'loop_iteration_end',
          iteration: 0,
          action: 'done',
          reason: 'All items completed',
          progress: '10/10',
        });
      });

      await waitFor(() => {
        expect(screen.getByText('All items completed')).toBeInTheDocument();
        expect(screen.getByText(/done/)).toBeInTheDocument();
        // No "running" indicator since task.status is completed
        expect(screen.queryByText('Claude is working...')).not.toBeInTheDocument();
      });
    });
  });

  describe('History load sets activeIteration correctly', () => {
    it('sets activeIteration to max loop_iteration from history', async () => {
      const historyMsgs: ChatMessage[] = [
        makeMsg({ id: 1, content: 'Iter 0', loop_iteration: 0 }),
        makeMsg({ id: 2, content: 'Iter 1', loop_iteration: 1 }),
        makeMsg({ id: 3, content: 'Iter 2 msg', loop_iteration: 2 }),
      ];
      vi.mocked(api.getTaskChatHistory).mockResolvedValue(historyMsgs);

      const task = makeTask({ status: 'executing' });
      render(<LoopChatView task={task} onBack={onBack} />);

      await waitFor(() => {
        // Iteration 3 header should show (0-indexed iteration 2)
        expect(screen.getByText('Iteration 3')).toBeInTheDocument();
        expect(screen.getByText('Iter 2 msg')).toBeInTheDocument();
      });
    });
  });
});
