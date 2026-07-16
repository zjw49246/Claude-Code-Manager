import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { THEME_OPTIONS, getTheme, setTheme, applyTheme } from './theme';

// vitest root = frontend/（jsdom 下 import.meta.url 非 file 协议，用 cwd 定位）
const indexCss = readFileSync(join(process.cwd(), 'src/index.css'), 'utf-8');

/** 提取 index.css 中某主题的变量块（主题块内无嵌套大括号） */
function themeBlock(theme: string): string {
  const marker = `html[data-theme='${theme}'] {`;
  const start = indexCss.indexOf(marker);
  expect(start, `index.css 应包含 ${marker} 主题块`).toBeGreaterThan(-1);
  const end = indexCss.indexOf('\n}', start);
  return indexCss.slice(start, end);
}

/** CLAUDE.md 约定：新增主题必须同时覆盖 gray 全档 + indigo 全档 */
const REQUIRED_GRAY = [950, 900, 800, 750, 700, 600, 500, 400, 300, 200, 100, 50];
const REQUIRED_INDIGO = [950, 900, 800, 700, 600, 500, 400, 300];

describe('theme config', () => {
  beforeEach(() => {
    localStorage.clear();
    document.querySelector('meta[name="theme-color"]')?.remove();
    const meta = document.createElement('meta');
    meta.setAttribute('name', 'theme-color');
    meta.setAttribute('content', '#131316');
    document.head.appendChild(meta);
  });

  it('注册了飞书主题（modern 组浅色）', () => {
    const feishu = THEME_OPTIONS.find((o) => o.value === 'feishu');
    expect(feishu).toBeDefined();
    expect(feishu!.group).toBe('modern');
    expect(feishu!.scheme).toBe('light');
    expect(feishu!.themeColor).toBe('#ecedef');
  });

  it('主题 value 无重复', () => {
    const values = THEME_OPTIONS.map((o) => o.value);
    expect(new Set(values).size).toBe(values.length);
  });

  it('setTheme 持久化并应用 data-theme 与 theme-color', () => {
    setTheme('feishu');
    expect(getTheme()).toBe('feishu');
    expect(document.documentElement.dataset.theme).toBe('feishu');
    expect(
      document.querySelector('meta[name="theme-color"]')!.getAttribute('content'),
    ).toBe('#ecedef');
  });

  it('getTheme 对无效存储值回退到 dark', () => {
    localStorage.setItem('cc_theme', 'no-such-theme');
    expect(getTheme()).toBe('dark');
    applyTheme();
    expect(document.documentElement.dataset.theme).toBe('dark');
  });
});

describe('index.css 主题变量覆盖完整性', () => {
  // 现代组非默认主题（默认 dark 定义在 @theme 里）都必须覆盖全档
  const modernOverrideThemes = THEME_OPTIONS.filter(
    (o) => o.group === 'modern' && o.value !== 'dark',
  ).map((o) => o.value);

  it.each(modernOverrideThemes)('%s 覆盖 gray + indigo 全档', (theme) => {
    const block = themeBlock(theme);
    for (const shade of REQUIRED_GRAY) {
      expect(block, `${theme} 缺 --color-gray-${shade}`).toContain(`--color-gray-${shade}:`);
    }
    for (const shade of REQUIRED_INDIGO) {
      expect(block, `${theme} 缺 --color-indigo-${shade}`).toContain(`--color-indigo-${shade}:`);
    }
    expect(block).toContain('--color-foreground:');
    expect(block).toContain('--ring:');
  });

  it('浅色主题声明 color-scheme: light 并深色化 accent 300/400 档', () => {
    for (const opt of THEME_OPTIONS.filter((o) => o.group === 'modern' && o.scheme === 'light')) {
      const block = themeBlock(opt.value);
      expect(block, `${opt.value} 缺 color-scheme`).toContain('color-scheme: light');
      // 浅色兼容规则：chip 文字用的 accent 300/400 必须反转为深色调
      for (const accent of ['red', 'orange', 'green', 'yellow', 'blue', 'purple']) {
        expect(block, `${opt.value} 缺 --color-${accent}-400`).toContain(`--color-${accent}-400:`);
        expect(block, `${opt.value} 缺 --color-${accent}-300`).toContain(`--color-${accent}-300:`);
      }
    }
  });

  it('飞书主题使用官方品牌蓝与中性色 token（截图实证）', () => {
    const block = themeBlock('feishu');
    expect(block).toContain('--color-indigo-600: #3370ff'); // 经典飞书蓝 B500（App 截图取色 #316efa 实证）
    expect(block).toContain('--color-indigo-500: #245bdb'); // B600 hover 向深走
    expect(block).toContain('--color-gray-100: #1f2329'); // N900 主文字
    expect(block).toContain('--color-gray-800: #ffffff'); // 卡片纯白
    expect(block).toContain('--color-gray-950: #ecedef'); // 侧栏壳 = 飞书 rail 灰（截图取色）
    expect(block).toContain('--color-gray-900: #fbfbfc'); // 画布近白：白底为主，区别于「现代浅色」的灰画布
  });

  it('飞书主题与现代浅色不趋同（白底为主 vs 灰画布）', () => {
    // 回归守卫：feishu 的画布(gray-900)必须显著白于 light 的画布，
    // 否则两个主题肉眼无法区分（2026-07-16 用户反馈）
    const feishu = themeBlock('feishu');
    const light = themeBlock('light');
    expect(feishu).toContain('--color-gray-900: #fbfbfc');
    // light 画布保持色调分层灰（tonal zinc），确保没人把两边改成同一取值
    expect(light).toContain('--color-gray-900: oklch(95.8% 0.002 286)');
    expect(light).toContain('--color-gray-950: oklch(92.5% 0.003 286)');
  });
});
