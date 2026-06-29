import { useState, useCallback, useRef } from 'react';
import { api, type UploadResult } from '../api/client';

export interface UploadEntry {
  id: string;
  file: File;
  preview: string;
  status: 'uploading' | 'uploaded' | 'failed';
  result?: UploadResult;
  error?: string;
}

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const MAX_FILES = 10;
const BLOCKED_EXTENSIONS = new Set(['.exe']);
const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
const isImageFile = (name: string) => IMAGE_EXTS.some(ext => name.toLowerCase().endsWith(ext));

export function useFileUpload() {
  const [uploads, setUploads] = useState<UploadEntry[]>([]);
  const uploadsRef = useRef<UploadEntry[]>([]);
  uploadsRef.current = uploads;

  const doUpload = useCallback(async (entry: UploadEntry) => {
    try {
      const results = await api.uploadImages([entry.file]);
      setUploads(prev => prev.map(u =>
        u.id === entry.id ? { ...u, status: 'uploaded' as const, result: results[0] } : u
      ));
    } catch (e) {
      setUploads(prev => prev.map(u =>
        u.id === entry.id ? { ...u, status: 'failed' as const, error: e instanceof Error ? e.message : String(e) } : u
      ));
    }
  }, []);

  const addFiles = useCallback((incoming: File[], onError?: (msg: string) => void) => {
    const blocked = incoming.filter(f => {
      const ext = f.name.toLowerCase().slice(f.name.lastIndexOf('.'));
      return BLOCKED_EXTENSIONS.has(ext);
    });
    if (blocked.length > 0) {
      onError?.(`File type not allowed: ${blocked.map(f => f.name).join(', ')}`);
    }
    const allowed = incoming.filter(f => {
      const ext = f.name.toLowerCase().slice(f.name.lastIndexOf('.'));
      return !BLOCKED_EXTENSIONS.has(ext);
    });
    const oversized = allowed.filter(f => f.size > MAX_FILE_SIZE);
    if (oversized.length > 0) {
      onError?.(oversized.length === 1
        ? `File "${oversized[0].name}" exceeds 50MB limit`
        : `${oversized.length} files exceed 50MB limit`);
    }
    const valid = allowed.filter(f => f.size <= MAX_FILE_SIZE);
    if (valid.length === 0) return;

    const slots = MAX_FILES - uploadsRef.current.length;
    if (slots <= 0) {
      onError?.(`Maximum ${MAX_FILES} files allowed`);
      return;
    }
    const accepted = valid.slice(0, slots);

    const newEntries: UploadEntry[] = accepted.map(f => ({
      id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
      file: f,
      preview: isImageFile(f.name) ? URL.createObjectURL(f) : '',
      status: 'uploading' as const,
    }));

    setUploads(prev => [...prev, ...newEntries]);
    newEntries.forEach(entry => doUpload(entry));
  }, [doUpload]);

  const removeFile = useCallback((id: string) => {
    setUploads(prev => {
      const removed = prev.find(u => u.id === id);
      if (removed?.preview) URL.revokeObjectURL(removed.preview);
      return prev.filter(u => u.id !== id);
    });
  }, []);

  const retryFile = useCallback((id: string) => {
    const entry = uploadsRef.current.find(u => u.id === id);
    if (!entry || entry.status !== 'failed') return;
    const updated = { ...entry, status: 'uploading' as const, error: undefined };
    setUploads(prev => prev.map(u => u.id === id ? updated : u));
    doUpload(updated);
  }, [doUpload]);

  const clear = useCallback(() => {
    setUploads(prev => {
      prev.forEach(u => { if (u.preview) URL.revokeObjectURL(u.preview); });
      return [];
    });
  }, []);

  return {
    uploads,
    addFiles,
    removeFile,
    retryFile,
    clear,
    uploadedResults: uploads.filter(u => u.status === 'uploaded').map(u => u.result!),
    isUploading: uploads.some(u => u.status === 'uploading'),
    allDone: uploads.length > 0 && uploads.every(u => u.status !== 'uploading'),
  };
}
