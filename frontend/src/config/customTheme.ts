/*
 * 自定义主题：从「背景色 + 品牌色」两个颜色推导整套 CSS 变量。
 * ----------------------------------------------------------------------
 * 换肤机制与其它主题一致（覆盖 --color-gray-* / --color-indigo-*），区别
 * 只在于：其它主题的色阶是手写常量，自定义主题的色阶是运行时算出来的，
 * 以内联样式写在 documentElement 上。组件类名不变，故按钮/图标/边框全部
 * 自动跟随。
 *
 * 推导方式（关键）：不是简单地把颜色变亮变暗，而是**沿用参考主题色阶的
 * 曲线形状**——把每一档在「壳色 → 前景色」这条线上的归一化位置 t 算出来，
 * 再用用户的颜色重新插值。t 由参考主题反推，故 userL == 参考壳色亮度时
 * 生成结果与参考主题逐档一致（见 customTheme.test.ts）。
 */

const BG_KEY = 'cc_theme_custom_bg';
const BRAND_KEY = 'cc_theme_custom_brand';
/** 有背景图标记。存 localStorage 而非跟图片一起放 IDB：色阶推导要同步知道
 * 该不该给表面档位加 alpha，而读 IDB 是异步的（图片字节由 customBg 异步铺）。 */
const HAS_BG_KEY = 'cc_theme_custom_has_bg';

export const hasBgImage = () => localStorage.getItem(HAS_BG_KEY) === '1';
export const setHasBgImage = (on: boolean) =>
  on ? localStorage.setItem(HAS_BG_KEY, '1') : localStorage.removeItem(HAS_BG_KEY);

export const CUSTOM_DEFAULT_BG = '#131316';
export const CUSTOM_DEFAULT_BRAND = '#4f7cf7';

/** L > 该值判定为浅色底，走浅色曲线（文字向深走）。 */
const LIGHT_L_CUTOFF = 60;
/**
 * 浅色底的壳色亮度上限。浅色曲线里卡片(800)要比壳更浅约 7.5 个亮度点，壳
 * 越接近纯白、可用余量越少：壳取 100% 时 800/750/700 会一起被夹到 100，
 * 壳/画布/卡片/边框塌成一片白、界面失去层次。故把壳压到留有余量的位置——
 * 选纯白得到的是「近白」而非纯白，这是让界面可用的必要让步。
 */
const LIGHT_SHELL_L_MAX = 92.5;
/** 表面档位的色度上限：给足色调但不至于刺眼（用户原色仅 950 壳保真）。 */
const SURFACE_C_CAP = 0.06;
/** 色度向文字端衰减比例：t=1 处保留 35%，避免正文被染色。 */
const TEXT_C_FALLOFF = 0.65;

export interface Oklch { l: number; c: number; h: number }

/** sRGB hex → OKLCh（l: 0-100, c: 0-0.4, h: 0-360）。 */
export function hexToOklch(hex: string): Oklch {
  const raw = hex.replace('#', '').trim();
  const full = raw.length === 3 ? raw.split('').map((x) => x + x).join('') : raw;
  const r = parseInt(full.slice(0, 2), 16) / 255;
  const g = parseInt(full.slice(2, 4), 16) / 255;
  const b = parseInt(full.slice(4, 6), 16) / 255;
  const lin = (v: number) => (v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  const R = lin(r), G = lin(g), B = lin(b);
  const l_ = Math.cbrt(0.4122214708 * R + 0.5363325363 * G + 0.0514459929 * B);
  const m_ = Math.cbrt(0.2119034982 * R + 0.6806995451 * G + 0.1073969566 * B);
  const s_ = Math.cbrt(0.0883024619 * R + 0.2817188376 * G + 0.6299787005 * B);
  const L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_;
  const a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_;
  const bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_;
  let h = (Math.atan2(bb, a) * 180) / Math.PI;
  if (h < 0) h += 360;
  return { l: L * 100, c: Math.sqrt(a * a + bb * bb), h };
}

/** OKLCh → sRGB hex（超出色域时按通道截断）。取色结果要回填 <input type=color>。 */
export function oklchToHex({ l, c, h }: Oklch): string {
  const L = l / 100;
  const rad = (h * Math.PI) / 180;
  const a = c * Math.cos(rad);
  const b = c * Math.sin(rad);
  const l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
  const lin = [
    4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
    -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
    -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
  ];
  const hex = lin
    .map((v) => (v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(Math.max(v, 0), 1 / 2.4) - 0.055))
    .map((v) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, '0'))
    .join('');
  return `#${hex}`;
}

const clampL = (l: number) => Math.max(0, Math.min(100, l));
const round = (v: number, p: number) => Number(v.toFixed(p));
const css = (o: Oklch, alpha?: number) => {
  const base = `${round(clampL(o.l), 2)}% ${round(Math.max(0, o.c), 4)} ${round(o.h, 2)}`;
  return `oklch(${alpha === undefined ? base : `${base} / ${alpha}`})`;
};

const GRAY_STOPS = ['950', '900', '800', '750', '700', '600', '500', '400', '300', '200', '100', '50'] as const;

/**
 * 有背景图时各表面档位的不透明度（无图时全部不透明，行为不变）。
 * 只有表面档（侧栏/画布/卡片/边框）透明——文字与图标档位必须保持不透明，
 * 否则正文会被背景图穿透、可读性崩掉。
 * 数值按叠加后的实效不透明度取：卡片(800) 落在画布(900) 上，对图片的实效
 * 遮盖 = 0.88 + 0.12×0.55 ≈ 0.95，故卡片上的文字仍然干净。
 */
const SURFACE_ALPHA: Partial<Record<typeof GRAY_STOPS[number], number>> = {
  '950': 0.72,  // 侧栏壳
  '900': 0.55,  // 画布：透出最多，背景图主要在这里可见
  '800': 0.88,  // 卡片：以可读性优先，只留一点通透感
  '750': 0.85,
  '700': 0.82,  // 边框/控件底
};

/** 参考中性色阶亮度（index.css 的 dark / light 主题逐档实测值）。 */
const GRAY_REF = {
  dark: [16, 19.5, 23.5, 26, 28.5, 37, 55.2, 71, 86, 93, 96.5, 98.5],
  light: [92.5, 95.8, 100, 96.5, 93, 70, 55.2, 44, 30, 23, 18, 14.1],
} as const;

const BRAND_STOPS = ['950', '900', '800', '700', '600', '500', '400', '300'] as const;

/** 参考品牌色阶（index.css 的 indigo 逐档实测值），以 500 档为锚。 */
const BRAND_REF = {
  dark: { l: [25, 32, 42, 49, 55, 62, 70, 78], c: [0.06, 0.09, 0.14, 0.17, 0.18, 0.17, 0.14, 0.1] },
  light: { l: [93, 88, 38, 42, 48, 52, 50, 46], c: [0.03, 0.05, 0.14, 0.17, 0.19, 0.18, 0.17, 0.16] },
} as const;

/**
 * 把参考色阶换算成每档在「壳色(950) → 前景色(50)」上的归一化位置。
 * t=0 即壳色，t=1 即前景色；t<0 表示比壳色更远离前景（浅色主题的白卡片）。
 */
function grayPositions(scheme: 'dark' | 'light'): number[] {
  const ref = GRAY_REF[scheme];
  const shell = ref[0];
  const span = ref[ref.length - 1] - shell;
  return ref.map((l) => (l - shell) / span);
}

/** 品牌色各档相对 500 锚点的亮度偏移与色度比例。 */
function brandDeltas(scheme: 'dark' | 'light') {
  const ref = BRAND_REF[scheme];
  const anchor = BRAND_STOPS.indexOf('500');
  return {
    dl: ref.l.map((l) => l - ref.l[anchor]),
    cr: ref.c.map((c) => c / ref.c[anchor]),
  };
}

export interface CustomThemeResult {
  scheme: 'dark' | 'light';
  vars: Record<string, string>;
  /** 状态栏 meta 用的近似色（= 用户所选背景色原值）。 */
  metaColor: string;
}

/** 从背景色 + 品牌色生成整套 CSS 变量。translucent=true 时表面档位带 alpha，
 * 供背景图透出（见 SURFACE_ALPHA）。 */
export function buildCustomTheme(bgHex: string, brandHex: string, translucent = false): CustomThemeResult {
  const bg = hexToOklch(bgHex);
  const brand = hexToOklch(brandHex);
  const scheme: 'dark' | 'light' = bg.l > LIGHT_L_CUTOFF ? 'light' : 'dark';
  const shellL = scheme === 'light' ? Math.min(bg.l, LIGHT_SHELL_L_MAX) : bg.l;

  // 前景端：深色底配近白文字，浅色底配近黑文字（沿用参考主题的两端取值）。
  const fgL = scheme === 'dark' ? 98.5 : 14.1;
  const vars: Record<string, string> = {};

  const surfaceC = Math.min(bg.c, SURFACE_C_CAP);
  grayPositions(scheme).forEach((t, i) => {
    const stop = GRAY_STOPS[i];
    // 壳色（950）保色度：用户挑的就是它，不做色度裁剪。
    const c = i === 0 ? bg.c : surfaceC * (1 - TEXT_C_FALLOFF * Math.max(0, Math.min(1, t)));
    const alpha = translucent ? SURFACE_ALPHA[stop] : undefined;
    vars[`--color-gray-${stop}`] = css({ l: shellL + t * (fgL - shellL), c, h: bg.h }, alpha);
  });

  const { dl, cr } = brandDeltas(scheme);
  BRAND_STOPS.forEach((stop, i) => {
    vars[`--color-indigo-${stop}`] = css({ l: brand.l + dl[i], c: brand.c * cr[i], h: brand.h });
  });

  vars['--color-foreground'] = css({ l: fgL, c: surfaceC * 0.35, h: bg.h });
  vars['--ring'] = css(brand);
  vars['--scrollbar-thumb'] = scheme === 'dark' ? 'oklch(100% 0 0 / 0.14)' : 'oklch(0% 0 0 / 0.14)';
  vars['--scrollbar-thumb-hover'] = scheme === 'dark' ? 'oklch(100% 0 0 / 0.24)' : 'oklch(0% 0 0 / 0.26)';

  return { scheme, vars, metaColor: bgHex };
}

export function getCustomColors(): { bg: string; brand: string } {
  return {
    bg: localStorage.getItem(BG_KEY) || CUSTOM_DEFAULT_BG,
    brand: localStorage.getItem(BRAND_KEY) || CUSTOM_DEFAULT_BRAND,
  };
}

export function setCustomColors(bg: string, brand: string) {
  localStorage.setItem(BG_KEY, bg);
  localStorage.setItem(BRAND_KEY, brand);
}

/** 全部由本模块管理的变量名（切走主题时要逐一清除）。 */
const MANAGED_VARS = [
  ...GRAY_STOPS.map((s) => `--color-gray-${s}`),
  ...BRAND_STOPS.map((s) => `--color-indigo-${s}`),
  '--color-foreground',
  '--ring',
  '--scrollbar-thumb',
  '--scrollbar-thumb-hover',
];

/** 应用自定义主题：内联变量 + data-scheme（浅色底要触发 accent 反转规则）。
 * 返回状态栏 meta 用色。 */
export function applyCustomTheme(): string {
  const { bg, brand } = getCustomColors();
  // 有图时表面档位带 alpha 透出背景图。标记读 localStorage 而非 IDB：色阶必须
  // 同步算出来（applyTheme 是同步的），图片字节则异步铺上（applyBgImage）。
  const { scheme, vars, metaColor } = buildCustomTheme(bg, brand, hasBgImage());
  const el = document.documentElement;
  Object.entries(vars).forEach(([k, v]) => el.style.setProperty(k, v));
  el.dataset.scheme = scheme;
  // 壳背景跟随生成的 950（而非用户原色）：浅色底会限幅，直接用原色会与
  // 侧栏 bg-gray-950 对不上，overscroll 处露出色差
  el.style.backgroundColor = 'var(--color-gray-950)';
  el.style.colorScheme = scheme;
  return metaColor;
}

/** 背景图相关的内联属性（切走主题要一并清掉，否则壁纸会留在新主题下）。 */
const BG_IMAGE_PROPS = ['background-image', 'background-size', 'background-position', 'background-attachment'];

/** 切换到非自定义主题时清场，避免内联变量/壁纸盖住新主题。 */
export function clearCustomTheme() {
  const el = document.documentElement;
  MANAGED_VARS.forEach((v) => el.style.removeProperty(v));
  BG_IMAGE_PROPS.forEach((p) => el.style.removeProperty(p));
  delete el.dataset.scheme;
  el.style.removeProperty('background-color');
  el.style.removeProperty('color-scheme');
}
