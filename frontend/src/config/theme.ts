const STORAGE_KEY = 'cc_theme';

export interface ThemeOption {
  value: string;
  label: string;
  /** modern = v2 设计（默认深/浅）；legacy = v1 保留主题 */
  group: 'modern' | 'legacy';
  scheme: 'dark' | 'light';
  /** 移动端状态栏 / PWA theme-color（≈ 各主题的壳背景色） */
  themeColor: string;
}

export const THEME_OPTIONS = [
  { value: 'dark', label: '深色', group: 'modern', scheme: 'dark', themeColor: '#131316' },
  { value: 'light', label: '浅色', group: 'modern', scheme: 'light', themeColor: '#f0f0f1' },
  { value: 'feishu', label: '飞书', group: 'modern', scheme: 'light', themeColor: '#eceef1' },
  { value: 'legacy', label: '经典深色', group: 'legacy', scheme: 'dark', themeColor: '#030712' },
  { value: 'ocean', label: '海蓝', group: 'legacy', scheme: 'dark', themeColor: '#06131f' },
  { value: 'forest', label: '森林', group: 'legacy', scheme: 'dark', themeColor: '#07130d' },
  { value: 'rose', label: '莓红', group: 'legacy', scheme: 'dark', themeColor: '#1a0b12' },
] as const satisfies readonly ThemeOption[];

export type Theme = typeof THEME_OPTIONS[number]['value'];

const THEME_MAP = new Map(THEME_OPTIONS.map((t) => [t.value, t]));

export function getTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored && THEME_MAP.has(stored as Theme) ? (stored as Theme) : 'dark';
}

export function setTheme(theme: Theme) {
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

export function applyTheme(theme?: Theme) {
  const t = theme || getTheme();
  const opt = THEME_MAP.get(t) ?? THEME_OPTIONS[0];
  document.documentElement.classList.remove('light');
  document.documentElement.dataset.theme = t;
  // 同步移动端状态栏 / PWA 顶栏颜色
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', opt.themeColor);
}
