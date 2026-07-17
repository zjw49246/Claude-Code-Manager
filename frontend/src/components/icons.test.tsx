import { describe, it, expect, afterEach } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { render, cleanup, act } from '@testing-library/react';
import { Star, Settings, Bot, X } from './icons';
import { setTheme } from '../config/theme';

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

describe('中央图标模块', () => {
  afterEach(() => {
    act(() => setTheme('dark'));
    cleanup();
  });

  it('架构守卫：除 icons.tsx / iconSets.tsx 外，禁止任何文件值导入 lucide-react', () => {
    // 直接 import lucide 的图标不会跟随主题图标集切换（本轮返工的根因），
    // 新代码必须从 components/icons 导入。type-only 导入（类型标注）豁免。
    const root = join(process.cwd(), 'src');
    const offenders: string[] = [];
    for (const f of walk(root)) {
      if (f.endsWith('components/icons.tsx') || f.endsWith('config/iconSets.tsx')) continue;
      const src = readFileSync(f, 'utf-8');
      for (const m of src.matchAll(/import\s+(type\s+)?\{[^}]*\}\s*from\s*'lucide-react'/gs)) {
        if (!m[1]) offenders.push(f.slice(root.length + 1));
      }
    }
    expect(offenders, `这些文件仍在值导入 lucide-react，应改从 components/icons 导入：\n${offenders.join('\n')}`).toEqual([]);
  });

  it('映射图标随主题切换：feishu → IconPark，apple → Ionicons，dark → Lucide 裸渲染', () => {
    const { container } = render(<Star size={14} className="text-yellow-400" />);
    expect(container.querySelector('svg')).toBeTruthy();
    expect(container.querySelector('[data-icon-set]')).toBeNull(); // dark = lucide

    act(() => setTheme('feishu'));
    expect(container.querySelector("[data-icon-set='feishu'] svg")).toBeTruthy();

    act(() => setTheme('apple'));
    expect(container.querySelector("[data-icon-set='sf'] svg")).toBeTruthy();

    act(() => setTheme('dark'));
    expect(container.querySelector('[data-icon-set]')).toBeNull();
    expect(container.querySelector('svg')).toBeTruthy();
  });

  it('className 透传（尺寸与文字色语义在三套图标下一致可用）', () => {
    act(() => setTheme('feishu'));
    const { container } = render(<Settings size={16} className="animate-spin" />);
    expect(container.innerHTML).toContain('animate-spin');
    act(() => setTheme('apple'));
    const again = render(<X size={16} className="shrink-0" />);
    expect(again.container.innerHTML).toContain('shrink-0');
  });

  it('fill 语义翻译：收藏星标的实心/空心在三套图标下都成立（TaskForm 空白按钮回归）', () => {
    // lucide 惯用 fill='currentColor'|'none'；直接透传会让 IconPark/Ionicons 隐形
    act(() => setTheme('feishu'));
    const solid = render(<Star size={13} fill="currentColor" />);
    const hollow = render(<Star size={13} fill="none" />);
    expect(solid.container.querySelector('svg')).toBeTruthy();
    expect(hollow.container.querySelector('svg')).toBeTruthy();
    // 实心/空心必须渲染出不同结果（filled vs outline 主题）
    expect(solid.container.innerHTML).not.toBe(hollow.container.innerHTML);
    // 笔画必须落在 currentColor 上（IconPark svg 根自带 fill="none" 是正常的，
    // 隐形 bug 的特征是 path 的 stroke/fill 全被 'none' 覆盖）
    expect(hollow.container.innerHTML).toContain('currentColor');
    solid.unmount(); hollow.unmount();

    act(() => setTheme('apple'));
    const sSolid = render(<Star size={13} fill="currentColor" />);
    const sHollow = render(<Star size={13} fill="none" />);
    expect(sSolid.container.querySelector('svg')).toBeTruthy();
    expect(sHollow.container.querySelector('svg')).toBeTruthy();
    expect(sSolid.container.innerHTML).not.toBe(sHollow.container.innerHTML); // IoStar vs IoStarOutline
  });

  it('无映射图标（如 Bot 品牌标识）在任何主题下都渲染 Lucide 原样', () => {
    act(() => setTheme('feishu'));
    const { container } = render(<Bot size={18} />);
    expect(container.querySelector('[data-icon-set]')).toBeNull();
    expect(container.querySelector('svg.lucide')).toBeTruthy();
  });
});
