import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatView } from './ChatView';
import type { Task, Project, ChatMessage } from '../../api/client';

// Mock dependencies
vi.mock('../../api/client', () => ({
  api: {
    getTaskChatHistory: vi.fn().mockResolvedValue([]),
    sendTaskChat: vi.fn().mockResolvedValue({}),
    updateTask: vi.fn().mockResolvedValue({}),
    stopTaskSession: vi.fn().mockResolvedValue({}),
    uploadImages: vi.fn().mockResolvedValue([]),
    listMonitorSessions: vi.fn().mockResolvedValue([]),
    getAskUserPending: vi.fn().mockResolvedValue({ pending: [] }),
    getRuntimeSettings: vi.fn().mockResolvedValue({ use_pty_mode: false, pty_available: false }),
    config: vi.fn().mockResolvedValue({ model_options: ['claude-opus-4-6'], codex_model_options: [] }),
    injectTaskMessage: vi.fn().mockResolvedValue({ ok: true, injected: true }),
    listQuickPhrases: vi.fn().mockResolvedValue([]),
    createQuickPhrase: vi.fn().mockResolvedValue({}),
    updateQuickPhrase: vi.fn().mockResolvedValue({}),
    deleteQuickPhrase: vi.fn().mockResolvedValue({}),
    getMessageDetail: vi.fn().mockResolvedValue({}),
    getMonitorChecks: vi.fn().mockResolvedValue([]),
    deleteMonitorSession: vi.fn().mockResolvedValue({}),
    resolvePermission: vi.fn().mockResolvedValue({}),
    submitAskUser: vi.fn().mockResolvedValue({}),
    starTask: vi.fn().mockResolvedValue({}),
    distillTask: vi.fn().mockResolvedValue({}),
    saveDistilledSkill: vi.fn().mockResolvedValue({}),
  },
}));

// Store the onMessage/onReconnect callbacks so tests can trigger them
let capturedOnReconnect: (() => void) | undefined;
let capturedOnMessage: ((msg: Record<string, unknown>) => void) | undefined;
vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn((_channels: string[], onMessage?: unknown, onReconnect?: () => void) => {
    capturedOnMessage = onMessage as typeof capturedOnMessage;
    capturedOnReconnect = onReconnect;
    return { lastMessage: null, isConnected: true };
  }),
}));

vi.mock('../Secrets/SecretPicker', () => ({
  SecretPicker: () => null,
}));

import { api } from '../../api/client';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    title: '',
    description: 'Initial task prompt here',
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
    must_complete: false,
    plan_content: null,
    plan_approved: null,
    starred: false,
    archived: false,
    has_unread: false,
    session_id: 'session-123',
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

describe('ChatView', () => {
  const projects: Project[] = [];
  const onBack = vi.fn();
  const onTaskUpdated = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  });

  describe('Initial prompt bubble', () => {
    it('renders initial prompt as first bubble', async () => {
      const task = makeTask({ title: 'Has Title', description: 'Build a login page' });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      expect(screen.getByText('— Initial Prompt —')).toBeInTheDocument();
      // Description appears in the initial prompt bubble (header shows title instead)
      expect(screen.getByText('Build a login page')).toBeInTheDocument();
    });

    it('shows timestamp on initial prompt bubble from task.created_at', async () => {
      const task = makeTask({ description: 'Hello', created_at: '2024-06-15T10:30:00Z' });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      // The initial prompt bubble should contain a MessageTimestamp span
      const initialPromptDiv = container.querySelector('[data-user-msg]')!;
      expect(initialPromptDiv).toBeInTheDocument();
      // Look for the timestamp span (text-[10px] is the MessageTimestamp class)
      const timestampSpan = initialPromptDiv.querySelector('span.select-none');
      expect(timestampSpan).toBeInTheDocument();
      expect(timestampSpan!.textContent).toBeTruthy();
    });

    it('does not show timestamp on initial prompt when created_at is missing', async () => {
      const task = makeTask({ description: 'Hello', created_at: '' });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      const initialPromptDiv = container.querySelector('[data-user-msg]')!;
      expect(initialPromptDiv).toBeInTheDocument();
      // No timestamp span should appear
      const timestampSpan = initialPromptDiv.querySelector('span.select-none');
      expect(timestampSpan).not.toBeInTheDocument();
    });

    it('does not render initial prompt bubble when description is null', async () => {
      const task = makeTask({ description: null });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      expect(screen.queryByText('— Initial Prompt —')).not.toBeInTheDocument();
    });
  });

  describe('Title display', () => {
    it('shows title when set', () => {
      const task = makeTask({ title: 'Custom Title', description: 'Some prompt' });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      expect(screen.getByText('Custom Title')).toBeInTheDocument();
    });

    it('falls back to description when title is empty', () => {
      const task = makeTask({ title: '', description: 'The prompt' });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      // Description appears both in header (as title fallback) and in initial prompt bubble
      const matches = screen.getAllByText('The prompt');
      expect(matches.length).toBeGreaterThanOrEqual(2);
    });

    it('shows Untitled when both title and description are empty', () => {
      const task = makeTask({ title: '', description: null });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      expect(screen.getByText('Untitled')).toBeInTheDocument();
    });
  });

  describe('Title editing', () => {
    it('enters edit mode on pencil click', async () => {
      const task = makeTask({ title: 'My Title' });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      const editBtn = screen.getByTitle('Edit title');
      await userEvent.click(editBtn);

      expect(screen.getByPlaceholderText('Enter title...')).toBeInTheDocument();
    });

    it('saves title on Enter and calls onTaskUpdated', async () => {
      const task = makeTask({ id: 5, title: 'Old Title' });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      await userEvent.click(screen.getByTitle('Edit title'));
      const input = screen.getByPlaceholderText('Enter title...');
      await userEvent.clear(input);
      await userEvent.type(input, 'New Title{Enter}');

      expect(api.updateTask).toHaveBeenCalledWith(5, { title: 'New Title' });
      expect(onTaskUpdated).toHaveBeenCalled();
    });

    it('cancels editing on Escape without saving', async () => {
      const task = makeTask({ title: 'Keep' });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      await userEvent.click(screen.getByTitle('Edit title'));
      const input = screen.getByPlaceholderText('Enter title...');
      await userEvent.type(input, 'Nope');
      await userEvent.keyboard('{Escape}');

      expect(api.updateTask).not.toHaveBeenCalled();
    });
  });

  describe('Scroll container', () => {
    it('message container has min-h-0 for proper flex scrolling', () => {
      const task = makeTask();
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      const messageContainer = container.querySelector('.overflow-y-auto.min-h-0');
      expect(messageContainer).toBeInTheDocument();
    });
  });

  describe('Textarea auto-resize', () => {
    it('textarea has ref and auto-resize classes', () => {
      const task = makeTask();
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      const textarea = container.querySelector('textarea');
      expect(textarea).toBeInTheDocument();
      expect(textarea?.className).toContain('max-h-48');
      expect(textarea?.className).toContain('overflow-y-auto');
      expect(textarea?.className).toContain('resize-none');
    });

    it('adjusts height when input changes', async () => {
      const task = makeTask();
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      const textarea = container.querySelector('textarea')!;
      // Mock scrollHeight
      Object.defineProperty(textarea, 'scrollHeight', { value: 80, configurable: true });

      await userEvent.type(textarea, 'Line 1\nLine 2\nLine 3');

      expect(textarea.style.height).toBe('80px');
    });

    it('resets height when input is cleared', async () => {
      const task = makeTask();
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      const textarea = container.querySelector('textarea')!;
      Object.defineProperty(textarea, 'scrollHeight', { value: 80, configurable: true });

      await userEvent.type(textarea, 'Hello');
      expect(textarea.style.height).toBe('80px');

      // Simulate clearing input and smaller scrollHeight
      Object.defineProperty(textarea, 'scrollHeight', { value: 40, configurable: true });
      await userEvent.clear(textarea);
      expect(textarea.style.height).toBe('40px');
    });
  });

  describe('Back button', () => {
    it('calls onBack when back button clicked', async () => {
      const task = makeTask();
      render(<ChatView task={task} projects={projects} onBack={onBack} />);

      const backButtons = screen.getAllByRole('button');
      // First button is the back arrow
      await userEvent.click(backButtons[0]);
      expect(onBack).toHaveBeenCalled();
    });
  });

  describe('Chat history loading', () => {
    it('loads chat history on mount', async () => {
      const task = makeTask({ id: 42 });
      render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        // compact=true, limit=HISTORY_PAGE_SIZE (paginated initial load)
        expect(api.getTaskChatHistory).toHaveBeenCalledWith(42, true, 200, 0, true);
      });
    });

    it('re-fetches chat history on WebSocket reconnect', async () => {
      const msgs: ChatMessage[] = [
        { id: 1, role: 'assistant', event_type: 'message', content: 'Hello', tool_name: null, tool_input: null, tool_output: null, is_error: false, loop_iteration: null, timestamp: '2024-01-01T00:00:00Z' },
      ];
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ id: 10 });
      render(<ChatView task={task} projects={projects} onBack={onBack} />);

      // Wait for initial load
      await waitFor(() => {
        expect(api.getTaskChatHistory).toHaveBeenCalledTimes(1);
      });

      // Simulate WebSocket reconnection
      capturedOnReconnect?.();

      await waitFor(() => {
        expect(api.getTaskChatHistory).toHaveBeenCalledTimes(2);
      });
    });

    it('passes onReconnect callback to useWebSocket', () => {
      const task = makeTask();
      render(<ChatView task={task} projects={projects} onBack={onBack} />);

      expect(capturedOnReconnect).toBeDefined();
      expect(typeof capturedOnReconnect).toBe('function');
    });

    it('copies a user message without its sender prefix', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.assign(navigator, { clipboard: { writeText } });
      const msgs: ChatMessage[] = [{
        id: 1,
        role: 'user',
        event_type: 'user_message',
        content: '[Admin] 现在进度怎么样了',
        tool_name: null,
        tool_input: null,
        tool_output: null,
        is_error: false,
        loop_iteration: null,
        timestamp: '2024-01-01T00:00:00Z',
        image_urls: null,
        attachments: null,
      }];
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);

      render(<ChatView task={makeTask({ description: null })} projects={projects} onBack={onBack} />);

      await userEvent.click(await screen.findByTitle('Copy message'));

      expect(writeText).toHaveBeenCalledWith('现在进度怎么样了');
    });
  });

  describe('User message navigation', () => {
    function makeChatMessages(count: number): ChatMessage[] {
      const msgs: ChatMessage[] = [];
      for (let i = 0; i < count; i++) {
        msgs.push({
          id: i * 2 + 1,
          role: 'user',
          event_type: 'user_message',
          content: `User message ${i + 1}`,
          tool_name: null,
          tool_input: null,
          tool_output: null,
          is_error: false,
          loop_iteration: null,
          timestamp: '2024-01-01T00:00:00Z',
          image_urls: null,
          attachments: null,
        });
        msgs.push({
          id: i * 2 + 2,
          role: 'assistant',
          event_type: 'message',
          content: `Assistant response ${i + 1}`,
          tool_name: null,
          tool_input: null,
          tool_output: null,
          is_error: false,
          loop_iteration: null,
          timestamp: '2024-01-01T00:01:00Z',
          image_urls: null,
          attachments: null,
        });
      }
      return msgs;
    }

    it('shows navigation buttons even with fewer than 2 user messages (always visible since e049d57)', async () => {
      const msgs = makeChatMessages(0);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: 'Only one user msg' });
      render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(api.getTaskChatHistory).toHaveBeenCalled();
      });

      // e049d57 起导航按钮常驻工具栏右侧（不再按消息数量显隐）
      expect(screen.getByTitle('Previous user message')).toBeInTheDocument();
      expect(screen.getByTitle('Next user message')).toBeInTheDocument();
    });

    it('shows navigation buttons when 2+ user messages exist (description + 1 chat msg)', async () => {
      const msgs = makeChatMessages(1);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: 'Initial prompt' });
      render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(screen.getByTitle('Previous user message')).toBeInTheDocument();
      });
      expect(screen.getByTitle('Next user message')).toBeInTheDocument();
    });

    it('shows navigation buttons when 2+ chat user messages exist (no description)', async () => {
      const msgs = makeChatMessages(2);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: null });
      render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(screen.getByTitle('Previous user message')).toBeInTheDocument();
      });
      expect(screen.getByTitle('Next user message')).toBeInTheDocument();
    });

    it('marks initial prompt with data-user-msg attribute', async () => {
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      const task = makeTask({ description: 'Initial prompt text' });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(api.getTaskChatHistory).toHaveBeenCalled();
      });

      const userMsgNodes = container.querySelectorAll('[data-user-msg]');
      expect(userMsgNodes.length).toBe(1);
    });

    it('marks user chat messages with data-user-msg attribute', async () => {
      const msgs = makeChatMessages(3);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: 'Prompt' });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(container.querySelectorAll('[data-user-msg]').length).toBe(4);
      });
    });

    it('does not mark assistant messages with data-user-msg attribute', async () => {
      const msgs = makeChatMessages(2);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: null });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(container.querySelectorAll('[data-user-msg]').length).toBe(2);
      });

      const allMsgDivs = container.querySelectorAll('.items-start');
      allMsgDivs.forEach((div) => {
        expect(div).not.toHaveAttribute('data-user-msg');
      });
    });

    it('clicking "Previous user message" calls scrollIntoView on a user message element', async () => {
      const msgs = makeChatMessages(3);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: 'Prompt' });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(screen.getByTitle('Previous user message')).toBeInTheDocument();
      });

      const scrollContainer = container.querySelector('.overflow-y-auto')!;
      const userMsgNodes = scrollContainer.querySelectorAll('[data-user-msg]');

      // Navigation is getBoundingClientRect-based: container top = 100;
      // nodes below except nodes[2], which sits above the viewport (top = 0)
      (scrollContainer as HTMLElement).getBoundingClientRect = () => ({ top: 100 } as DOMRect);
      const scrollIntoViewMock = vi.fn();
      userMsgNodes.forEach((node, i) => {
        (node as HTMLElement).scrollIntoView = scrollIntoViewMock;
        (node as HTMLElement).getBoundingClientRect = () => ({ top: i === 2 ? 0 : 200 } as DOMRect);
      });

      await userEvent.click(screen.getByTitle('Previous user message'));

      expect(scrollIntoViewMock).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' });
    });

    it('clicking "Next user message" calls scrollIntoView on the next user message', async () => {
      const msgs = makeChatMessages(3);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: 'Prompt' });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(screen.getByTitle('Next user message')).toBeInTheDocument();
      });

      const scrollContainer = container.querySelector('.overflow-y-auto')!;
      const userMsgNodes = scrollContainer.querySelectorAll('[data-user-msg]');

      // Container top = 100; all nodes below the viewport top (top = 200)
      // → "down" navigates to the first node past container top + threshold
      (scrollContainer as HTMLElement).getBoundingClientRect = () => ({ top: 100 } as DOMRect);
      const scrollIntoViewMock = vi.fn();
      userMsgNodes.forEach((node) => {
        (node as HTMLElement).scrollIntoView = scrollIntoViewMock;
        (node as HTMLElement).getBoundingClientRect = () => ({ top: 200 } as DOMRect);
      });

      await userEvent.click(screen.getByTitle('Next user message'));

      expect(scrollIntoViewMock).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' });
    });

    it('does nothing when already at the top and clicking up', async () => {
      const msgs = makeChatMessages(2);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: 'Prompt' });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(screen.getByTitle('Previous user message')).toBeInTheDocument();
      });

      const scrollContainer = container.querySelector('.overflow-y-auto')!;
      const userMsgNodes = scrollContainer.querySelectorAll('[data-user-msg]');

      const scrollIntoViewMock = vi.fn();
      userMsgNodes.forEach((node, i) => {
        (node as HTMLElement).scrollIntoView = scrollIntoViewMock;
        Object.defineProperty(node, 'offsetTop', { value: i * 300 + 100, configurable: true });
      });

      Object.defineProperty(scrollContainer, 'scrollTop', { value: 0, configurable: true, writable: true });

      await userEvent.click(screen.getByTitle('Previous user message'));

      expect(scrollIntoViewMock).not.toHaveBeenCalled();
    });

    it('does nothing when already at the last user message and clicking down', async () => {
      const msgs = makeChatMessages(2);
      (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue(msgs);
      const task = makeTask({ description: 'Prompt' });
      const { container } = render(<ChatView task={task} projects={projects} onBack={onBack} />);

      await waitFor(() => {
        expect(screen.getByTitle('Next user message')).toBeInTheDocument();
      });

      const scrollContainer = container.querySelector('.overflow-y-auto')!;
      const userMsgNodes = scrollContainer.querySelectorAll('[data-user-msg]');

      const scrollIntoViewMock = vi.fn();
      userMsgNodes.forEach((node, i) => {
        (node as HTMLElement).scrollIntoView = scrollIntoViewMock;
        Object.defineProperty(node, 'offsetTop', { value: i * 100, configurable: true });
      });

      Object.defineProperty(scrollContainer, 'scrollTop', { value: 9999, configurable: true, writable: true });

      await userEvent.click(screen.getByTitle('Next user message'));

      expect(scrollIntoViewMock).not.toHaveBeenCalled();
    });
  });

  describe('Draft buffering (localStorage)', () => {
    const draftKey = (id: number) => `ccm-chat-draft-${id}`;

    beforeEach(() => {
      localStorage.clear();
    });

    it('persists typed input to localStorage', async () => {
      const task = makeTask({ id: 7 });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      const textarea = screen.getByPlaceholderText(/follow-up message/i);
      fireEvent.change(textarea, { target: { value: 'unsent draft' } });

      await waitFor(() => {
        expect(localStorage.getItem(draftKey(7))).toBe('unsent draft');
      });
    });

    it('restores the draft when re-entering the chat', async () => {
      localStorage.setItem(draftKey(7), 'restored draft');
      const task = makeTask({ id: 7 });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      const textarea = screen.getByPlaceholderText(/follow-up message/i) as HTMLTextAreaElement;
      expect(textarea.value).toBe('restored draft');
    });

    it('does not leak drafts between tasks', async () => {
      localStorage.setItem(draftKey(7), 'task seven draft');
      const task = makeTask({ id: 8 });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      const textarea = screen.getByPlaceholderText(/follow-up message/i) as HTMLTextAreaElement;
      expect(textarea.value).toBe('');
    });

    it('clears the draft after sending', async () => {
      const task = makeTask({ id: 7 });
      render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);

      const textarea = screen.getByPlaceholderText(/follow-up message/i);
      fireEvent.change(textarea, { target: { value: 'about to send' } });
      await waitFor(() => expect(localStorage.getItem(draftKey(7))).toBe('about to send'));

      fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter', ctrlKey: true });

      await waitFor(() => {
        expect(localStorage.getItem(draftKey(7))).toBeNull();
      });
    });
  });
});

describe('聊天图片附件展示（2026-07-16 用户反馈：发图后图片不显示）', () => {
  const projects: Project[] = [];
  const onBack = vi.fn();
  const onTaskUpdated = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    delete (window as Record<string, unknown>).Capacitor;
  });

  function wsUserMessage(taskId: number, data: Record<string, unknown>) {
    capturedOnMessage!({
      channel: `task:${taskId}`,
      data: { event_type: 'user_message', ...data },
    });
  }

  it('WS user_message 与已展示消息内容重复时，附件必须合并进已展示消息（去重不能吃掉图片）', async () => {
    const task = makeTask({ id: 11 });
    render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);
    await waitFor(() => expect(api.getTaskChatHistory).toHaveBeenCalled());

    // 第一条：乐观回显场景 —— 只有文字、无附件
    await act(async () => {
      wsUserMessage(11, { content: '看下这张截图', image_urls: null, attachments: null });
    });
    expect(screen.getByText('看下这张截图')).toBeInTheDocument();

    // 第二条：服务端广播 —— 同样内容但带图片附件（真实发送时后端会广播这条）
    await act(async () => {
      wsUserMessage(11, {
        content: '看下这张截图',
        image_urls: ['/api/uploads/shot.png'],
        attachments: [{ url: '/api/uploads/shot.png', name: 'shot.png', is_image: true }],
      });
    });

    // 不应产生重复文本消息
    expect(screen.getAllByText('看下这张截图')).toHaveLength(1);
    // 但图片必须显示出来（去重时合并附件，而不是整条丢弃）
    const imgs = document.querySelectorAll('img[src*="/api/uploads/shot.png"]');
    expect(imgs.length).toBeGreaterThan(0);
  });

  it('Capacitor（手机 App）下附件相对 URL 必须拼上远程服务器地址', async () => {
    (window as Record<string, unknown>).Capacitor = {};
    localStorage.setItem('cc_server_url', 'https://ccm.example.com');
    const task = makeTask({ id: 12 });
    render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);
    await waitFor(() => expect(api.getTaskChatHistory).toHaveBeenCalled());

    await act(async () => {
      wsUserMessage(12, {
        content: '手机发图',
        image_urls: ['/api/uploads/phone.png'],
        attachments: [
          { url: '/api/uploads/phone.png', name: 'phone.png', is_image: true },
          { url: '/api/uploads/doc.pdf', name: 'doc.pdf', is_image: false },
        ],
      });
    });

    const img = document.querySelector('img[src="https://ccm.example.com/api/uploads/phone.png"]');
    expect(img).not.toBeNull();
    const link = document.querySelector('a[href="https://ccm.example.com/api/uploads/doc.pdf"]');
    expect(link).not.toBeNull();
  });

  it('初始 Prompt 气泡渲染 task.metadata_.attachments 里的图片', () => {
    const task = makeTask({
      id: 13,
      description: '看图建任务',
      metadata_: {
        attachments: [{ url: '/api/uploads/init.png', name: 'init.png', is_image: true }],
      },
    } as Partial<Task>);
    render(<ChatView task={task} projects={projects} onBack={onBack} onTaskUpdated={onTaskUpdated} />);
    const img = document.querySelector('img[src*="/api/uploads/init.png"]');
    expect(img).not.toBeNull();
  });
});

describe('Codex app-server 增量消息', () => {
  const projects: Project[] = [];
  const onBack = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (api.getTaskChatHistory as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  });

  it('按 item_id 合并 delta，并用最终消息原位替换而不重复', async () => {
    const task = makeTask({ id: 21, provider: 'codex' });
    render(<ChatView task={task} projects={projects} onBack={onBack} />);
    await waitFor(() => expect(api.getTaskChatHistory).toHaveBeenCalled());

    await act(async () => {
      capturedOnMessage!({
        channel: 'task:21',
        data: { event_type: 'message_delta', item_id: 'msg-1', content: 'Hel' },
      });
      capturedOnMessage!({
        channel: 'task:21',
        data: { event_type: 'message_delta', item_id: 'msg-1', content: 'lo' },
      });
    });
    expect(screen.getAllByText('Hello')).toHaveLength(1);

    await act(async () => {
      capturedOnMessage!({
        channel: 'task:21',
        data: {
          event_type: 'message', item_id: 'msg-1', role: 'assistant',
          content: 'Hello', is_error: false,
        },
      });
    });
    expect(screen.getAllByText('Hello')).toHaveLength(1);
  });
});
