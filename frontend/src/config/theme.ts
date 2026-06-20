const STORAGE_KEY = 'cc_theme';

export const THEME_OPTIONS = [
  { value: 'dark', label: '深色' },
  { value: 'light', label: '浅色' },
  { value: 'ocean', label: '海蓝' },
  { value: 'forest', label: '森林' },
  { value: 'rose', label: '莓红' },
] as const;

export type Theme = typeof THEME_OPTIONS[number]['value'];

const THEME_VALUES = new Set<Theme>(THEME_OPTIONS.map((t) => t.value));

export function getTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY) as Theme | null;
  return stored && THEME_VALUES.has(stored) ? stored : 'dark';
}

export function setTheme(theme: Theme) {
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

export function applyTheme(theme?: Theme) {
  const t = theme || getTheme();
  document.documentElement.classList.toggle('light', t === 'light');
  document.documentElement.dataset.theme = t;
}
