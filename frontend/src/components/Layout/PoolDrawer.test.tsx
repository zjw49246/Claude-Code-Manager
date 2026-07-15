import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, within, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { PoolDrawer } from './PoolDrawer';

vi.mock('../../api/client', () => ({
  api: {
    getPoolStatus: vi.fn(),
    getPoolUsage: vi.fn(),
    clearPoolCooldown: vi.fn(),
    setPoolPreferred: vi.fn(),
    poolRelogin: vi.fn(),
    poolReloginStatus: vi.fn(),
    poolAddAccount: vi.fn(),
    poolAddStatus: vi.fn(),
    poolDeleteAccount: vi.fn(),
    getCcSettings: vi.fn().mockResolvedValue({ settings: {} }),
    putCcSettings: vi.fn(),
  },
}));

import { api } from '../../api/client';

const mockPoolUsage = {
  total: 2,
  available: 2,
  preferred: null,
  last_selected: 'acc-1',
  accounts: [
    {
      id: 'acc-1',
      email: 'user1@example.com',
      available: true,
      enabled: true,
      subscription_type: 'pro',
      usage: {
        five_hour: { utilization: 30, resets_at: '2026-07-15T12:00:00Z' },
        seven_day: { utilization: 50, resets_at: '2026-07-20T00:00:00Z' },
        seven_day_opus: null,
      },
      usage_error: null,
    },
  ],
};

function enablePool() {
  vi.mocked(api.getPoolStatus).mockResolvedValue({ enabled: true } as never);
  vi.mocked(api.getPoolUsage).mockResolvedValue(mockPoolUsage as never);
}

async function renderAndWaitForPro() {
  render(<PoolDrawer />);
  await waitFor(() => {
    expect(screen.getByText('Pro')).toBeInTheDocument();
  });
}

async function openDrawer(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText('Pro'));
  await waitFor(() => {
    expect(screen.getByText('Claude Pool 额度')).toBeInTheDocument();
  });
}

describe('PoolDrawer', () => {
  beforeEach(() => {
    enablePool();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders the Pro trigger button when pool is enabled', async () => {
    await renderAndWaitForPro();
    expect(screen.getByText('Pro')).toBeInTheDocument();
  });

  it('does not render anything when pool is disabled', async () => {
    vi.mocked(api.getPoolStatus).mockResolvedValue({ enabled: false } as never);
    const { container } = render(<PoolDrawer />);
    await waitFor(() => {
      expect(container.innerHTML).toBe('');
    });
  });

  describe('z-index layering (portal)', () => {
    it('renders the drawer overlay via portal on document.body', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]');
      expect(overlay).toBeTruthy();
      expect(overlay!.parentElement).toBe(document.body);
    });

    it('drawer overlay is NOT inside the component render container', async () => {
      const user = userEvent.setup();
      const { container } = render(<PoolDrawer />);
      await waitFor(() => {
        expect(screen.getByText('Pro')).toBeInTheDocument();
      });
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]');
      expect(container.contains(overlay)).toBe(false);
    });

    it('drawer overlay uses z-[70], higher than z-50 used by page overlays', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]');
      expect(overlay!.className).toContain('z-[70]');
      expect(overlay!.className).not.toMatch(/\bz-50\b/);
    });

    it('drawer overlay has fixed positioning with full viewport coverage', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]');
      expect(overlay!.className).toContain('fixed');
      expect(overlay!.className).toContain('inset-0');
    });

    it('drawer panel has safe-area-inset-top padding for mobile notch/status bar', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const panel = screen.getByText('Claude Pool 额度').closest('[class*="max-w-sm"]');
      expect(panel).toBeTruthy();
      expect(panel!.className).toContain('pt-[env(safe-area-inset-top)]');
    });

    it('drawer portal escapes a header ancestor with position:relative', async () => {
      const user = userEvent.setup();

      const headerLike = document.createElement('header');
      headerLike.className = 'bg-gray-900 border-b';
      headerLike.style.position = 'relative';
      document.body.appendChild(headerLike);

      const innerDiv = document.createElement('div');
      headerLike.appendChild(innerDiv);

      render(<PoolDrawer />, { container: innerDiv });
      await waitFor(() => {
        expect(screen.getByText('Pro')).toBeInTheDocument();
      });
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]');
      expect(overlay!.parentElement).toBe(document.body);
      expect(headerLike.contains(overlay!)).toBe(false);

      headerLike.remove();
    });

    it('drawer z-index (70) is numerically greater than ChatView z-index (50)', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const drawerOverlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]') as HTMLElement;
      const match = drawerOverlay.className.match(/z-\[(\d+)\]/);
      expect(match).toBeTruthy();
      const drawerZ = parseInt(match![1], 10);
      expect(drawerZ).toBeGreaterThan(50);
    });
  });

  describe('drawer open/close behavior', () => {
    it('opens drawer on Pro button click', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();

      expect(screen.queryByText('Claude Pool 额度')).not.toBeInTheDocument();

      await user.click(screen.getByText('Pro'));

      await waitFor(() => {
        expect(screen.getByText('Claude Pool 额度')).toBeInTheDocument();
      });
    });

    it('closes drawer on backdrop click', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]');
      const backdrop = overlay!.querySelector('[class*="bg-black"]');
      expect(backdrop).toBeTruthy();

      await user.click(backdrop!);

      await waitFor(() => {
        expect(screen.queryByText('Claude Pool 额度')).not.toBeInTheDocument();
      });
    });

    it('closes drawer on X button click', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]') as HTMLElement;
      const headerBar = within(overlay).getByText('Claude Pool 额度').closest('div[class*="border-b"]') as HTMLElement;
      const buttons = within(headerBar).getAllByRole('button');
      const closeButton = buttons[buttons.length - 1];

      await user.click(closeButton);

      await waitFor(() => {
        expect(screen.queryByText('Claude Pool 额度')).not.toBeInTheDocument();
      });
    });

    it('removes portal element from body when drawer closes', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      let portalElements = Array.from(document.body.children).filter(
        (el) => el instanceof HTMLElement && el.className.includes('z-[70]')
      );
      expect(portalElements.length).toBe(1);

      const backdrop = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]')!.querySelector('[class*="bg-black"]');
      await user.click(backdrop!);

      await waitFor(() => {
        portalElements = Array.from(document.body.children).filter(
          (el) => el instanceof HTMLElement && el.className?.includes?.('z-[70]')
        );
        expect(portalElements.length).toBe(0);
      });
    });

    it('can reopen drawer after closing and portal still targets body', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const backdrop = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]')!.querySelector('[class*="bg-black"]');
      await user.click(backdrop!);

      await waitFor(() => {
        expect(screen.queryByText('Claude Pool 额度')).not.toBeInTheDocument();
      });

      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool 额度').closest('[class*="fixed"]');
      expect(overlay!.parentElement).toBe(document.body);
    });
  });

  describe('drawer content rendering', () => {
    it('displays account info after opening', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      expect(screen.getByText('acc-1')).toBeInTheDocument();
      expect(screen.getByText('user1@example.com')).toBeInTheDocument();
      expect(screen.getByText('2/2 可用')).toBeInTheDocument();
    });

    it('displays subscription badge', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      expect(screen.getByText('pro')).toBeInTheDocument();
    });

    it('shows loading state before data loads', async () => {
      vi.mocked(api.getPoolUsage).mockImplementation(() => new Promise(() => {}));

      const user = userEvent.setup();
      await renderAndWaitForPro();

      await user.click(screen.getByText('Pro'));

      await waitFor(() => {
        expect(screen.getByText('加载中…')).toBeInTheDocument();
      });
    });
  });
});
