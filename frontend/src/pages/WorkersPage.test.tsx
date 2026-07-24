import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WorkersPage from './WorkersPage';
import type { Worker, WorkerPoolStatus } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    listWorkers: vi.fn(),
    getTeamUsers: vi.fn(),
    createWorker: vi.fn(),
    getWorkerLogs: vi.fn(),
    getWorkerRuntimeSettings: vi.fn(),
    updateWorkerRuntimeSettings: vi.fn(),
    getWorkerPoolUsage: vi.fn(),
    addWorkerAccount: vi.fn(),
    workerAddStatus: vi.fn(),
    submitWorkerLoginOtp: vi.fn(),
    cancelWorkerLogin: vi.fn(),
    deleteWorkerAccount: vi.fn(),
    renameWorker: vi.fn(),
    assignWorker: vi.fn(),
    retryWorker: vi.fn(),
    stopWorker: vi.fn(),
    startWorker: vi.fn(),
    destroyWorker: vi.fn(),
  },
}));

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(),
}));

import { api } from '../api/client';

const baseWorker: Worker = {
  id: 1,
  name: 'worker-one',
  status: 'ready',
  owner_user_id: null,
  cloud_instance_id: 'i-test',
  private_ip: '10.0.0.1',
  public_ip: null,
  ssh_user: 'ubuntu',
  ssh_key_path: null,
  ccm_port: 8000,
  ccm_commit: 'abcdef123456',
  accounts: [],
  last_heartbeat: null,
  bootstrap_step: null,
  bootstrap_error: null,
  created_at: '2026-07-22T00:00:00',
  updated_at: '2026-07-22T00:00:00',
};

const codexPool: WorkerPoolStatus = {
  enabled: true,
  provider: 'codex',
  total: 1,
  available: 1,
  accounts: [{
    id: 'codex-a',
    email: 'codex@example.com',
    enabled: true,
    available: true,
    plan_type: 'pro',
    quota: {
      primary_used_percent: 33,
      primary_window_minutes: 300,
      primary_resets_at: 1_785_000_000,
      secondary_used_percent: 44,
      secondary_window_minutes: 10080,
      secondary_resets_at: 1_785_100_000,
      is_rate_limited: false,
      has_credits: true,
    },
  }],
};

const claudePool: WorkerPoolStatus = {
  enabled: true,
  provider: 'claude',
  total: 1,
  available: 1,
  accounts: [{
    id: 'claude-a',
    email: 'claude@example.com',
    enabled: true,
    available: true,
    subscription_type: 'pro',
    usage: {
      five_hour: { utilization: 12, resets_at: '2026-07-22T12:00:00Z' },
      seven_day: { utilization: 24, resets_at: '2026-07-29T00:00:00Z' },
    },
  }],
};

async function openAddWorker(user: ReturnType<typeof userEvent.setup>) {
  render(<WorkersPage />);
  await user.click(await screen.findByRole('button', { name: /Add Worker/ }));
}

describe('WorkersPage provider-aware accounts', () => {
  beforeEach(() => {
    localStorage.setItem('cc_user', JSON.stringify({ id: 1, role: 'admin' }));
    vi.mocked(api.listWorkers).mockResolvedValue([]);
    vi.mocked(api.getTeamUsers).mockResolvedValue([]);
    vi.mocked(api.createWorker).mockResolvedValue(baseWorker);
    vi.mocked(api.getWorkerRuntimeSettings).mockResolvedValue({ use_pty_mode: false } as never);
    vi.mocked(api.addWorkerAccount).mockResolvedValue({ ok: true, status: 'running' });
    vi.mocked(api.workerAddStatus).mockResolvedValue({ status: 'running' });
    vi.mocked(api.submitWorkerLoginOtp).mockResolvedValue({ ok: true, status: 'verifying_otp' });
    vi.mocked(api.cancelWorkerLogin).mockResolvedValue({ ok: true, status: 'cancelled' });
    vi.mocked(api.deleteWorkerAccount).mockResolvedValue({ ok: true });
    vi.spyOn(window, 'alert').mockImplementation(() => {});
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it('creates Codex accounts by default and requires an email token for unattended login', async () => {
    const user = userEvent.setup();
    await openAddWorker(user);

    expect(screen.getByLabelText('账号 1 Provider')).toHaveValue('codex');
    await user.type(screen.getByPlaceholderText('如 worker-prod-1'), 'worker-codex');
    await user.type(screen.getByPlaceholderText('账号 1 Email'), 'codex@example.com');
    await user.type(screen.getByPlaceholderText('OpenAI 密码（可选）'), 'openai-secret');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    expect(await screen.findByText(/Codex 账号 1 的 Worker 自动登录需要邮箱 Token/)).toBeInTheDocument();
    expect(api.createWorker).not.toHaveBeenCalled();

    await user.type(screen.getByPlaceholderText('邮箱接码 Token *'), 'mail-token');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(api.createWorker).toHaveBeenCalledWith({
        name: 'worker-codex',
        accounts: [{
          email: 'codex@example.com',
          provider: 'codex',
          token: 'mail-token',
          password: 'openai-secret',
        }],
      });
    });
  });

  it('keeps Claude account creation compatible and requires its token', async () => {
    const user = userEvent.setup();
    await openAddWorker(user);

    await user.selectOptions(screen.getByLabelText('账号 1 Provider'), 'claude');
    expect(screen.queryByPlaceholderText('OpenAI 密码（可选）')).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText('如 worker-prod-1'), 'worker-claude');
    await user.type(screen.getByPlaceholderText('账号 1 Email'), 'claude@example.com');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    expect(await screen.findByText(/Claude 账号 1 的接码 Token 必填/)).toBeInTheDocument();
    expect(api.createWorker).not.toHaveBeenCalled();

    await user.type(screen.getByPlaceholderText('接码 Token *'), 'mail-token');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(api.createWorker).toHaveBeenCalledWith({
        name: 'worker-claude',
        accounts: [{
          email: 'claude@example.com',
          provider: 'claude',
          token: 'mail-token',
        }],
      });
    });
  });

  it('interprets legacy provider-less Worker accounts as Claude', async () => {
    vi.mocked(api.listWorkers).mockResolvedValue([{
      ...baseWorker,
      accounts: [{ email: 'old@example.com', status: 'ready' }],
    }]);
    vi.mocked(api.getWorkerPoolUsage).mockResolvedValue(claudePool);
    const user = userEvent.setup();

    render(<WorkersPage />);
    await user.click(await screen.findByTitle('Worker 号池额度'));

    await waitFor(() => expect(api.getWorkerPoolUsage).toHaveBeenCalledWith(1, 'claude'));
  });

  it('loads provider-specific pools and renders Codex quota plus legacy Claude usage', async () => {
    const worker = {
      ...baseWorker,
      accounts: [
        { email: 'legacy@example.com', status: 'ready' },
        { email: 'codex@example.com', provider: 'codex' as const, status: 'ready' },
      ],
    };
    vi.mocked(api.listWorkers).mockResolvedValue([worker]);
    vi.mocked(api.getWorkerPoolUsage).mockImplementation(async (_workerId, provider) => (
      provider === 'claude' ? claudePool : codexPool
    ));
    vi.mocked(api.workerAddStatus).mockResolvedValue({ status: 'success' });
    const user = userEvent.setup();

    render(<WorkersPage />);
    await user.click(await screen.findByTitle('Worker 号池额度'));

    await waitFor(() => expect(api.getWorkerPoolUsage).toHaveBeenCalledWith(1, 'codex'));
    expect(await screen.findByText('33%')).toBeInTheDocument();
    expect(screen.getByText('44%')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /添加 Codex 账号/ }));
    await user.type(screen.getByLabelText('Worker 号池账号 Email'), 'new-codex@example.com');
    await user.type(screen.getByLabelText('Worker 号池 OpenAI 密码'), 'openai-password');
    expect(screen.getByLabelText('Worker 号池 OpenAI 密码')).toHaveAttribute('type', 'password');
    await user.click(screen.getByRole('button', { name: '开始登录' }));
    expect(await screen.findByText('Worker 自动登录需要邮箱 Token')).toBeInTheDocument();
    expect(api.addWorkerAccount).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText('Worker 号池接码 Token'), 'mail-token');
    await user.click(screen.getByRole('button', { name: '开始登录' }));
    await waitFor(() => {
      expect(api.addWorkerAccount).toHaveBeenCalledWith(1, {
        email: 'new-codex@example.com',
        provider: 'codex',
        token: 'mail-token',
        password: 'openai-password',
      });
    });
    await waitFor(() => {
      expect(api.workerAddStatus).toHaveBeenCalledWith(1, 'new-codex@example.com', 'codex');
      expect(screen.queryByLabelText('Worker 号池账号 Email')).not.toBeInTheDocument();
    });

    await user.click(screen.getByRole('tab', { name: 'Claude' }));
    await waitFor(() => expect(api.getWorkerPoolUsage).toHaveBeenCalledWith(1, 'claude'));
    expect(await screen.findByText('12%')).toBeInTheDocument();
    expect(screen.getByText('24%')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '删除' }));
    await waitFor(() => expect(api.deleteWorkerAccount).toHaveBeenCalledWith(1, 'claude-a', 'claude'));
  });

  it('submits and cancels an OTP challenge while dynamically adding a Codex account', async () => {
    vi.mocked(api.listWorkers).mockResolvedValue([baseWorker]);
    vi.mocked(api.getWorkerPoolUsage).mockResolvedValue(codexPool);
    vi.mocked(api.workerAddStatus).mockResolvedValue({
      status: 'awaiting_otp',
      attempt_id: 'attempt/worker-1',
      challenge_id: 'challenge-1',
      expires_at: Math.floor(Date.now() / 1000) + 600,
    });
    const user = userEvent.setup();

    render(<WorkersPage />);
    await user.click(await screen.findByTitle('Worker 号池额度'));
    await user.click(await screen.findByRole('button', { name: /添加 Codex 账号/ }));
    await user.type(screen.getByLabelText('Worker 号池账号 Email'), 'otp@example.com');
    await user.type(screen.getByLabelText('Worker 号池接码 Token'), 'otp-mail-token');
    await user.click(screen.getByRole('button', { name: '开始登录' }));

    await waitFor(() => {
      expect(api.addWorkerAccount).toHaveBeenCalledWith(1, {
        email: 'otp@example.com',
        provider: 'codex',
        token: 'otp-mail-token',
      });
    });
    const otpInput = await screen.findByLabelText(
      'Worker OpenAI 邮箱验证码',
      {},
      { timeout: 3000 },
    );
    expect(api.workerAddStatus).toHaveBeenCalledWith(1, 'otp@example.com', 'codex');

    await user.type(otpInput, '654321');
    await user.click(screen.getByRole('button', { name: '提交验证码' }));
    await waitFor(() => {
      expect(api.submitWorkerLoginOtp).toHaveBeenCalledWith(
        1,
        'attempt/worker-1',
        'challenge-1',
        '654321',
      );
    });

    await user.click(screen.getByRole('button', { name: '取消登录' }));
    await waitFor(() => {
      expect(api.cancelWorkerLogin).toHaveBeenCalledWith(1, 'attempt/worker-1');
    });
    expect(screen.queryByLabelText('Worker OpenAI 邮箱验证码')).not.toBeInTheDocument();
  });

  it('does not offer destroy while a Worker lifecycle transition is busy', async () => {
    vi.mocked(api.listWorkers).mockResolvedValue(
      ['creating', 'bootstrapping', 'starting', 'stopping', 'destroying'].map((status, index) => ({
        ...baseWorker,
        id: index + 1,
        name: `busy-worker-${index + 1}`,
        status,
      })),
    );

    render(<WorkersPage />);

    expect(await screen.findByText('busy-worker-1')).toBeInTheDocument();
    expect(screen.queryByTitle('销毁（terminate EC2）')).not.toBeInTheDocument();
  });

  it('allows retry and cleanup when provisioning failed before an EC2 id exists', async () => {
    vi.mocked(api.listWorkers).mockResolvedValue([{
      ...baseWorker,
      status: 'error',
      cloud_instance_id: null,
      private_ip: null,
      bootstrap_step: 'provision-config',
      bootstrap_error: 'SSH key preflight failed',
    }]);

    render(<WorkersPage />);

    expect(await screen.findByText('worker-one')).toBeInTheDocument();
    expect(screen.getByTitle('重试 bootstrap')).toBeInTheDocument();
    expect(screen.getByTitle('销毁（terminate EC2）')).toBeInTheDocument();
  });
});
