import type { ReactNode } from 'react';
import {
  DashboardTwo, ListCheckbox, FolderCode, Key, FolderClose, CommentOne,
  PullRequests, Server, Magic, Peoples, International,
} from '@icon-park/react';
import {
  IoGrid, IoList, IoFolder, IoKey, IoDocuments, IoChatbubbles,
  IoGitPullRequest, IoServer, IoSparkles, IoPeople, IoGlobe,
} from 'react-icons/io5';

/**
 * 主题图标集注册表
 * ----------------------------------------------------------------------
 * 与主题系统一脉相承的声明式机制：ThemeOption.iconSet 指向这里注册的集合名，
 * AppShell 按导航语义 key 解析渲染器；未声明 iconSet 的主题、或集合缺某个
 * key 时，一律回退 Lucide 默认图标 —— 图标集是纯增强，绝不成为阻塞项。
 *
 * 新增主题接入图标集（可选步骤）：
 *   1. 此文件注册一个 IconSet（覆盖 NAV_ICON_KEYS 全部 key，测试有断言）；
 *   2. theme.ts 的主题条目加 iconSet: '<注册名>'。
 * 新增导航页时：NAV_ICON_KEYS 补 key + 各集合补图标（iconSets.test 会红）。
 *
 * 集合来源（授权与出处）：
 * - feishu → IconPark（字节跳动官方开源图标库，Apache-2.0）two-tone 双色，
 *   与飞书系产品同源的图标语言；
 * - sf → Ionicons 5（MIT，react-icons/io5），iOS 系统风格填充图标，
 *   颜色交给 CSS currentColor（apple 主题的 squircle 白色线稿由结构层控制）。
 */

/** 导航语义图标名 = AppShell 导航项的 key */
export const NAV_ICON_KEYS = [
  'dashboard', 'tasks', 'projects', 'secrets', 'files', 'discussions',
  'pr-monitor', 'workers', 'skills', 'team', 'server',
] as const;
export type NavIconKey = typeof NAV_ICON_KEYS[number];

export interface NavIconProps {
  size: number;
  /** 选中态（飞书 two-tone 依赖它切换蓝/灰双色；sf 集颜色走 CSS，忽略） */
  active?: boolean;
}
export type NavIconRenderer = (props: NavIconProps) => ReactNode;
export type IconSet = Record<NavIconKey, NavIconRenderer>;

/* eslint-disable @typescript-eslint/no-explicit-any -- 两个三方库的组件 props
   类型互不兼容，注册表内以最小公共面（size/theme/fill）调用 */

/** 飞书 two-tone 双色：主笔画 + 次级填充（取证自官方 rail：选中飞书蓝，未选中 N600 灰） */
/* 官方 rail 取证：未选中 = 深灰笔画 + 白色内部填充（灰 rail 上的经典飞书
   duotone）；选中 = 飞书蓝笔画 + 淡蓝填充（白 tile 上） */
const feishuFill = (active?: boolean): string[] =>
  active ? ['#3370ff', '#c7dcff'] : ['#51565d', '#ffffff'];
const fs = (C: any): NavIconRenderer => ({ size, active }) => (
  <C size={size} theme="two-tone" fill={feishuFill(active)} strokeWidth={4} />
);

const sf = (C: any): NavIconRenderer => ({ size }) => <C size={size} />;

export const ICON_SETS: Record<string, IconSet> = {
  feishu: {
    dashboard: fs(DashboardTwo),
    tasks: fs(ListCheckbox),
    projects: fs(FolderCode),
    secrets: fs(Key),
    files: fs(FolderClose),
    discussions: fs(CommentOne),
    'pr-monitor': fs(PullRequests),
    workers: fs(Server),
    skills: fs(Magic),
    team: fs(Peoples),
    server: fs(International),
  },
  sf: {
    dashboard: sf(IoGrid),
    tasks: sf(IoList),
    projects: sf(IoFolder),
    secrets: sf(IoKey),
    files: sf(IoDocuments),
    discussions: sf(IoChatbubbles),
    'pr-monitor': sf(IoGitPullRequest),
    workers: sf(IoServer),
    skills: sf(IoSparkles),
    team: sf(IoPeople),
    server: sf(IoGlobe),
  },
};

/** 解析导航图标渲染器；主题未声明集合 / 集合缺该 key → null（调用方回退 Lucide） */
export function getNavIcon(iconSet: string | undefined, key: string): NavIconRenderer | null {
  if (!iconSet) return null;
  return ICON_SETS[iconSet]?.[key as NavIconKey] ?? null;
}
