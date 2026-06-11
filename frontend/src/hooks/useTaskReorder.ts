import { useRef, useState, useCallback, useEffect } from 'react';
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
  /** 整行可拖（侧边栏用）：targetProps + handleProps 合并。 */
  itemProps: (t: Task, idx: number) => Record<string, unknown>;
  /** 拖放目标 + 移动端长按（行容器用）。 */
  targetProps: (t: Task, idx: number) => Record<string, unknown>;
  /** 桌面拖拽手柄（行内有大段可选中文字时，整行 draggable 会被
   * 文本选择手势抢走 dragStart——主列表必须用显式手柄）。 */
  handleProps: (t: Task, idx: number) => Record<string, unknown>;
}

/**
 * 任务拖拽排序（任务列表与侧边栏共用）。
 * - 桌面：HTML5 drag & drop
 * - 移动端：长按 450ms 激活；激活后在 document 上挂非被动 touchmove
 *   （preventDefault 阻止浏览器把手势接管成滚动 → 否则会收到 touchcancel
 *   导致"浮起来但拖不动"）
 * - 标星置顶保留：只能在同 starred 分组内移动
 */
export function useTaskReorder(tasks: Task[], onReordered: () => void): ReorderApi {
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);
  const longPress = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 实时引用，供 document 级监听器读取（避免闭包过期）
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;
  const dragRef = useRef<number | null>(null);
  const overRef = useRef<number | null>(null);

  const commit = useCallback(async (fromId: number, toIdxRaw: number) => {
    const list = tasksRef.current;
    const fromIdx = list.findIndex((t) => t.id === fromId);
    if (fromIdx < 0) return;
    const [gs, ge] = groupRange(list, fromIdx);
    const toIdx = Math.min(Math.max(toIdxRaw, gs), ge);
    if (toIdx === fromIdx) return;
    const sort = newSortFor(list, fromIdx, toIdx);
    try {
      await api.updateTask(fromId, { sort_order: sort });
      onReordered();
    } catch { /* keep order */ }
  }, [onReordered]);

  const endDrag = useCallback((commitDrop: boolean) => {
    const fromId = dragRef.current;
    const toIdx = overRef.current;
    dragRef.current = null;
    overRef.current = null;
    setDraggingId(null);
    setOverIndex(null);
    document.body.style.overflow = '';
    if (commitDrop && fromId != null && toIdx != null) void commit(fromId, toIdx);
  }, [commit]);

  // 移动端：激活后用 document 级监听器跟踪手指（非被动，可 preventDefault）
  useEffect(() => {
    if (draggingId == null) return;
    const onMove = (e: TouchEvent) => {
      e.preventDefault(); // 阻止滚动接管，避免 touchcancel
      const touch = e.touches[0];
      const el = document.elementFromPoint(touch.clientX, touch.clientY);
      const item = el?.closest('[data-reorder-idx]');
      if (item) {
        const idx = Number(item.getAttribute('data-reorder-idx'));
        overRef.current = idx;
        setOverIndex(idx);
      }
    };
    const onEnd = () => endDrag(true);
    const onCancel = () => endDrag(false);
    document.addEventListener('touchmove', onMove, { passive: false });
    document.addEventListener('touchend', onEnd);
    document.addEventListener('touchcancel', onCancel);
    return () => {
      document.removeEventListener('touchmove', onMove);
      document.removeEventListener('touchend', onEnd);
      document.removeEventListener('touchcancel', onCancel);
    };
  }, [draggingId, endDrag]);

  const handleProps = useCallback((t: Task, _idx: number) => ({
    draggable: true,
    onDragStart: (e: React.DragEvent) => {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(t.id)); // Firefox 需要 setData 才会启动拖拽
      dragRef.current = t.id;
      setDraggingId(t.id);
    },
    onDragEnd: () => endDrag(false),
  }), [endDrag]);

  const targetProps = useCallback((t: Task, idx: number) => ({
    'data-reorder-idx': idx,
    onDragOver: (e: React.DragEvent) => {
      if (dragRef.current == null) return;
      e.preventDefault();
      overRef.current = idx;
      setOverIndex(idx);
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      overRef.current = idx;
      endDrag(true);
    },
    // 移动端长按激活
    onTouchStart: () => {
      longPress.current = setTimeout(() => {
        dragRef.current = t.id;
        overRef.current = idx;
        setDraggingId(t.id);
        document.body.style.overflow = 'hidden';
        if (navigator.vibrate) navigator.vibrate(30);
      }, 450);
    },
    onTouchMove: () => {
      // 长按未触发前移动 = 滚动意图，取消长按（激活后由 document 监听接管）
      if (dragRef.current == null && longPress.current) {
        clearTimeout(longPress.current);
        longPress.current = null;
      }
    },
    onTouchEnd: () => {
      if (longPress.current) { clearTimeout(longPress.current); longPress.current = null; }
    },
  }), [endDrag]);

  const itemProps = useCallback((t: Task, idx: number) => ({
    ...targetProps(t, idx),
    ...handleProps(t, idx),
  }), [targetProps, handleProps]);

  return { draggingId, overIndex, itemProps, targetProps, handleProps };
}
