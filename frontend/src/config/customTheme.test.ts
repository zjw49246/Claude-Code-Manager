import { describe, it, expect, beforeEach } from 'vitest';
import {
  hexToOklch, buildCustomTheme, getCustomColors, setCustomColors,
  applyCustomTheme, clearCustomTheme, getBgVisible, setBgVisible,
  CUSTOM_DEFAULT_BG, CUSTOM_DEFAULT_BRAND, CUSTOM_DEFAULT_BG_VISIBLE,
} from './customTheme';
import { setTheme } from './theme';

/** 解析 `oklch(L% C H)` 回结构，用于断言逐档取值（忽略可能的 / alpha 尾巴） */
function parseOklch(v: string) {
  const m = v.match(/oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)/);
  expect(m, `应为 oklch 三元组: ${v}`).toBeTruthy();
  return { l: Number(m![1]), c: Number(m![2]), h: Number(m![3]) };
}

/** 取 oklch 字符串的 alpha（无 / 尾巴则返回 1，即不透明） */
function alphaOf(v: string): number {
  const m = v.match(/\/\s*([\d.]+)\s*\)/);
  return m ? Number(m[1]) : 1;
}

const REQUIRED_GRAY = [950, 900, 800, 750, 700, 600, 500, 400, 300, 200, 100, 50];
const REQUIRED_INDIGO = [950, 900, 800, 700, 600, 500, 400, 300];

describe('hexToOklch', () => {
  it('黑白灰的亮度与色度符合 OKLCh 定义', () => {
    expect(hexToOklch('#000000').l).toBeCloseTo(0, 1);
    expect(hexToOklch('#ffffff').l).toBeCloseTo(100, 1);
    // 中性灰无色度
    expect(hexToOklch('#808080').c).toBeCloseTo(0, 2);
  });

  it('支持 3 位简写并与 6 位等价', () => {
    expect(hexToOklch('#abc')).toEqual(hexToOklch('#aabbcc'));
  });

  it('纯色相落在预期扇区（红≈29° 绿≈142° 蓝≈264°）', () => {
    expect(hexToOklch('#ff0000').h).toBeCloseTo(29, 0);
    expect(hexToOklch('#00ff00').h).toBeCloseTo(142, 0);
    expect(hexToOklch('#0000ff').h).toBeCloseTo(264, 0);
  });
});

describe('buildCustomTheme 色阶推导', () => {
  it('覆盖 gray + indigo 全档（CLAUDE.md 约定）', () => {
    const { vars } = buildCustomTheme('#131316', '#4f7cf7');
    for (const s of REQUIRED_GRAY) expect(vars, `缺 gray-${s}`).toHaveProperty(`--color-gray-${s}`);
    for (const s of REQUIRED_INDIGO) expect(vars, `缺 indigo-${s}`).toHaveProperty(`--color-indigo-${s}`);
    expect(vars).toHaveProperty('--color-foreground');
    expect(vars).toHaveProperty('--ring');
  });

  it('按背景亮度判定明暗（深底 → dark，浅底 → light）', () => {
    expect(buildCustomTheme('#131316', '#4f7cf7').scheme).toBe('dark');
    expect(buildCustomTheme('#e9e9ec', '#4f7cf7').scheme).toBe('light');
  });

  it('壳色（gray-950）对用户所选背景保真', () => {
    const bg = '#1a0b12';
    const src = hexToOklch(bg);
    const got = parseOklch(buildCustomTheme(bg, '#ec4899').vars['--color-gray-950']);
    expect(got.l).toBeCloseTo(src.l, 1);
    expect(got.c).toBeCloseTo(src.c, 3);
    expect(got.h).toBeCloseTo(src.h, 1);
  });

  it('近白背景：壳被限幅以留出卡片余量，表面档位不塌成一片白', () => {
    const { vars } = buildCustomTheme('#ffffff', '#4f7cf7');
    const at = (s: number) => parseOklch(vars[`--color-gray-${s}`]).l;
    expect(at(950), '壳应低于纯白').toBeLessThan(100);
    expect(at(800), '卡片应比壳更浅').toBeGreaterThan(at(950));
    // 壳 / 画布 / 卡片 / 边框必须互相可辨（塌成一片白即为回归）
    expect(new Set([at(950), at(900), at(800), at(700)]).size).toBe(4);
    expect(at(800) - at(950)).toBeGreaterThan(3);
  });

  it('品牌色（indigo-500）对用户所选品牌色保真', () => {
    const brand = '#16a34a';
    const src = hexToOklch(brand);
    const got = parseOklch(buildCustomTheme('#07130d', brand).vars['--color-indigo-500']);
    expect(got.l).toBeCloseTo(src.l, 1);
    expect(got.c).toBeCloseTo(src.c, 3);
    expect(got.h).toBeCloseTo(src.h, 1);
  });

  it('深色底：中性色阶从壳色单调递增到近白前景', () => {
    const { vars } = buildCustomTheme('#131316', '#4f7cf7');
    const ls = REQUIRED_GRAY.map((s) => parseOklch(vars[`--color-gray-${s}`]).l);
    for (let i = 1; i < ls.length; i++) {
      expect(ls[i], `gray-${REQUIRED_GRAY[i]} 应比 gray-${REQUIRED_GRAY[i - 1]} 亮`).toBeGreaterThan(ls[i - 1]);
    }
    expect(ls[ls.length - 1]).toBeGreaterThan(95); // 前景近白
  });

  it('浅色底：卡片(800)比壳(950)更浅，正文(50)向深走', () => {
    const { vars } = buildCustomTheme('#e9e9ec', '#4f7cf7');
    const shell = parseOklch(vars['--color-gray-950']).l;
    const card = parseOklch(vars['--color-gray-800']).l;
    const text = parseOklch(vars['--color-gray-50']).l;
    expect(card).toBeGreaterThan(shell);
    expect(text).toBeLessThan(20);
  });

  // 核心不变量：不管锚点取什么颜色，各档在「壳色 → 前景色」上的归一化位置
  // 都与参考色阶一致（= 曲线形状不变，只是两端被用户的颜色重新定义）。
  // 直接断言绝对亮度是行不通的——参考主题的壳色是 oklch 原生值，而入口是
  // hex（#131316 实为 L=18.8，非 dark 的 L=16），会引入 sRGB 近似误差。
  it.each([
    ['dark', '#131316', [16, 19.5, 23.5, 26, 28.5, 37, 55.2, 71, 86, 93, 96.5, 98.5]],
    ['light', '#e9e9ec', [92.5, 95.8, 100, 96.5, 93, 70, 55.2, 44, 30, 23, 18, 14.1]],
  ] as const)('%s 底：逐档归一化位置与参考色阶一致', (_name, bg, ref) => {
    const { vars } = buildCustomTheme(bg, '#4f7cf7');
    const ls = REQUIRED_GRAY.map((s) => parseOklch(vars[`--color-gray-${s}`]).l);
    const norm = (arr: readonly number[]) =>
      arr.map((v) => (v - arr[0]) / (arr[arr.length - 1] - arr[0]));
    const got = norm(ls);
    const want = norm(ref);
    got.forEach((t, i) => {
      expect(t, `gray-${REQUIRED_GRAY[i]} 归一化位置`).toBeCloseTo(want[i], 2);
    });
  });

  it('色度向文字端衰减，正文不被背景色染色', () => {
    const { vars } = buildCustomTheme('#1a0b12', '#ec4899'); // 高色度莓红底
    const shellC = parseOklch(vars['--color-gray-950']).c;
    const textC = parseOklch(vars['--color-gray-50']).c;
    expect(textC).toBeLessThan(shellC);
  });

  it('高饱和背景的表面档位被限幅（避免整屏刺眼）', () => {
    const { vars } = buildCustomTheme('#ff0000', '#4f7cf7'); // 纯红，色度 ≈0.26
    // 壳保真，但派生的画布/卡片档要收敛
    expect(parseOklch(vars['--color-gray-900']).c).toBeLessThanOrEqual(0.061);
    expect(parseOklch(vars['--color-gray-800']).c).toBeLessThanOrEqual(0.061);
  });

  it('所有档位亮度落在合法区间 [0,100]', () => {
    for (const bg of ['#000000', '#ffffff', '#ff0000', '#131316', '#e9e9ec']) {
      const { vars } = buildCustomTheme(bg, '#4f7cf7');
      for (const v of Object.values(vars)) {
        if (!v.startsWith('oklch(') || v.includes('/')) continue;
        const { l } = parseOklch(v);
        expect(l, `${bg} → ${v}`).toBeGreaterThanOrEqual(0);
        expect(l).toBeLessThanOrEqual(100);
      }
    }
  });
});

describe('背景图透明度（可见度滑块）', () => {
  const SURFACE = [950, 900, 800, 750, 700];
  const TEXTISH = [600, 500, 400, 300, 200, 100, 50];

  it('无图（null）时所有档位不透明，行为与纯色一致', () => {
    const { vars } = buildCustomTheme('#131316', '#4f7cf7', null);
    for (const s of [...SURFACE, ...TEXTISH]) {
      expect(alphaOf(vars[`--color-gray-${s}`]), `gray-${s} 应不透明`).toBe(1);
    }
  });

  it('可见度=100 时表面档位取最透边界（画布 0.55 / 卡片 0.88）', () => {
    const { vars } = buildCustomTheme('#131316', '#4f7cf7', 100);
    expect(alphaOf(vars['--color-gray-900'])).toBeCloseTo(0.55, 3);
    expect(alphaOf(vars['--color-gray-800'])).toBeCloseTo(0.88, 3);
    expect(alphaOf(vars['--color-gray-950'])).toBeCloseTo(0.72, 3);
  });

  it('可见度=0 时表面档位完全不透明（等于没图）', () => {
    const { vars } = buildCustomTheme('#131316', '#4f7cf7', 0);
    for (const s of SURFACE) expect(alphaOf(vars[`--color-gray-${s}`]), `gray-${s}`).toBe(1);
  });

  it('文字/图标档位在任何可见度下都保持不透明（正文不被穿透）', () => {
    for (const vis of [0, 50, 100]) {
      const { vars } = buildCustomTheme('#131316', '#4f7cf7', vis);
      for (const s of TEXTISH) {
        expect(alphaOf(vars[`--color-gray-${s}`]), `vis=${vis} gray-${s}`).toBe(1);
      }
    }
  });

  it('可见度越高表面越透（alpha 单调下降）', () => {
    const a = (vis: number) => alphaOf(buildCustomTheme('#131316', '#4f7cf7', vis).vars['--color-gray-900']);
    expect(a(100)).toBeLessThan(a(50));
    expect(a(50)).toBeLessThan(a(0));
  });

  it('可见度=50 是不透明与最透边界的中点', () => {
    const { vars } = buildCustomTheme('#131316', '#4f7cf7', 50);
    // 画布 base=0.55 → 中点 = 1 - 0.5*(1-0.55) = 0.775
    expect(alphaOf(vars['--color-gray-900'])).toBeCloseTo(0.775, 3);
  });
});

describe('自定义主题持久化与应用', () => {
  beforeEach(() => {
    localStorage.clear();
    clearCustomTheme();
    document.documentElement.dataset.theme = '';
  });

  it('未设置时回退到默认配色', () => {
    expect(getCustomColors()).toEqual({ bg: CUSTOM_DEFAULT_BG, brand: CUSTOM_DEFAULT_BRAND });
  });

  it('setCustomColors 持久化后可读回', () => {
    setCustomColors('#07130d', '#16a34a');
    expect(getCustomColors()).toEqual({ bg: '#07130d', brand: '#16a34a' });
  });

  it('背景图可见度默认 100，setBgVisible 持久化并夹到 [0,100]', () => {
    expect(getBgVisible()).toBe(CUSTOM_DEFAULT_BG_VISIBLE);
    setBgVisible(40);
    expect(getBgVisible()).toBe(40);
    setBgVisible(999);
    expect(getBgVisible()).toBe(100);
    setBgVisible(-5);
    expect(getBgVisible()).toBe(0);
  });

  it('applyCustomTheme 内联变量并按亮度标注 data-scheme', () => {
    setCustomColors('#e9e9ec', '#4f7cf7');
    const shell = applyCustomTheme();
    const el = document.documentElement;
    expect(shell).toBe('#e9e9ec');
    expect(el.dataset.scheme).toBe('light');
    expect(el.style.getPropertyValue('--color-gray-950')).toBeTruthy();
    expect(el.style.getPropertyValue('--color-indigo-500')).toBeTruthy();
  });

  it('clearCustomTheme 清空全部内联变量与 data-scheme', () => {
    setCustomColors('#07130d', '#16a34a');
    applyCustomTheme();
    clearCustomTheme();
    const el = document.documentElement;
    expect(el.dataset.scheme).toBeUndefined();
    for (const s of REQUIRED_GRAY) expect(el.style.getPropertyValue(`--color-gray-${s}`)).toBe('');
    for (const s of REQUIRED_INDIGO) expect(el.style.getPropertyValue(`--color-indigo-${s}`)).toBe('');
    expect(el.style.getPropertyValue('--color-foreground')).toBe('');
  });

  it('切走自定义主题时清场，内联变量不会盖住新主题', () => {
    setTheme('custom');
    expect(document.documentElement.style.getPropertyValue('--color-gray-950')).toBeTruthy();
    setTheme('feishu');
    expect(document.documentElement.style.getPropertyValue('--color-gray-950')).toBe('');
    expect(document.documentElement.dataset.scheme).toBeUndefined();
  });

  it('custom 主题的 theme-color 取用户所选背景（而非注册表常量）', () => {
    document.head.innerHTML = '<meta name="theme-color" content="#131316">';
    setCustomColors('#07130d', '#16a34a');
    setTheme('custom');
    expect(document.querySelector('meta[name="theme-color"]')!.getAttribute('content')).toBe('#07130d');
  });
});
