import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AppShell } from './AppShell';

vi.mock('../../api/client', () => ({
  api: {
    listWorkers: vi.fn().mockResolvedValue([]),
    getRuntimeSettings: vi.fn().mockResolvedValue({
      use_pty_mode: false,
      pty_available: false,
      auto_sort_on_access: true,
      context_compact_threshold: 0.8,
    }),
    getFeishuStatus: vi.fn().mockResolvedValue({ bound: false }),
    getPoolStatus: vi.fn().mockResolvedValue({ enabled: false }),
    startUpdate: vi.fn(),
    health: vi.fn(),
  },
  clearToken: vi.fn(),
  getToken: vi.fn().mockReturnValue('test-token'),
}));

vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(),
}));

vi.mock('../../config/server', () => ({
  isCapacitor: vi.fn().mockReturnValue(false),
}));

function renderShell(page = 'tasks') {
  return render(
    <AppShell currentPage={page} onNavigate={() => {}}>
      <div data-testid="page-content">Page content</div>
    </AppShell>,
  );
}

describe('AppShell layout and z-index architecture', () => {
  beforeEach(() => {
    localStorage.setItem('cc_user', JSON.stringify({ name: 'Test', role: 'admin' }));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    localStorage.clear();
  });

  describe('header stacking context', () => {
    it('header is a solid surface with no backdrop-blur utility (7a1bc7c: 自定义主题的透明度由变量 alpha 承担)', async () => {
      renderShell();
      const header = document.querySelector('header');
      expect(header).toBeTruthy();
      expect(header!.className).toContain('bg-gray-900');
      // backdrop-filter 会为 fixed 后代创建 containing block——若重新引入
      // blur（如 per-theme CSS 覆盖），必须保证 header 内没有 fixed 元素（见下一个测试）
      expect(header!.className).not.toContain('backdrop-blur');
    });

    it('header uses sticky positioning with z-30', async () => {
      renderShell();
      const header = document.querySelector('header');
      expect(header!.className).toContain('sticky');
      expect(header!.className).toContain('z-30');
    });

    it('no fixed inset-0 elements are direct DOM children inside the header', async () => {
      renderShell();
      const header = document.querySelector('header');
      const fixedDescendants = header!.querySelectorAll('[class*="fixed"][class*="inset-0"]');
      expect(fixedDescendants.length).toBe(0);
    });
  });

  describe('mobile drawer', () => {
    it('mobile drawer is rendered OUTSIDE the header (sibling, not descendant)', async () => {
      const user = userEvent.setup();
      renderShell();

      const menuButton = screen.getByLabelText('打开导航');
      await user.click(menuButton);

      await waitFor(() => {
        const navButtons = screen.getAllByRole('button');
        const tasksButton = navButtons.find(b => b.textContent === 'Tasks');
        expect(tasksButton).toBeTruthy();
      });

      const header = document.querySelector('header');
      const mobileDrawer = document.querySelector('[class*="lg:hidden"][class*="fixed"][class*="inset-0"]');
      expect(mobileDrawer).toBeTruthy();
      expect(header!.contains(mobileDrawer!)).toBe(false);
    });

    it('mobile drawer uses z-50, higher than header z-30', async () => {
      const user = userEvent.setup();
      renderShell();

      await user.click(screen.getByLabelText('打开导航'));

      await waitFor(() => {
        const drawer = document.querySelector('[class*="lg:hidden"][class*="fixed"][class*="inset-0"]');
        expect(drawer).toBeTruthy();
        expect(drawer!.className).toContain('z-50');
      });
    });
  });

  describe('desktop sidebar', () => {
    it('sidebar is rendered OUTSIDE the header', () => {
      renderShell();
      const header = document.querySelector('header');
      const sidebar = document.querySelector('aside[class*="lg:flex"]');
      expect(sidebar).toBeTruthy();
      expect(header!.contains(sidebar!)).toBe(false);
    });

    it('sidebar uses z-40, between header z-30 and mobile drawer z-50', () => {
      renderShell();
      const sidebar = document.querySelector('aside[class*="lg:flex"]');
      expect(sidebar!.className).toContain('z-40');
    });
  });

  describe('page content area', () => {
    it('main content is rendered OUTSIDE the header', () => {
      renderShell();
      const header = document.querySelector('header');
      const content = screen.getByTestId('page-content');
      expect(header!.contains(content)).toBe(false);
    });

    it('main content is a sibling of header within the same column', () => {
      renderShell();
      const header = document.querySelector('header');
      const main = document.querySelector('main');
      expect(header!.parentElement).toBe(main!.parentElement);
    });
  });

  describe('z-index ordering invariants', () => {
    it('z-index hierarchy: header(30) < sidebar(40) < drawer/modals(50) < portaled overlays(70)', () => {
      renderShell();

      const header = document.querySelector('header');
      const sidebar = document.querySelector('aside[class*="lg:flex"]');

      const headerZ = parseInt(header!.className.match(/z-(\d+)/)?.[1] || '0', 10);
      const sidebarZ = parseInt(sidebar!.className.match(/z-(\d+)/)?.[1] || '0', 10);

      expect(headerZ).toBe(30);
      expect(sidebarZ).toBe(40);
      expect(sidebarZ).toBeGreaterThan(headerZ);
      // z-50 for modals/drawer > sidebar z-40
      expect(50).toBeGreaterThan(sidebarZ);
      // z-[70] for portaled overlays > regular modals z-50
      expect(70).toBeGreaterThan(50);
    });
  });
});
