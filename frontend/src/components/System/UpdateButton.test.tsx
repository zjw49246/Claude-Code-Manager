import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
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
};

function findModalOverlay(): HTMLElement | null {
  return document.body.querySelector('[class*="fixed"][class*="z-[70]"]');
}

describe('UpdateButton', () => {
  beforeEach(() => {
    vi.mocked(api.startUpdate).mockResolvedValue(mockDryRun as never);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
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
  });
});
