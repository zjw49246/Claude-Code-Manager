import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';

describe('Worker provider API routing', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      headers: { get: () => null },
      json: async () => ({ ok: true, status: 'idle', accounts: [] }),
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('passes provider through Worker pool status, usage, and delete requests', async () => {
    await api.workerAddStatus(7, 'user+one@example.com', 'claude');
    await api.getWorkerPoolUsage(7, 'claude');
    await api.getWorkerPool(7, 'codex');
    await api.deleteWorkerAccount(7, 'account/1', 'claude');

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/workers/7/pool/add/user%2Bone%40example.com?provider=claude',
      '/api/workers/7/pool/usage?provider=claude',
      '/api/workers/7/pool?provider=codex',
      '/api/workers/7/pool/account%2F1?provider=claude',
    ]);
    expect(fetchMock.mock.calls[3][1]).toMatchObject({ method: 'DELETE' });
  });

  it('defaults legacy callers to the Codex Worker pool', async () => {
    await api.workerAddStatus(3, 'codex@example.com');
    await api.getWorkerPoolUsage(3);
    await api.deleteWorkerAccount(3, 'account-1');

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/workers/3/pool/add/codex%40example.com?provider=codex',
      '/api/workers/3/pool/usage?provider=codex',
      '/api/workers/3/pool/account-1?provider=codex',
    ]);
  });

  it('routes Worker OTP submission and cancellation through the encoded login attempt URL', async () => {
    await api.submitWorkerLoginOtp(
      9,
      'attempt/one + two',
      'challenge-1',
      '012345',
    );
    await api.cancelWorkerLogin(9, 'attempt/one + two');

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/workers/9/pool/login-attempts/attempt%2Fone%20%2B%20two/otp',
      '/api/workers/9/pool/login-attempts/attempt%2Fone%20%2B%20two',
    ]);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ challenge_id: 'challenge-1', code: '012345' }),
    });
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'DELETE' });
  });
});
