import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, cleanup, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { UpdateButton } from './UpdateButton';

vi.mock('../../api/client', () => ({
  api: {
    startUpdate: vi.fn(),
    getUpdateStatus: vi.fn(),
    rollbackUpdate: vi.fn(),
    health: vi.fn(),
  },
}));

vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(),
}));

import { api } from '../../api/client';

const mockDryRun = {
  has_updates: true,
  needs_restart: false,
  commits_behind: 3,
  current_commit: 'abc1234',
  latest_commit: 'def5678',
  commit_messages: ['fix: bug', 'feat: new feature', 'chore: cleanup'],
  has_new_migrations: false,
  has_frontend_changes: true,
  has_package_changes: false,
  active_task_count: 0,
  active_tasks: [],
  update_blocked: false,
};

function findModalOverlay(): HTMLElement | null {
  return document.body.querySelector('[class*="fixed"][class*="z-[70]"]');
}

describe('UpdateButton', () => {
  beforeEach(() => {
    vi.mocked(api.startUpdate).mockResolvedValue(mockDryRun as never);
    vi.mocked(api.getUpdateStatus).mockResolvedValue({ status: 'idle' } as never);
    localStorage.clear();
    Object.defineProperty(document, 'visibilityState', {
      value: 'visible',
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.useRealTimers();
    localStorage.clear();
  });

  it('renders the update trigger button', () => {
    render(<UpdateButton />);
    expect(screen.getByTitle('更新并重启')).toBeInTheDocument();
  });

  describe('modal portal rendering', () => {
    it('renders modal via portal on document.body when opened', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeTruthy();
      });

      const modal = findModalOverlay()!;
      expect(modal.parentElement).toBe(document.body);
    });

    it('modal is NOT inside the component render container', async () => {
      const user = userEvent.setup();
      const { container } = render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeTruthy();
      });

      expect(container.contains(findModalOverlay())).toBe(false);
    });

    it('modal uses z-[70], higher than z-50 page overlays', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeTruthy();
      });

      const modal = findModalOverlay()!;
      expect(modal.className).toContain('z-[70]');
      expect(modal.className).not.toMatch(/\bz-50\b/);
    });

    it('modal has fixed positioning with full viewport coverage and centering', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeTruthy();
      });

      const modal = findModalOverlay()!;
      expect(modal.className).toContain('fixed');
      expect(modal.className).toContain('inset-0');
      expect(modal.className).toContain('items-center');
      expect(modal.className).toContain('justify-center');
    });

    it('modal escapes a header ancestor with backdrop-blur (the root cause)', async () => {
      const user = userEvent.setup();

      const headerLike = document.createElement('header');
      headerLike.className = 'sticky top-0 z-30 bg-gray-900/85 backdrop-blur-md';
      document.body.appendChild(headerLike);

      const innerDiv = document.createElement('div');
      headerLike.appendChild(innerDiv);

      render(<UpdateButton />, { container: innerDiv });

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeTruthy();
      });

      const modal = findModalOverlay()!;
      expect(modal.parentElement).toBe(document.body);
      expect(headerLike.contains(modal)).toBe(false);

      headerLike.remove();
    });

    it('modal z-index (70) is numerically greater than page overlay z-index (50)', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeTruthy();
      });

      const modal = findModalOverlay()!;
      const match = modal.className.match(/z-\[(\d+)\]/);
      expect(match).toBeTruthy();
      expect(parseInt(match![1], 10)).toBeGreaterThan(50);
    });
  });

  describe('modal open/close behavior', () => {
    it('opens modal on update button click', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      expect(findModalOverlay()).toBeNull();

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeTruthy();
      });
    });

    it('closes modal on cancel button click', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeTruthy();
      });

      await user.click(screen.getByText('取消'));

      await waitFor(() => {
        expect(findModalOverlay()).toBeNull();
      });
    });

    it('removes portal element from body when modal closes', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        const portals = document.body.querySelectorAll('[class*="z-[70]"]');
        expect(portals.length).toBe(1);
      });

      await user.click(screen.getByText('取消'));

      await waitFor(() => {
        const portals = document.body.querySelectorAll('[class*="z-[70]"]');
        expect(portals.length).toBe(0);
      });
    });

    it('shows "已是最新版本" when no updates available', async () => {
      vi.mocked(api.startUpdate).mockResolvedValue({
        has_updates: false,
        needs_restart: false,
      } as never);

      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(screen.getByText('已是最新版本，无需更新。')).toBeInTheDocument();
      });
    });

    it('forces a fresh dry-run when the user checks manually', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(api.startUpdate).toHaveBeenCalledWith({
          dry_run: true,
          force: true,
          branch: undefined,
        });
      });
    });

    it('allows a locally pending restart when the remote check failed', async () => {
      vi.mocked(api.startUpdate).mockResolvedValue({
        has_updates: false,
        needs_restart: true,
        manual_update_detected: true,
        current_commit: 'def5678',
        running_commit: 'abc1234',
        error: 'network unavailable',
      } as never);

      const user = userEvent.setup();
      render(<UpdateButton />);
      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(screen.getByText(/磁盘代码已更新/)).toBeInTheDocument();
      });
      expect(screen.getByText(/远端更新检查失败/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '完成部署并重启' })).toBeEnabled();
      expect(screen.queryByText('检查更新失败')).not.toBeInTheDocument();
    });
  });

  describe('modal content rendering', () => {
    it('displays commit count when updates available', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(screen.getByText('3')).toBeInTheDocument();
      });
    });

    it('displays commit hashes', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(screen.getByText(/abc1234/)).toBeInTheDocument();
        expect(screen.getByText(/def5678/)).toBeInTheDocument();
      });
    });

    it('displays commit messages', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(screen.getByText('fix: bug')).toBeInTheDocument();
        expect(screen.getByText('feat: new feature')).toBeInTheDocument();
      });
    });

    it('displays frontend changes badge', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(screen.getByText('前端变更')).toBeInTheDocument();
      });
    });

    it('blocks confirmation while tasks are active', async () => {
      vi.mocked(api.startUpdate).mockResolvedValue({
        ...mockDryRun,
        active_task_count: 1,
        active_tasks: [{ id: 42, title: '正在写代码', status: 'executing' }],
        update_blocked: true,
      } as never);

      const user = userEvent.setup();
      render(<UpdateButton />);
      await user.click(screen.getByTitle('更新并重启'));

      await waitFor(() => {
        expect(screen.getByText(/当前有 1 个任务正在执行/)).toBeInTheDocument();
      });
      expect(screen.getByText(/#42 正在写代码/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '等待任务完成' })).toBeDisabled();
      expect(api.startUpdate).toHaveBeenCalledTimes(1);
    });
  });

  describe('automatic update reminder', () => {
    it('performs a dry-run and shows a non-blocking top notice after the initial delay', async () => {
      vi.useFakeTimers();
      render(<UpdateButton />);

      expect(findModalOverlay()).toBeNull();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(api.getUpdateStatus).toHaveBeenCalledTimes(1);
      expect(api.startUpdate).toHaveBeenCalledTimes(1);
      expect(api.startUpdate).toHaveBeenCalledWith({ dry_run: true });
      const notice = screen.getByTestId('update-available-notice');
      expect(notice.className).toContain('pointer-events-none');
      expect(screen.getByText('发现可用更新')).toBeInTheDocument();
      expect(screen.getByTestId('update-available-dot')).toBeInTheDocument();
      expect(findModalOverlay()).toBeNull();
    });

    it('opens the existing update modal only after the user clicks view details', async () => {
      vi.useFakeTimers();
      render(<UpdateButton />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(findModalOverlay()).toBeNull();

      await act(async () => {
        screen.getByRole('button', { name: '查看详情' }).click();
      });

      expect(screen.queryByTestId('update-available-notice')).not.toBeInTheDocument();
      expect(findModalOverlay()).toBeTruthy();
      expect(screen.getByRole('button', { name: '确认更新' })).toBeInTheDocument();
    });

    it('silently ignores automatic check failures', async () => {
      vi.useFakeTimers();
      vi.mocked(api.startUpdate).mockRejectedValue(new Error('offline'));
      render(<UpdateButton />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(findModalOverlay()).toBeNull();
      expect(screen.queryByTestId('update-available-notice')).not.toBeInTheDocument();
      expect(screen.queryByText('更新失败')).not.toBeInTheDocument();
    });

    it('stays silent when the automatic check finds the latest version', async () => {
      vi.useFakeTimers();
      vi.mocked(api.startUpdate).mockResolvedValue({
        has_updates: false,
        needs_restart: false,
      } as never);
      render(<UpdateButton />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(api.startUpdate).toHaveBeenCalledWith({ dry_run: true });
      expect(findModalOverlay()).toBeNull();
      expect(screen.queryByTestId('update-available-notice')).not.toBeInTheDocument();
      expect(screen.queryByTestId('update-available-dot')).not.toBeInTheDocument();
    });

    it('still reminds about a manual pull when the remote fetch failed', async () => {
      vi.useFakeTimers();
      vi.mocked(api.startUpdate).mockResolvedValue({
        has_updates: false,
        needs_restart: true,
        manual_update_detected: true,
        current_commit: 'def5678',
        running_commit: 'abc1234',
        error: 'network unavailable',
      } as never);
      render(<UpdateButton />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(screen.getByText('检测到待完成的本地更新')).toBeInTheDocument();
      expect(screen.getByTestId('update-available-dot')).toBeInTheDocument();
      expect(findModalOverlay()).toBeNull();

      await act(async () => {
        screen.getByRole('button', { name: '查看详情' }).click();
      });
      expect(screen.getByText(/磁盘代码已更新/)).toBeInTheDocument();
      expect(screen.getByText(/远端更新检查失败/)).toBeInTheDocument();
    });

    it('does not repeat the same reminder fingerprint during one page lifetime', async () => {
      vi.useFakeTimers();
      render(<UpdateButton />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(api.startUpdate).toHaveBeenCalledWith({ dry_run: true });
      expect(screen.getByText('发现可用更新')).toBeInTheDocument();

      await act(async () => {
        screen.getByRole('button', { name: '关闭更新提醒' }).click();
      });
      expect(screen.queryByTestId('update-available-notice')).not.toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(60 * 60_000);
      });

      expect(api.startUpdate).toHaveBeenCalledTimes(2);
      expect(findModalOverlay()).toBeNull();
      expect(screen.queryByTestId('update-available-notice')).not.toBeInTheDocument();
      expect(screen.getByTestId('update-available-dot')).toBeInTheDocument();
    });

    it('reminds again when the page is opened again', async () => {
      vi.useFakeTimers();
      const firstPage = render(<UpdateButton />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(screen.getByTestId('update-available-notice')).toBeInTheDocument();

      firstPage.unmount();
      render(<UpdateButton />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(api.startUpdate).toHaveBeenCalledTimes(2);
      expect(screen.getByTestId('update-available-notice')).toBeInTheDocument();
      expect(screen.getByText('发现可用更新')).toBeInTheDocument();
    });
  });

  describe('visibilitychange recovery', () => {
    function simulateVisibilityChange(state: 'visible' | 'hidden') {
      Object.defineProperty(document, 'visibilityState', {
        value: state,
        writable: true,
        configurable: true,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    }

    it('polls update status when page becomes visible during running phase', async () => {
      vi.mocked(api.startUpdate)
        .mockResolvedValueOnce(mockDryRun as never)
        .mockResolvedValueOnce({ update_id: 'u1', old_commit: 'abc' } as never);

      const mockStatus = {
        status: 'completed',
        old_commit: 'abc1234',
        new_commit: 'def5678',
        steps: [{ name: 'git_pull', status: 'completed', duration_ms: 500 }],
      };
      vi.mocked(api.getUpdateStatus).mockResolvedValue(mockStatus as never);

      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));
      await waitFor(() => expect(findModalOverlay()).toBeTruthy());

      const confirmBtn = screen.getAllByText('确认更新').find(el => el.tagName === 'BUTTON');
      await user.click(confirmBtn!);

      await waitFor(() => {
        expect(screen.getByText('更新中...')).toBeInTheDocument();
      });

      simulateVisibilityChange('hidden');
      simulateVisibilityChange('visible');

      await waitFor(() => {
        expect(api.getUpdateStatus).toHaveBeenCalled();
      });

      await waitFor(() => {
        expect(screen.getByText('更新完成')).toBeInTheDocument();
      });
    });

    it('does NOT poll when page becomes visible during idle phase', async () => {
      render(<UpdateButton />);

      simulateVisibilityChange('hidden');
      simulateVisibilityChange('visible');

      await new Promise(r => setTimeout(r, 50));
      expect(api.getUpdateStatus).not.toHaveBeenCalled();
    });

    it('does NOT poll when page becomes visible during confirming phase', async () => {
      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));
      await waitFor(() => expect(findModalOverlay()).toBeTruthy());

      simulateVisibilityChange('hidden');
      simulateVisibilityChange('visible');

      await new Promise(r => setTimeout(r, 50));
      expect(api.getUpdateStatus).not.toHaveBeenCalled();
    });

    it('handles failed status on visibility recovery', async () => {
      vi.mocked(api.startUpdate)
        .mockResolvedValueOnce(mockDryRun as never)
        .mockResolvedValueOnce({ update_id: 'u1', old_commit: 'abc' } as never);

      vi.mocked(api.getUpdateStatus).mockResolvedValue({
        status: 'failed',
        error: '迁移出错',
        steps: [{ name: 'alembic_upgrade', status: 'failed' }],
      } as never);

      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));
      await waitFor(() => expect(findModalOverlay()).toBeTruthy());

      const confirmBtn = screen.getAllByText('确认更新').find(el => el.tagName === 'BUTTON');
      await user.click(confirmBtn!);

      await waitFor(() => {
        expect(screen.getByText('更新中...')).toBeInTheDocument();
      });

      simulateVisibilityChange('visible');

      await waitFor(() => {
        expect(screen.getByText('更新失败')).toBeInTheDocument();
        expect(screen.getByText('迁移出错')).toBeInTheDocument();
      });
    });

    it('keeps current phase if getUpdateStatus fails (server still restarting)', async () => {
      vi.mocked(api.startUpdate)
        .mockResolvedValueOnce(mockDryRun as never)
        .mockResolvedValueOnce({ update_id: 'u1', old_commit: 'abc' } as never);

      vi.mocked(api.getUpdateStatus).mockRejectedValue(new Error('connection refused'));

      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));
      await waitFor(() => expect(findModalOverlay()).toBeTruthy());

      const confirmBtn = screen.getAllByText('确认更新').find(el => el.tagName === 'BUTTON');
      await user.click(confirmBtn!);

      await waitFor(() => {
        expect(screen.getByText('更新中...')).toBeInTheDocument();
      });

      simulateVisibilityChange('visible');

      await new Promise(r => setTimeout(r, 100));
      expect(screen.getByText('更新中...')).toBeInTheDocument();
    });

    it('does NOT trigger on hidden event (only on visible)', async () => {
      vi.mocked(api.startUpdate)
        .mockResolvedValueOnce(mockDryRun as never)
        .mockResolvedValueOnce({ update_id: 'u1', old_commit: 'abc' } as never);

      const user = userEvent.setup();
      render(<UpdateButton />);

      await user.click(screen.getByTitle('更新并重启'));
      await waitFor(() => expect(findModalOverlay()).toBeTruthy());

      const confirmBtn = screen.getAllByText('确认更新').find(el => el.tagName === 'BUTTON');
      await user.click(confirmBtn!);

      await waitFor(() => {
        expect(screen.getByText('更新中...')).toBeInTheDocument();
      });

      simulateVisibilityChange('hidden');

      await new Promise(r => setTimeout(r, 50));
      expect(api.getUpdateStatus).not.toHaveBeenCalled();
    });
  });
});
