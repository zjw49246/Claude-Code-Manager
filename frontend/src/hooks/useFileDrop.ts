import { useEffect, useCallback, type RefObject } from 'react';

const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB
const MAX_FILES = 10;

interface UseFileDropOptions {
  targetRef?: RefObject<HTMLElement | null>;
  pendingFiles: File[];
  setPendingFiles: React.Dispatch<React.SetStateAction<File[]>>;
  setFilePreviews: React.Dispatch<React.SetStateAction<string[]>>;
  imageExts?: string[];
  disabled?: boolean;
  onError?: (message: string) => void;
}

export function useFileDrop({
  targetRef,
  pendingFiles,
  setPendingFiles,
  setFilePreviews,
  imageExts = ['.png', '.jpg', '.jpeg', '.gif', '.webp'],
  disabled = false,
  onError,
}: UseFileDropOptions) {
  const isImageFile = useCallback(
    (f: File) => imageExts.some((ext) => f.name.toLowerCase().endsWith(ext)),
    [imageExts],
  );

  const addFiles = useCallback(
    (incoming: File[]) => {
      const oversized = incoming.filter((f) => f.size > MAX_FILE_SIZE);
      const valid = incoming.filter((f) => f.size <= MAX_FILE_SIZE);

      if (oversized.length > 0) {
        const names = oversized.map((f) => f.name).join(', ');
        onError?.(
          oversized.length === 1
            ? `File "${names}" exceeds 50MB limit`
            : `${oversized.length} files exceed 10MB limit: ${names}`,
        );
      }

      if (valid.length === 0) return;

      const slots = MAX_FILES - pendingFiles.length;
      if (slots <= 0) {
        onError?.(`Maximum ${MAX_FILES} files allowed`);
        return;
      }

      if (valid.length > slots) {
        onError?.(`Only ${slots} more file${slots === 1 ? '' : 's'} can be added (max ${MAX_FILES})`);
      }

      const accepted = valid.slice(0, slots);
      const combined = [...pendingFiles, ...accepted];
      setPendingFiles(combined);
      setFilePreviews(combined.map((f) => (isImageFile(f) ? URL.createObjectURL(f) : '')));
    },
    [pendingFiles, setPendingFiles, setFilePreviews, isImageFile, onError],
  );

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
      addFiles(Array.from(dt.files));
    };

    target.addEventListener('dragover', handleDragOver);
    target.addEventListener('drop', handleDrop);

    // When listening on a local element (not document), also block
    // the browser's default file-open behavior on the rest of the page.
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
  }, [targetRef, disabled, addFiles]);

  return { addFiles };
}
