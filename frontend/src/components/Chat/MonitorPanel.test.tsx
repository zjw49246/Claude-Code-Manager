import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MonitorPanel } from './MonitorPanel';

vi.mock('../../api/client', () => ({
  api: {
    listMonitorSessions: vi.fn(() => Promise.resolve([])),
    getMonitorChecks: vi.fn(() => Promise.resolve([])),
    stopMonitorSession: vi.fn(),
  },
}));

const baseProps = {
  taskId: 1,
  sessions: [],
  onSessionsChange: vi.fn(),
  onClose: vi.fn(),
};

describe('MonitorPanel codex annotation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows a claude-only notice for codex tasks', () => {
    render(<MonitorPanel {...baseProps} provider="codex" />);
    expect(
      screen.getByText('Monitor / Sub-Agent 暂不支持 Codex 任务（仅 Claude 可用）'),
    ).toBeInTheDocument();
  });

  it('shows no notice for claude tasks', () => {
    render(<MonitorPanel {...baseProps} provider="claude" />);
    expect(
      screen.queryByText('Monitor / Sub-Agent 暂不支持 Codex 任务（仅 Claude 可用）'),
    ).not.toBeInTheDocument();
  });

  it('shows no notice when provider is omitted', () => {
    render(<MonitorPanel {...baseProps} />);
    expect(
      screen.queryByText(/暂不支持 Codex/),
    ).not.toBeInTheDocument();
  });
});
