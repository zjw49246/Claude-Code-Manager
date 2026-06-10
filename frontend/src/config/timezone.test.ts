import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { formatMessageTime, formatDateTime, resolveTimezone, getTimezone, setTimezone } from './timezone';

const mockStorage: Record<string, string> = {};
beforeEach(() => {
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => mockStorage[k] ?? null,
    setItem: (k: string, v: string) => { mockStorage[k] = v; },
    removeItem: (k: string) => { delete mockStorage[k]; },
  });
});
afterEach(() => {
  Object.keys(mockStorage).forEach(k => delete mockStorage[k]);
  vi.restoreAllMocks();
});

// Time portion is locale-dependent (24h or 12h with AM/PM), so we match flexibly.
const TIME_RE = /\d{1,2}:\d{2}(\s*[AP]M)?/i;

describe('getTimezone / setTimezone', () => {
  it('defaults to "auto"', () => {
    expect(getTimezone()).toBe('auto');
  });

  it('round-trips a value', () => {
    setTimezone('Asia/Shanghai');
    expect(getTimezone()).toBe('Asia/Shanghai');
  });
});

describe('resolveTimezone', () => {
  it('returns browser timezone for "auto"', () => {
    const tz = resolveTimezone();
    expect(typeof tz).toBe('string');
    expect(tz.length).toBeGreaterThan(0);
  });

  it('returns explicit timezone when set', () => {
    setTimezone('Europe/London');
    expect(resolveTimezone()).toBe('Europe/London');
  });
});

describe('formatMessageTime', () => {
  beforeEach(() => {
    setTimezone('Asia/Shanghai');
  });

  it('shows only time for today', () => {
    const now = new Date('2026-05-16T10:00:00+08:00');
    const msg = '2026-05-16T08:30:00+08:00';
    const result = formatMessageTime(msg, now);
    // Should be time only, no date prefix
    expect(result).toMatch(TIME_RE);
    expect(result).not.toMatch(/\//);
  });

  it('shows MM/DD + time for a different day in the same year', () => {
    const now = new Date('2026-05-16T10:00:00+08:00');
    const msg = '2026-05-15T22:45:00+08:00';
    const result = formatMessageTime(msg, now);
    expect(result).toMatch(/^05\/15\s+/);
    expect(result).toMatch(TIME_RE);
  });

  it('shows YYYY/MM/DD + time for a different year', () => {
    const now = new Date('2026-05-16T10:00:00+08:00');
    const msg = '2025-12-31T23:59:00+08:00';
    const result = formatMessageTime(msg, now);
    expect(result).toMatch(/^2025\/12\/31\s+/);
    expect(result).toMatch(TIME_RE);
  });

  it('handles timezone boundary — message is yesterday in user tz but same UTC date', () => {
    setTimezone('Asia/Shanghai');
    // May 16 01:00 UTC = May 16 09:00 Shanghai (today)
    // May 15 15:00 UTC = May 15 23:00 Shanghai (yesterday)
    const now = new Date('2026-05-16T01:00:00Z');
    const msg = '2026-05-15T15:00:00Z';
    const result = formatMessageTime(msg, now);
    expect(result).toMatch(/^05\/15\s+/);
    expect(result).toMatch(TIME_RE);
  });

  it('handles timezone boundary — message appears today in user tz despite different UTC date', () => {
    setTimezone('America/New_York');
    // May 16 05:00 UTC = May 16 01:00 ET (today in ET)
    // May 16 04:30 UTC = May 16 00:30 ET (same day)
    const now = new Date('2026-05-16T05:00:00Z');
    const msg = '2026-05-16T04:30:00Z';
    const result = formatMessageTime(msg, now);
    // Both are May 16 in ET → today → only time, no slash
    expect(result).toMatch(TIME_RE);
    expect(result).not.toMatch(/\//);
  });

  it('pads single-digit month and day', () => {
    const now = new Date('2026-03-15T12:00:00+08:00');
    const msg = '2026-01-05T09:07:00+08:00';
    const result = formatMessageTime(msg, now);
    expect(result).toMatch(/^01\/05\s+/);
  });

  it('works with UTC timezone', () => {
    setTimezone('UTC');
    const now = new Date('2026-05-16T12:00:00Z');
    const msg = '2026-05-16T09:05:00Z';
    const result = formatMessageTime(msg, now);
    // Same day in UTC → only time
    expect(result).toMatch(TIME_RE);
    expect(result).not.toMatch(/\//);
  });

  it('Jan 1 message shown with year prefix when now is next year', () => {
    const now = new Date('2027-01-02T10:00:00+08:00');
    const msg = '2026-01-01T08:00:00+08:00';
    const result = formatMessageTime(msg, now);
    expect(result).toMatch(/^2026\/01\/01\s+/);
    expect(result).toMatch(TIME_RE);
  });

  it('yesterday at midnight boundary shows date', () => {
    const now = new Date('2026-05-16T00:01:00+08:00');
    const msg = '2026-05-15T23:59:00+08:00';
    const result = formatMessageTime(msg, now);
    expect(result).toMatch(/^05\/15\s+/);
  });

  it('treats naive timestamp (no Z) as UTC — same as with Z suffix', () => {
    setTimezone('Asia/Shanghai');
    const now = new Date('2026-05-16T12:00:00Z');
    const naiveResult = formatMessageTime('2026-05-16T04:00:00', now);
    const utcResult = formatMessageTime('2026-05-16T04:00:00Z', now);
    expect(naiveResult).toBe(utcResult);
  });

  it('naive timestamp converts correctly to non-UTC timezone', () => {
    setTimezone('Asia/Shanghai');
    // 2026-05-16T02:00:00 UTC = 2026-05-16T10:00:00 Shanghai
    const now = new Date('2026-05-16T12:00:00Z');
    const result = formatMessageTime('2026-05-16T02:00:00', now);
    // Should show 10:00 in Shanghai time (today → time only)
    expect(result).toMatch(TIME_RE);
    expect(result).not.toMatch(/\//);
  });

  it('naive timestamp with microseconds is handled', () => {
    setTimezone('UTC');
    const now = new Date('2026-05-16T12:00:00Z');
    const result = formatMessageTime('2026-05-16T09:05:00.123456', now);
    expect(result).toMatch(TIME_RE);
    expect(result).not.toMatch(/\//);
  });

  it('timestamp with positive offset is preserved (not double-converted)', () => {
    setTimezone('Asia/Shanghai');
    const now = new Date('2026-05-16T12:00:00+08:00');
    const result = formatMessageTime('2026-05-16T10:00:00+08:00', now);
    expect(result).toMatch(TIME_RE);
    expect(result).not.toMatch(/\//);
  });

  it('timestamp with negative offset is preserved', () => {
    setTimezone('America/New_York');
    const now = new Date('2026-05-16T12:00:00-04:00');
    const result = formatMessageTime('2026-05-16T10:00:00-04:00', now);
    expect(result).toMatch(TIME_RE);
    expect(result).not.toMatch(/\//);
  });
});

describe('formatDateTime', () => {
  beforeEach(() => {
    setTimezone('Asia/Shanghai');
  });

  it('always includes date even for today', () => {
    const now = new Date('2026-05-16T10:00:00+08:00');
    const msg = '2026-05-16T08:30:00+08:00';
    const result = formatDateTime(msg, now);
    expect(result).toMatch(/^05\/16\s+/);
    expect(result).toMatch(TIME_RE);
  });

  it('shows YYYY prefix for different year', () => {
    const now = new Date('2026-05-16T10:00:00+08:00');
    const msg = '2025-12-31T23:59:00+08:00';
    const result = formatDateTime(msg, now);
    expect(result).toMatch(/^2025\/12\/31\s+/);
    expect(result).toMatch(TIME_RE);
  });

  it('treats naive timestamp as UTC', () => {
    setTimezone('Asia/Shanghai');
    const now = new Date('2026-05-16T12:00:00Z');
    const naiveResult = formatDateTime('2026-05-16T04:00:00', now);
    const utcResult = formatDateTime('2026-05-16T04:00:00Z', now);
    expect(naiveResult).toBe(utcResult);
  });

  it('converts UTC timestamp to user timezone for display', () => {
    setTimezone('Asia/Shanghai');
    // 2026-05-15T20:00:00 UTC = 2026-05-16T04:00:00 Shanghai
    const now = new Date('2026-05-16T12:00:00Z');
    const result = formatDateTime('2026-05-15T20:00:00Z', now);
    // In Shanghai timezone, this is May 16 → same year → MM/DD format
    expect(result).toMatch(/^05\/16\s+/);
  });
});
