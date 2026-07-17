import { applyCustomTheme, clearCustomTheme } from './customTheme';
import { applyBgImage } from './customBg';

const STORAGE_KEY = 'cc_theme';

export interface ThemeOption {
  value: string;
  label: string;
  /** modern = v2 设计（默认深/浅）；legacy = v1 保留主题；custom = 用户自定义配色 */
  group: 'modern' | 'legacy' | 'custom';
  /** custom 的实际 scheme 由背景色亮度运行时判定，此处为名义值 */
  scheme: 'dark' | 'light';
  /** 移动端状态栏 / PWA theme-color（≈ 各主题的壳背景色；custom 运行时取用户所选背景） */
  themeColor: string;
}

export const THEME_OPTIONS = [
  { value: 'dark', label: '深色', group: 'modern', scheme: 'dark', themeColor: '#131316' },
  { value: 'light', label: '浅色', group: 'modern', scheme: 'light', themeColor: '#e9e9ec' },
  { value: 'feishu', label: '飞书', group: 'modern', scheme: 'light', themeColor: '#ecedef' },
  { value: 'apple', label: '苹果', group: 'modern', scheme: 'light', themeColor: '#f9f9f9' },
  { value: 'legacy', label: '经典深色', group: 'legacy', scheme: 'dark', themeColor: '#030712' },
  { value: 'ocean', label: '海蓝', group: 'legacy', scheme: 'dark', themeColor: '#06131f' },
  { value: 'forest', label: '森林', group: 'legacy', scheme: 'dark', themeColor: '#07130d' },
  { value: 'rose', label: '莓红', group: 'legacy', scheme: 'dark', themeColor: '#1a0b12' },
  { value: 'custom', label: '自定义', group: 'custom', scheme: 'dark', themeColor: '#131316' },
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
  // custom 的色阶是运行时算出来的内联变量；切走时必须清场，否则会盖住新主题
  // 注意类型：THEME_OPTIONS 是 as const，opt.themeColor 是字面量联合；
  // custom 的取色是运行时算的普通 string，故这里必须显式放宽
  let themeColor: string = opt.themeColor;
  if (t === 'custom') {
    themeColor = applyCustomTheme();
    void applyBgImage();  // 图片字节要读 IDB，异步铺；色阶已同步就位
  } else {
    clearCustomTheme();
  }
  // 同步移动端状态栏 / PWA 顶栏颜色
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', themeColor);
}
