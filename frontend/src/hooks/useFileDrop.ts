import { useEffect, type RefObject } from 'react';

interface UseFileDropOptions {
  targetRef?: RefObject<HTMLElement | null>;
  onDrop: (files: File[]) => void;
  disabled?: boolean;
}

export function useFileDrop({ targetRef, onDrop, disabled = false }: UseFileDropOptions) {
  useEffect(() => {
    if (disabled) return;

    const target = targetRef?.current ?? document;
    const isLocalTarget = !!targetRef?.current;

    const handleDragOver = (e: Event) => {
      e.preventDefault();
      e.stopPropagation();
      const dt = (e as DragEvent).dataTransfer;
      if (dt) dt.dropEffect = 'copy';
    };

    const handleDrop = (e: Event) => {
      e.preventDefault();
      e.stopPropagation();
      const dt = (e as DragEvent).dataTransfer;
      if (!dt?.files.length) return;
      onDrop(Array.from(dt.files));
    };

    target.addEventListener('dragover', handleDragOver);
    target.addEventListener('drop', handleDrop);

    const blockBrowserOpen = (e: Event) => {
      e.preventDefault();
      const dt = (e as DragEvent).dataTransfer;
      if (dt) dt.dropEffect = 'none';
    };
    if (isLocalTarget) {
      document.addEventListener('dragover', blockBrowserOpen);
      document.addEventListener('drop', blockBrowserOpen);
    }

    return () => {
      target.removeEventListener('dragover', handleDragOver);
      target.removeEventListener('drop', handleDrop);
      if (isLocalTarget) {
        document.removeEventListener('dragover', blockBrowserOpen);
        document.removeEventListener('drop', blockBrowserOpen);
      }
    };
  }, [targetRef, disabled, onDrop]);
}
