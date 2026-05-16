const STORAGE_KEY = 'cc_timezone';

/** Common timezones grouped by region */
export const TIMEZONE_OPTIONS: { label: string; value: string }[] = [
  { label: 'Auto (Browser)', value: 'auto' },
  { label: 'UTC', value: 'UTC' },
  // Americas
  { label: 'US Pacific (Los Angeles)', value: 'America/Los_Angeles' },
  { label: 'US Mountain (Denver)', value: 'America/Denver' },
  { label: 'US Central (Chicago)', value: 'America/Chicago' },
  { label: 'US Eastern (New York)', value: 'America/New_York' },
  { label: 'São Paulo', value: 'America/Sao_Paulo' },
  // Europe
  { label: 'London', value: 'Europe/London' },
  { label: 'Paris / Berlin', value: 'Europe/Paris' },
  { label: 'Moscow', value: 'Europe/Moscow' },
  // Asia
  { label: 'Dubai', value: 'Asia/Dubai' },
  { label: 'Kolkata', value: 'Asia/Kolkata' },
  { label: 'Bangkok', value: 'Asia/Bangkok' },
  { label: 'Singapore', value: 'Asia/Singapore' },
  { label: 'Shanghai', value: 'Asia/Shanghai' },
  { label: 'Seoul', value: 'Asia/Seoul' },
  { label: 'Tokyo', value: 'Asia/Tokyo' },
  // Oceania
  { label: 'Sydney', value: 'Australia/Sydney' },
  { label: 'Auckland', value: 'Pacific/Auckland' },
];

export function getTimezone(): string {
  return localStorage.getItem(STORAGE_KEY) || 'auto';
}

export function setTimezone(tz: string) {
  localStorage.setItem(STORAGE_KEY, tz);
}

/** Resolve the effective IANA timezone string */
export function resolveTimezone(): string {
  const tz = getTimezone();
  if (tz === 'auto') {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  }
  return tz;
}

function getDateParts(date: Date, tz: string): { year: number; month: number; day: number } {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
  }).formatToParts(date);
  return {
    year: Number(parts.find(p => p.type === 'year')!.value),
    month: Number(parts.find(p => p.type === 'month')!.value),
    day: Number(parts.find(p => p.type === 'day')!.value),
  };
}

/** Format an ISO timestamp string for display in chat.
 *  Today → HH:MM, same year → MM/DD HH:MM, different year → YYYY/MM/DD HH:MM */
export function formatMessageTime(isoString: string, now?: Date): string {
  const tz = resolveTimezone();
  const date = new Date(isoString);
  const msgParts = getDateParts(date, tz);
  const nowParts = getDateParts(now ?? new Date(), tz);

  const time = date.toLocaleTimeString(undefined, {
    timeZone: tz,
    hour: '2-digit',
    minute: '2-digit',
  });

  const isToday = msgParts.year === nowParts.year
    && msgParts.month === nowParts.month
    && msgParts.day === nowParts.day;

  if (isToday) return time;

  const mm = String(msgParts.month).padStart(2, '0');
  const dd = String(msgParts.day).padStart(2, '0');

  if (msgParts.year !== nowParts.year) {
    return `${msgParts.year}/${mm}/${dd} ${time}`;
  }
  return `${mm}/${dd} ${time}`;
}
