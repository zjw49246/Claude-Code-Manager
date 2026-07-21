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
    getCodexPoolStatus: vi.fn(),
    getCodexPoolUsage: vi.fn(),
    clearCodexPoolCooldown: vi.fn(),
    setCodexPoolPreferred: vi.fn(),
    codexPoolDeleteAccount: vi.fn(),
    codexPoolRelogin: vi.fn(),
    codexPoolReloginStatus: vi.fn(),
    codexPoolAddAccount: vi.fn(),
    codexPoolAddStatus: vi.fn(),
    codexPoolSubmitOtp: vi.fn(),
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
  vi.mocked(api.getCodexPoolStatus).mockRejectedValue(new Error('Codex pool disabled'));
}

function enableCodexPool(usage: Record<string, unknown> = { total: 0, available: 0, preferred: null, accounts: [] }) {
  vi.mocked(api.getCodexPoolStatus).mockResolvedValue({ enabled: true } as never);
  vi.mocked(api.getCodexPoolUsage).mockResolvedValue(usage as never);
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
    expect(screen.getByText('Claude Pool')).toBeInTheDocument();
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

      const overlay = screen.getByText('Claude Pool').closest('[class*="fixed"]');
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

      const overlay = screen.getByText('Claude Pool').closest('[class*="fixed"]');
      expect(container.contains(overlay)).toBe(false);
    });

    it('drawer overlay uses z-[70], higher than z-50 used by page overlays', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool').closest('[class*="fixed"]');
      expect(overlay!.className).toContain('z-[70]');
      expect(overlay!.className).not.toMatch(/\bz-50\b/);
    });

    it('drawer overlay has fixed positioning with full viewport coverage', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool').closest('[class*="fixed"]');
      expect(overlay!.className).toContain('fixed');
      expect(overlay!.className).toContain('inset-0');
    });

    it('drawer panel has safe-area-inset-top padding for mobile notch/status bar', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const panel = screen.getByText('Claude Pool').closest('[class*="max-w-sm"]');
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

      const overlay = screen.getByText('Claude Pool').closest('[class*="fixed"]');
      expect(overlay!.parentElement).toBe(document.body);
      expect(headerLike.contains(overlay!)).toBe(false);

      headerLike.remove();
    });

    it('drawer z-index (70) is numerically greater than ChatView z-index (50)', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const drawerOverlay = screen.getByText('Claude Pool').closest('[class*="fixed"]') as HTMLElement;
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

      expect(screen.queryByText('Claude Pool')).not.toBeInTheDocument();

      await user.click(screen.getByText('Pro'));

      await waitFor(() => {
        expect(screen.getByText('Claude Pool')).toBeInTheDocument();
      });
    });

    it('closes drawer on backdrop click', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool').closest('[class*="fixed"]');
      const backdrop = overlay!.querySelector('[class*="bg-black"]');
      expect(backdrop).toBeTruthy();

      await user.click(backdrop!);

      await waitFor(() => {
        expect(screen.queryByText('Claude Pool')).not.toBeInTheDocument();
      });
    });

    it('closes drawer on X button click', async () => {
      const user = userEvent.setup();
      await renderAndWaitForPro();
      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool').closest('[class*="fixed"]') as HTMLElement;
      const headerBar = within(overlay).getByText('Claude Pool').closest('div[class*="border-b"]') as HTMLElement;
      const buttons = within(headerBar).getAllByRole('button');
      const closeButton = buttons[buttons.length - 1];

      await user.click(closeButton);

      await waitFor(() => {
        expect(screen.queryByText('Claude Pool')).not.toBeInTheDocument();
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

      const backdrop = screen.getByText('Claude Pool').closest('[class*="fixed"]')!.querySelector('[class*="bg-black"]');
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

      const backdrop = screen.getByText('Claude Pool').closest('[class*="fixed"]')!.querySelector('[class*="bg-black"]');
      await user.click(backdrop!);

      await waitFor(() => {
        expect(screen.queryByText('Claude Pool')).not.toBeInTheDocument();
      });

      await openDrawer(user);

      const overlay = screen.getByText('Claude Pool').closest('[class*="fixed"]');
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

  describe('Codex account login source', () => {
    it('allows password-only login and keeps mailbox token optional', async () => {
      enableCodexPool();
      vi.mocked(api.codexPoolAddAccount).mockResolvedValue({ ok: true, status: 'running' });
      const user = userEvent.setup();

      await renderAndWaitForPro();
      await openDrawer(user);
      await user.click(screen.getByRole('button', { name: 'Codex' }));
      await waitFor(() => expect(screen.getByText('Codex Pool')).toBeInTheDocument());
      await user.click(screen.getByTitle('添加账号'));

      await user.type(screen.getByLabelText('OpenAI 邮箱'), 'password-only@mail.com');
      const addButton = screen.getByRole('button', { name: '添加' });
      expect(addButton).toBeDisabled();

      await user.type(screen.getByLabelText('OpenAI 密码（可选）'), 'openai-password');
      expect(addButton).toBeEnabled();
      await user.click(addButton);

      await waitFor(() => {
        expect(api.codexPoolAddAccount).toHaveBeenCalledWith({
          email: 'password-only@mail.com',
          token: undefined,
          password: 'openai-password',
          login_method: undefined,
        });
        expect(screen.getByLabelText('OpenAI 密码（可选）')).toHaveValue('');
      });
    });

    it('offers generic MailCatcher and sends it for a 163 mailbox', async () => {
      enableCodexPool();
      vi.mocked(api.codexPoolAddAccount).mockResolvedValue({ ok: true, status: 'running' });
      const user = userEvent.setup();

      await renderAndWaitForPro();
      await openDrawer(user);
      await user.click(screen.getByRole('button', { name: 'Codex' }));
      await waitFor(() => expect(screen.getByText('Codex Pool')).toBeInTheDocument());
      await user.click(screen.getByTitle('添加账号'));

      const methodSelect = screen.getByLabelText('验证码邮箱来源');
      expect(within(methodSelect).getByRole('option', { name: '171mail（API 接码）' })).toHaveValue('171mail');
      expect(within(methodSelect).getByRole('option', { name: 'MailCatcher（163 / mail.com / Onet / Gazeta 等）' })).toHaveValue('mailcatcher');
      expect(within(methodSelect).getByRole('option', { name: 'mail.com（MailCatcher 接码）' })).toHaveValue('mailcom');
      expect(within(methodSelect).getByRole('option', { name: 'Onet（MailCatcher 接码）' })).toHaveValue('onet');
      expect(within(methodSelect).getByRole('option', { name: 'Gazeta（MailCatcher 接码）' })).toHaveValue('gazeta');

      await user.type(screen.getByLabelText('OpenAI 邮箱'), 'test-user@163.com');
      await user.selectOptions(methodSelect, 'mailcatcher');
      await user.type(screen.getByLabelText('MailCatcher 查询 Token（可选）'), 'mail-query-token');
      await user.click(screen.getByRole('button', { name: '添加' }));

      await waitFor(() => {
        expect(api.codexPoolAddAccount).toHaveBeenCalledWith({
          email: 'test-user@163.com',
          token: 'mail-query-token',
          password: undefined,
          login_method: 'mailcatcher',
        });
        expect(screen.getByLabelText('MailCatcher 查询 Token（可选）')).toHaveValue('');
      });
    });

    it('pauses for a human email code and resumes the same login attempt', async () => {
      enableCodexPool();
      vi.mocked(api.codexPoolAddAccount).mockResolvedValue({
        ok: true,
        status: 'running',
        attempt_id: 'attempt-1',
      });
      vi.mocked(api.codexPoolAddStatus).mockResolvedValue({
        status: 'awaiting_otp',
        attempt_id: 'attempt-1',
        challenge_id: 'challenge-1',
        expires_at: Math.floor(Date.now() / 1000) + 600,
      });
      vi.mocked(api.codexPoolSubmitOtp).mockResolvedValue({
        ok: true,
        status: 'verifying_otp',
      });
      const user = userEvent.setup();

      await renderAndWaitForPro();
      await openDrawer(user);
      await user.click(screen.getByRole('button', { name: 'Codex' }));
      await waitFor(() => expect(screen.getByText('Codex Pool')).toBeInTheDocument());
      await user.click(screen.getByTitle('添加账号'));
      await user.type(screen.getByLabelText('OpenAI 邮箱'), 'human@example.com');
      await user.type(screen.getByLabelText('OpenAI 密码（可选）'), 'openai-password');
      await user.click(screen.getByRole('button', { name: '添加' }));

      const otpInput = await screen.findByLabelText('OpenAI 邮箱验证码', {}, { timeout: 3000 });
      await user.type(otpInput, '654321');
      await user.click(screen.getByRole('button', { name: '继续登录' }));

      await waitFor(() => {
        expect(api.codexPoolSubmitOtp).toHaveBeenCalledWith(
          'attempt-1',
          'challenge-1',
          '654321',
        );
      });
      expect(screen.getByText('验证码已提交，正在继续登录…')).toBeInTheDocument();
    });

    it('keeps polling an add attempt after a transient status failure', async () => {
      enableCodexPool();
      vi.mocked(api.codexPoolAddAccount).mockResolvedValue({
        ok: true,
        status: 'running',
        attempt_id: 'add-attempt-retry',
      });
      vi.mocked(api.codexPoolAddStatus)
        .mockRejectedValueOnce(new Error('temporary network error'))
        .mockResolvedValue({
          status: 'awaiting_otp',
          attempt_id: 'add-attempt-retry',
          challenge_id: 'add-challenge-retry',
          expires_at: Math.floor(Date.now() / 1000) + 600,
        });
      const user = userEvent.setup();

      await renderAndWaitForPro();
      await openDrawer(user);
      await user.click(screen.getByRole('button', { name: 'Codex' }));
      await waitFor(() => expect(screen.getByText('Codex Pool')).toBeInTheDocument());
      await user.click(screen.getByTitle('添加账号'));
      await user.type(screen.getByLabelText('OpenAI 邮箱'), 'retry@example.com');
      await user.type(screen.getByLabelText('OpenAI 密码（可选）'), 'openai-password');
      await user.click(screen.getByRole('button', { name: '添加' }));

      await waitFor(() => {
        expect(screen.getByText(/状态查询暂时失败，正在重试/)).toBeInTheDocument();
      }, { timeout: 2500 });
      await screen.findByLabelText(
        'OpenAI 邮箱验证码',
        {},
        { timeout: 5000 },
      );
      expect(api.codexPoolAddStatus).toHaveBeenCalledTimes(2);
    });
  });

  describe('Codex account controls', () => {
    const codexAccount = {
      id: 'codex-2',
      email: 'codex@example.com',
      codex_home: '/home/ubuntu/.codex-codex-2',
      enabled: true,
      available: true,
      cooldown_until: null,
      cooldown_remaining: 0,
      plan_type: 'pro',
      quota: null,
      quota_error: 'no_rollout_data',
    };

    async function openCodexTab(user: ReturnType<typeof userEvent.setup>) {
      await renderAndWaitForPro();
      await openDrawer(user);
      await user.click(screen.getByRole('button', { name: 'Codex' }));
      await waitFor(() => expect(screen.getByText('Codex Pool')).toBeInTheDocument());
    }

    it('shows each account CODEX_HOME and can set it preferred', async () => {
      enableCodexPool({
        enabled: true,
        total: 1,
        available: 1,
        cooldown: 0,
        disabled: 0,
        preferred: null,
        accounts: [codexAccount],
      });
      vi.mocked(api.setCodexPoolPreferred).mockResolvedValue({ ok: true, preferred: 'codex-2' });
      const user = userEvent.setup();

      await openCodexTab(user);

      expect(screen.getByText(`CODEX_HOME: ${codexAccount.codex_home}`)).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: '切换到此账号' }));
      await waitFor(() => {
        expect(api.setCodexPoolPreferred).toHaveBeenCalledWith('codex-2');
      });
    });

    it('marks the preferred account and can restore automatic rotation', async () => {
      enableCodexPool({
        enabled: true,
        total: 1,
        available: 1,
        cooldown: 0,
        disabled: 0,
        preferred: 'codex-2',
        accounts: [codexAccount],
      });
      vi.mocked(api.setCodexPoolPreferred).mockResolvedValue({ ok: true, preferred: null });
      const user = userEvent.setup();

      await openCodexTab(user);

      expect(screen.getByText('当前指定')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: '恢复自动' }));
      await waitFor(() => {
        expect(api.setCodexPoolPreferred).toHaveBeenCalledWith(null);
      });
    });

    it('retries a transient relogin status failure and still accepts the OTP', async () => {
      enableCodexPool({
        enabled: true,
        total: 1,
        available: 1,
        cooldown: 0,
        disabled: 0,
        preferred: null,
        accounts: [codexAccount],
      });
      vi.mocked(api.codexPoolRelogin).mockResolvedValue({
        ok: true,
        status: 'running',
        attempt_id: 'relogin-attempt',
      });
      vi.mocked(api.codexPoolReloginStatus)
        .mockRejectedValueOnce(new Error('temporary network error'))
        .mockResolvedValue({
          status: 'awaiting_otp',
          attempt_id: 'relogin-attempt',
          challenge_id: 'relogin-challenge',
          expires_at: Math.floor(Date.now() / 1000) + 600,
        });
      vi.mocked(api.codexPoolSubmitOtp).mockResolvedValue({
        ok: true,
        status: 'verifying_otp',
      });
      const user = userEvent.setup();

      await openCodexTab(user);
      await user.click(screen.getByRole('button', { name: '重新登录' }));

      await waitFor(() => {
        expect(screen.getByText(/状态查询暂时失败，正在重试/)).toBeInTheDocument();
      }, { timeout: 2500 });
      const otpInput = await screen.findByLabelText(
        'OpenAI 邮箱验证码',
        {},
        { timeout: 5000 },
      );
      expect(api.codexPoolReloginStatus).toHaveBeenCalledTimes(2);

      await user.type(otpInput, '123456');
      await user.click(screen.getByRole('button', { name: '继续登录' }));
      await waitFor(() => {
        expect(api.codexPoolSubmitOtp).toHaveBeenCalledWith(
          'relogin-attempt',
          'relogin-challenge',
          '123456',
        );
      });
    });
  });
});
