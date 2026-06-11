import { useRef, useState, useCallback } from 'react';
import { api } from '../api/client';
import type { Task } from '../api/client';

/** 排序键：手动 sort_order 优先，否则最近访问/创建时间（秒）。越大越靠前。 */
export function effectiveKey(t: Task): number {
  if (t.sort_order != null) return t.sort_order;
  const ts = t.last_accessed_at || t.created_at;
  if (!ts) return 0;
  const iso = ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z';
  return new Date(iso).getTime() / 1000;
}

/** 计算把 fromIdx 的任务移到 toIdx（移除后的下标）所需的 sort_order。 */
function newSortFor(list: Task[], fromIdx: number, toIdx: number): number {
  const without = list.filter((_, i) => i !== fromIdx);
  const prev = toIdx > 0 ? without[toIdx - 1] : null;
  const next = toIdx < without.length ? without[toIdx] : null;
  const pk = prev ? effectiveKey(prev) : null;
  const nk = next ? effectiveKey(next) : null;
  if (pk != null && nk != null) return (pk + nk) / 2;
  if (pk == null && nk != null) return nk + 3600; // 置顶（同组内）
  if (pk != null && nk == null) return pk - 3600; // 置底
  return Date.now() / 1000;
}

/** 同 starred 分组内的下标范围（标星置顶逻辑：不允许跨组拖动）。 */
function groupRange(list: Task[], idx: number): [number, number] {
  const starred = list[idx]?.starred ?? false;
  let start = 0;
  let end = list.length - 1;
  for (let i = 0; i < list.length; i++) {
    if ((list[i].starred ?? false) === starred) { start = i; break; }
  }
  for (let i = list.length - 1; i >= 0; i--) {
    if ((list[i].starred ?? false) === starred) { end = i; break; }
  }
  return [start, end];
}

interface ReorderApi {
  draggingId: number | null;
  overIndex: number | null;
  /** 给每个任务行/项 spread 的属性（含桌面 DnD + 移动端长按拖动）。 */
  itemProps: (t: Task, idx: number) => Record<string, unknown>;
}

/**
 * 任务拖拽排序（任务列表与侧边栏共用）。
 * - 桌面：HTML5 drag & drop
 * - 移动端：长按 450ms 激活后跟随手指
 * - 标星置顶保留：只能在同 starred 分组内移动
 */
export function useTaskReorder(tasks: Task[], onReordered: () => void): ReorderApi {
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);
  const longPress = useRef<ReturnType<typeof setTimeout> | null>(null);
  const touchActive = useRef(false);

  const commit = useCallback(async (fromIdx: number, toIdxRaw: number) => {
    const [gs, ge] = groupRange(tasks, fromIdx);
    const toIdx = Math.min(Math.max(toIdxRaw, gs), ge);
    if (toIdx === fromIdx) return;
    const sort = newSortFor(tasks, fromIdx, toIdx > fromIdx ? toIdx : toIdx);
    try {
      await api.updateTask(tasks[fromIdx].id, { sort_order: sort });
      onReordered();
    } catch { /* keep order */ }
  }, [tasks, onReordered]);

  const finish = useCallback((fromId: number | null, toIdx: number | null) => {
    setDraggingId(null);
    setOverIndex(null);
    document.body.style.overflow = '';
    if (fromId == null || toIdx == null) return;
    const fromIdx = tasks.findIndex((t) => t.id === fromId);
    if (fromIdx < 0) return;
    void commit(fromIdx, toIdx);
  }, [tasks, commit]);

  const itemProps = useCallback((t: Task, idx: number) => ({
    'data-reorder-idx': idx,
    draggable: true,
    onDragStart: (e: React.DragEvent) => {
      e.dataTransfer.effectAllowed = 'move';
      setDraggingId(t.id);
    },
    onDragOver: (e: React.DragEvent) => {
      if (draggingId == null) return;
      e.preventDefault();
      setOverIndex(idx);
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      finish(draggingId, idx);
    },
    onDragEnd: () => { setDraggingId(null); setOverIndex(null); },
    // 移动端长按拖动
    onTouchStart: () => {
      longPress.current = setTimeout(() => {
        touchActive.current = true;
        setDraggingId(t.id);
        document.body.style.overflow = 'hidden'; // 拖动期间禁页面滚动
        if (navigator.vibrate) navigator.vibrate(30);
      }, 450);
    },
    onTouchMove: (e: React.TouchEvent) => {
      if (!touchActive.current) {
        // 长按未触发前移动 = 滚动意图，取消长按
        if (longPress.current) { clearTimeout(longPress.current); longPress.current = null; }
        return;
      }
      const touch = e.touches[0];
      const el = document.elementFromPoint(touch.clientX, touch.clientY);
      const item = el?.closest('[data-reorder-idx]');
      if (item) setOverIndex(Number(item.getAttribute('data-reorder-idx')));
    },
    onTouchEnd: () => {
      if (longPress.current) { clearTimeout(longPress.current); longPress.current = null; }
      if (touchActive.current) {
        touchActive.current = false;
        finish(draggingId, overIndex);
      }
    },
  }), [draggingId, overIndex, finish]);

  return { draggingId, overIndex, itemProps };
}
