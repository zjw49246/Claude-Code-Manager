import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import App from './App';

vi.mock('./api/client', () => ({
  getToken: vi.fn(() => 'member-token'),
}));
vi.mock('./config/server', () => ({
  isCapacitor: vi.fn(() => false),
  getServerUrl: vi.fn(() => ''),
  getApiBase: vi.fn(() => ''),
}));
vi.mock('./components/Layout/AppShell', () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => (
    <div
      data-testid="app-shell"
      data-role={JSON.parse(localStorage.getItem('cc_user') || '{}').role || ''}
    >
      {children}
    </div>
  ),
}));
vi.mock('./pages/TasksPage', () => ({
  TasksPage: () => <div>Tasks screen</div>,
}));
vi.mock('./pages/LoginPage', () => ({
  LoginPage: () => <div>Login screen</div>,
}));

describe('App authentication probe', () => {
  beforeEach(() => {
    window.location.hash = '#/tasks';
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('uses auth/me instead of the Instance admin API', async () => {
    localStorage.setItem('cc_user', JSON.stringify({
      id: 4,
      name: 'Stale Admin',
      role: 'admin',
    }));
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({
        ok: true,
        json: vi.fn().mockResolvedValue({
          ok: true,
          user: { id: 4, name: 'Member', role: 'member' },
        }),
      });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    expect(await screen.findByText('Tasks screen')).toBeInTheDocument();
    expect(screen.getByTestId('app-shell')).toHaveAttribute(
      'data-role',
      'member',
    );
    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls).toEqual(['/api/system/health', '/api/auth/me']);
    expect(urls.some((url) => url.includes('/api/instances'))).toBe(false);
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('cc_user') || '{}'))
        .toMatchObject({ name: 'Member' });
    });
  });

  it('shows login when auth/me rejects the current credentials', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: false, status: 401 });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    expect(await screen.findByText('Login screen')).toBeInTheDocument();
  });

  it('replaces a stale cached identity in no-auth mode', async () => {
    localStorage.setItem('cc_user', JSON.stringify({
      name: 'Old member',
      role: 'member',
    }));
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({
        ok: true,
        json: vi.fn().mockResolvedValue({
          ok: true,
          auth_type: 'none',
          role: 'super_admin',
        }),
      });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    expect(await screen.findByText('Tasks screen')).toBeInTheDocument();
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('cc_user') || '{}')).toEqual({
        name: 'Local Admin',
        role: 'super_admin',
      });
    });
  });
});
