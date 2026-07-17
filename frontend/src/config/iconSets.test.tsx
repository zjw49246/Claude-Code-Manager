import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { ICON_SETS, NAV_ICON_KEYS, getNavIcon } from './iconSets';
import { THEME_OPTIONS } from './theme';

describe('主题图标集注册表', () => {
  it('每个主题声明的 iconSet 都已注册（新主题接入自检）', () => {
    for (const o of THEME_OPTIONS) {
      if (o.iconSet) {
        expect(ICON_SETS[o.iconSet], `主题 ${o.value} 声明的图标集 "${o.iconSet}" 未在 iconSets.tsx 注册`).toBeTruthy();
      }
    }
  });

  it('飞书/苹果主题声明各自图标集，其余主题缺省 Lucide', () => {
    const by = (v: string) => THEME_OPTIONS.find((o) => o.value === v)!;
    expect(by('feishu').iconSet).toBe('feishu');
    expect(by('apple').iconSet).toBe('sf');
    expect(by('light').iconSet).toBeUndefined();
    expect(by('dark').iconSet).toBeUndefined();
  });

  it('每个图标集覆盖全部导航语义 key，且 active/非 active 都渲染出 svg', () => {
    // 新增导航页（NAV_ICON_KEYS 加 key）而没补图标集时，这里会精确红出缺哪个
    for (const [name, set] of Object.entries(ICON_SETS)) {
      for (const key of NAV_ICON_KEYS) {
        const renderer = set[key];
        expect(renderer, `图标集 ${name} 缺导航 key "${key}"`).toBeTypeOf('function');
        for (const active of [true, false]) {
          const { container, unmount } = render(<>{renderer({ size: 16, active })}</>);
          expect(
            container.querySelector('svg'),
            `${name}.${key} (active=${active}) 未渲染出 svg`,
          ).toBeTruthy();
          unmount();
        }
      }
    }
  });

  it('getNavIcon：未声明图标集 / 未注册集合 / 未知 key 一律回退 null（Lucide 兜底）', () => {
    expect(getNavIcon(undefined, 'tasks')).toBeNull();
    expect(getNavIcon('no-such-set', 'tasks')).toBeNull();
    expect(getNavIcon('feishu', 'no-such-page')).toBeNull();
  });

  it('飞书集是 two-tone 双色：选中=飞书蓝+淡蓝填充，未选中=深灰+白填充（官方 rail 取证）', () => {
    const r = ICON_SETS.feishu.tasks;
    const active = render(<>{r({ size: 16, active: true })}</>);
    expect(active.container.innerHTML).toContain('#3370ff');
    expect(active.container.innerHTML).toContain('#c7dcff');
    active.unmount();
    const idle = render(<>{r({ size: 16, active: false })}</>);
    expect(idle.container.innerHTML).toContain('#51565d');
    expect(idle.container.innerHTML).toContain('#ffffff');
    idle.unmount();
  });
});
