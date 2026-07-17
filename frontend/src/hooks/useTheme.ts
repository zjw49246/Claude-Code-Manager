import { useSyncExternalStore } from 'react';
import { getTheme, subscribeTheme, type Theme } from '../config/theme';

/** 当前主题（响应 setTheme 实时更新）。目前用于按主题解析导航图标集。 */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribeTheme, getTheme);
}
