'use client';

import { useEffect, useRef, useState } from 'react';
import { create } from 'zustand';
import { api, API_BASE } from '@/lib/api/client';
import { APIError, humanMessage } from '@/lib/api/errors';
import {
  AssetSchema,
  UploadSessionSchema,
  type Asset,
  type UploadSession,
} from '@/lib/api/schemas';

const DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024;
const PARALLELISM = 3;
const MAX_RETRIES_PER_CHUNK = 3;
const STORAGE_KEY_PREFIX = 'reelforge:upload:';
// Mirrors the server's max_upload_gb (5 GB) so oversized files fail fast with
// a useful message instead of a 413 after the session handshake.
export const MAX_UPLOAD_BYTES = 5 * 1024 ** 3;

// Browsers derive File.type from the OS extension registry, and it comes back
// empty (or application/octet-stream) for plenty of real video files —
// camera-card footage, uncommon containers, files copied off network shares.
// Extension is the reliable signal; ffprobe on the server is the real gate.
const VIDEO_EXTENSIONS = new Set([
  'mp4', 'm4v', 'mov', 'webm', 'mkv', 'avi', 'mpg', 'mpeg', 'm2v',
  'wmv', 'flv', '3gp', '3g2', 'mts', 'm2ts', 'ts', 'mxf', 'ogv',
  '360', 'insv', 'lrv',
]);

// Photos become still shots inside a reel. HEIC/HEIF are deliberately absent:
// the bundled FFmpeg can't decode them, so they're caught early with advice
// rather than failing after a full upload.
const PHOTO_EXTENSIONS = new Set([
  'jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'tif', 'tiff',
]);
const UNSUPPORTED_PHOTO_EXTENSIONS = new Set(['heic', 'heif']);

export function fileExtension(name: string): string {
  const i = name.lastIndexOf('.');
  return i < 0 ? '' : name.slice(i + 1).toLowerCase();
}

export function looksLikeVideo(file: File): boolean {
  if (file.type.startsWith('video/')) return true;
  return VIDEO_EXTENSIONS.has(fileExtension(file.name));
}

export function looksLikePhoto(file: File): boolean {
  const ext = fileExtension(file.name);
  if (UNSUPPORTED_PHOTO_EXTENSIONS.has(ext)) return false;
  if (file.type.startsWith('image/') && !file.type.includes('hei')) return true;
  return PHOTO_EXTENSIONS.has(ext);
}

export function isUnsupportedPhoto(file: File): boolean {
  return (
    UNSUPPORTED_PHOTO_EXTENSIONS.has(fileExtension(file.name)) ||
    file.type.includes('hei')
  );
}

/** Content-type to advertise to the API, which requires video/* or image/*. */
function uploadContentType(file: File): string {
  if (file.type.startsWith('video/') || file.type.startsWith('image/')) {
    return file.type;
  }
  return PHOTO_EXTENSIONS.has(fileExtension(file.name))
    ? 'image/jpeg'
    : 'video/mp4';
}

type ChunkState = 'idle' | 'uploading' | 'done' | 'failed';

/** One file's result within a multi-file batch. */
export interface UploadOutcome {
  name: string;
  assetId?: string;
  /** Set when the file was skipped (rejected up front, or cancelled). */
  error?: string;
}

/** Why a file can't be uploaded at all, or null when it looks fine. The server
 * (ffprobe) is still the real gate; this catches the obvious cases before a
 * multi-GB transfer. */
export function validateFile(file: File): APIError | null {
  if (isUnsupportedPhoto(file)) {
    return new APIError(
      'UPLOAD_UNSUPPORTED_TYPE',
      400,
      `“${file.name}” is an Apple HEIC photo, which can't be decoded yet. ` +
        `In Photos use File → Export → Export Photo and choose JPEG, or ` +
        `switch Settings → Camera → Formats to “Most Compatible”.`,
    );
  }
  if (!looksLikeVideo(file) && !looksLikePhoto(file)) {
    return new APIError(
      'UPLOAD_UNSUPPORTED_TYPE',
      400,
      `“${file.name}” doesn't look like a video or photo (type ${
        file.type || 'unknown'
      }). Video: MP4, MOV, M4V, WebM, MKV, AVI, MTS/M2TS. ` +
        `Photos: JPEG, PNG, WebP, GIF, TIFF.`,
    );
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return new APIError(
      'UPLOAD_TOO_LARGE',
      413,
      `“${file.name}” is ${(file.size / 1024 ** 3).toFixed(1)} GB — the ` +
        `limit is 5 GB. Trim the clip first, or raise MAX_UPLOAD_GB in .env.`,
    );
  }
  if (file.size === 0) {
    return new APIError(
      'UPLOAD_UNSUPPORTED_TYPE',
      400,
      `“${file.name}” is empty (0 bytes). If it lives on a camera card or ` +
        `cloud drive, copy it to local disk first.`,
    );
  }
  return null;
}

export interface UploaderStatusSnapshot {
  status:
    | 'idle'
    | 'creatingSession'
    | 'uploading'
    | 'pausing'
    | 'paused'
    | 'resuming'
    | 'completing'
    | 'done'
    | 'failed';
  file: File | null;
  uploadId: string | null;
  totalBytes: number;
  bytesUploaded: number;
  progress: number;           // 0..1
  speedBps: number;
  etaSeconds: number | null;
  chunkSize: number;
  chunkCount: number;
  chunkStates: Record<number, ChunkState>;
  error: APIError | null;
  asset: Asset | null;
  // Multi-file uploads: files wait here and go one at a time through the
  // single-upload machinery above (sessions, chunk pool, resume).
  queue: File[];
  /** Files in the current batch, accepted + rejected. */
  batchTotal: number;
  finished: UploadOutcome[];
}

interface InternalState extends UploaderStatusSnapshot {
  pauseRequested: boolean;
  abortControllers: Record<number, AbortController>;
  // Lives in the shared store (not a hook ref): two mounted hook instances
  // must see the same "a chunk pool is running" flag or they can double-run
  // concurrent pools against the same session.
  running: boolean;
  set: (partial: Partial<InternalState>) => void;
  setChunk: (idx: number, state: ChunkState) => void;
  reset: () => void;
}

const initial = (): InternalState => ({
  status: 'idle',
  file: null,
  uploadId: null,
  totalBytes: 0,
  bytesUploaded: 0,
  progress: 0,
  speedBps: 0,
  etaSeconds: null,
  chunkSize: DEFAULT_CHUNK_BYTES,
  chunkCount: 0,
  chunkStates: {},
  error: null,
  asset: null,
  queue: [],
  batchTotal: 0,
  finished: [],
  pauseRequested: false,
  abortControllers: {},
  running: false,
  set: () => {},
  setChunk: () => {},
  reset: () => {},
});

function makeStore() {
  return create<InternalState>((set) => {
    const base = initial();
    return {
      ...base,
      set: (partial) => set((s) => ({ ...s, ...partial })),
      setChunk: (idx, state) =>
        set((s) => ({ chunkStates: { ...s.chunkStates, [idx]: state } })),
      reset: () =>
        set(() => ({
          ...initial(),
          set: base.set,
          setChunk: base.setChunk,
          reset: base.reset,
        })),
    };
  });
}

const stores = new Map<string, ReturnType<typeof makeStore>>();

function storeFor(projectId: string) {
  if (!stores.has(projectId)) stores.set(projectId, makeStore());
  return stores.get(projectId)!;
}

/** Return a project's uploader to a clean slate from outside the hook.
 *
 * The store is module-level so it survives the panel unmounting, which means
 * a finished/failed upload can otherwise leave the next one stuck. Callers
 * that (re)open the uploader use this instead of relying on mount-time state
 * heuristics. */
export function resetUploaderStore(projectId: string): void {
  const store = stores.get(projectId);
  if (!store) return;
  const snap = store.getState();
  if (snap.status === 'uploading' || snap.status === 'creatingSession') return;
  for (const ac of Object.values(snap.abortControllers)) ac.abort();
  snap.reset();
}

const ACTIVE_STATUSES = new Set([
  'creatingSession',
  'uploading',
  'pausing',
  'paused',
  'resuming',
  'completing',
  'failed',
]);

/** Whether the project's uploader has something the user must still see: a
 * batch in flight or queued, a failed upload awaiting retry, or reasons for
 * skipped files. Pages use it to keep the uploader mounted — hiding it once
 * the project has its first clip used to vanish the panel mid-batch. */
export function useUploaderActive(projectId: string): boolean {
  return storeFor(projectId)(
    (s) =>
      s.running ||
      s.queue.length > 0 ||
      ACTIVE_STATUSES.has(s.status) ||
      s.finished.some((o) => o.error),
  );
}

// ---------- helpers ----------

function storageKey(projectId: string) {
  return `${STORAGE_KEY_PREFIX}${projectId}`;
}

function persistSession(projectId: string, uploadId: string, filename: string) {
  try {
    localStorage.setItem(
      storageKey(projectId),
      JSON.stringify({ uploadId, filename, savedAt: Date.now() }),
    );
  } catch {
    /* storage disabled; non-fatal */
  }
}

function readPersistedSession(projectId: string): {
  uploadId: string;
  filename: string;
} | null {
  try {
    const raw = localStorage.getItem(storageKey(projectId));
    if (!raw) return null;
    const data = JSON.parse(raw) as { uploadId: string; filename: string };
    if (!data.uploadId) return null;
    return data;
  } catch {
    return null;
  }
}

function clearPersistedSession(projectId: string) {
  try {
    localStorage.removeItem(storageKey(projectId));
  } catch {
    /* noop */
  }
}

function chunkBoundaries(
  total: number,
  chunkSize: number,
): Array<{ index: number; start: number; end: number }> {
  const out: Array<{ index: number; start: number; end: number }> = [];
  let idx = 0;
  for (let start = 0; start < total; start += chunkSize) {
    const end = Math.min(total, start + chunkSize);
    out.push({ index: idx, start, end });
    idx += 1;
  }
  return out;
}

async function putChunk(
  uploadId: string,
  index: number,
  body: ArrayBuffer,
  signal: AbortSignal,
): Promise<UploadSession> {
  const resp = await fetch(
    `${API_BASE}/api/v1/uploads/${uploadId}/chunks/${index}`,
    {
      method: 'PUT',
      headers: { 'content-length': String(body.byteLength) },
      body,
      signal,
    },
  );
  if (!resp.ok) {
    let payload: unknown = null;
    try {
      payload = await resp.json();
    } catch {
      /* ignore */
    }
    if (payload && typeof payload === 'object' && 'error' in (payload as Record<string, unknown>)) {
      const envelope = payload as {
        error: { code: string; message: string; details?: Record<string, unknown> };
      };
      throw new APIError(
        envelope.error.code,
        resp.status,
        envelope.error.message,
        envelope.error.details,
      );
    }
    throw new APIError('INTERNAL_ERROR', resp.status, `chunk upload failed`);
  }
  const parsed = UploadSessionSchema.parse(await resp.json());
  return parsed;
}

// ---------- the hook ----------

export function useUploader(projectId: string) {
  const store = storeFor(projectId);
  const s = store();
  const windowRef = useRef<Array<{ t: number; bytes: number }>>([]);

  // Crash-recovery: on mount, check for a persisted session and show it.
  useEffect(() => {
    const persisted = readPersistedSession(projectId);
    if (!persisted) return;
    let cancelled = false;
    // Probe the server for its status.
    (async () => {
      try {
        const session = await api<UploadSession>(`/uploads/${persisted.uploadId}`, {
          schema: UploadSessionSchema,
        });
        if (cancelled) return;
        if (session.status !== 'active') {
          clearPersistedSession(projectId);
          return;
        }
        // Only surface the recovered session if nothing newer is in flight.
        if (store.getState().status !== 'idle') return;
        store.setState({
          status: 'paused',
          uploadId: session.id,
          totalBytes: session.total_bytes,
          chunkSize: session.chunk_size,
          chunkCount: Math.ceil(session.total_bytes / session.chunk_size),
          bytesUploaded: session.received_bytes,
          progress: session.total_bytes ? session.received_bytes / session.total_bytes : 0,
        });
      } catch {
        if (!cancelled) clearPersistedSession(projectId);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  function selectFile(file: File) {
    const invalid = validateFile(file);
    if (invalid) {
      store.setState({ status: 'failed', error: invalid });
      return;
    }
    windowRef.current = [];
    store.setState({
      status: 'idle',
      file,
      error: null,
      totalBytes: file.size,
      bytesUploaded: 0,
      progress: 0,
      chunkSize: DEFAULT_CHUNK_BYTES,
      chunkCount: Math.ceil(file.size / DEFAULT_CHUNK_BYTES),
      chunkStates: {},
      pauseRequested: false,
      asset: null,
      uploadId: null,
    });
  }

  async function start() {
    const snap = store.getState();
    if (!snap.file) return;
    if (snap.running) return;
    store.setState({ running: true, status: 'creatingSession', error: null });
    try {
      const session = await api<UploadSession>(
        `/projects/${projectId}/uploads`,
        {
          method: 'POST',
          body: {
            filename: snap.file.name,
            content_type: uploadContentType(snap.file),
            total_bytes: snap.file.size,
            chunk_size: DEFAULT_CHUNK_BYTES,
          },
          schema: UploadSessionSchema,
        },
      );
      persistSession(projectId, session.id, snap.file.name);
      store.setState({
        status: 'uploading',
        uploadId: session.id,
        totalBytes: session.total_bytes,
        chunkSize: session.chunk_size,
        chunkCount: Math.ceil(session.total_bytes / session.chunk_size),
        bytesUploaded: session.received_bytes,
      });
      await runChunkPool(session.received_chunk_indices);
    } catch (err) {
      const apiErr = err instanceof APIError ? err : new APIError('NETWORK_ERROR', 0, String(err));
      if (!store.getState().uploadId) {
        // Failed before any bytes went up (the server refused the session, or
        // the network dropped): nothing to resume, so record it as skipped and
        // keep the batch moving instead of stalling the queue.
        store.setState((state) => ({
          running: false,
          status: 'idle',
          file: null,
          error: null,
          finished: [...state.finished, { name: snap.file!.name, error: humanMessage(apiErr) }],
        }));
        advance();
        return;
      }
      store.setState({
        running: false,
        status: 'failed',
        error: apiErr,
      });
    }
  }

  async function resume() {
    const snap = store.getState();
    if (!snap.uploadId || !snap.file) {
      // Resume with no file: the user picked "Resume upload" on reload but
      // hasn't re-selected the file yet. That's unrecoverable without the
      // bytes — tell them to pick the same file.
      store.setState({
        status: 'failed',
        error: new APIError(
          'UPLOAD_SESSION_NOT_FOUND',
          409,
          'Re-select the file to continue this upload.',
        ),
      });
      return;
    }
    if (snap.running) return;
    store.setState({ running: true, status: 'resuming', pauseRequested: false, error: null });
    try {
      // Pull canonical state — including exactly which chunks the server has.
      const session = await api<UploadSession>(`/uploads/${snap.uploadId}`, {
        schema: UploadSessionSchema,
      });
      if (session.status === 'completed') {
        store.setState({ status: 'completing' });
        await completeSession(session.id);
        return;
      }
      store.setState({
        status: 'uploading',
        bytesUploaded: session.received_bytes,
        progress: session.total_bytes ? session.received_bytes / session.total_bytes : 0,
      });
      await runChunkPool(session.received_chunk_indices);
    } catch (err) {
      store.setState({
        running: false,
        status: 'failed',
        error: err instanceof APIError ? err : new APIError('NETWORK_ERROR', 0, String(err)),
      });
    }
  }

  /** Re-attach the user's file to a persisted (paused) session and continue.
   * Returns false when the picked file can't belong to the session. */
  async function attachAndResume(file: File): Promise<boolean> {
    const snap = store.getState();
    if (!snap.uploadId || snap.file) return false;
    if (file.size !== snap.totalBytes) return false;
    store.setState({ file, error: null });
    await resume();
    return true;
  }

  async function pause() {
    const snap = store.getState();
    if (snap.status !== 'uploading') return;
    store.setState({ status: 'pausing', pauseRequested: true });
    for (const ac of Object.values(snap.abortControllers)) {
      ac.abort();
    }
  }

  /** True while a file is mid-transfer (or paused / waiting on a retry) —
   * new files queue behind it instead of starting. */
  function busy(): boolean {
    const snap = store.getState();
    if (snap.running) return true;
    if (['creatingSession', 'uploading', 'pausing', 'paused', 'resuming', 'completing'].includes(snap.status)) {
      return true;
    }
    return snap.status === 'failed' && !!snap.uploadId;
  }

  /** Start the next queued file, if nothing is in flight. */
  function advance(): boolean {
    const snap = store.getState();
    if (busy() || snap.queue.length === 0) return false;
    const [next, ...rest] = snap.queue;
    store.setState({ queue: rest });
    selectFile(next);
    void start();
    return true;
  }

  /** Add files to the batch. Files that can't be uploaded are recorded as
   * skipped (with the reason) so one bad file never blocks the rest. */
  function enqueue(files: File[]) {
    if (files.length === 0) return;
    const snap = store.getState();
    const startingFresh = !busy() && snap.queue.length === 0;
    const accepted: File[] = [];
    const rejected: UploadOutcome[] = [];
    for (const f of files) {
      const err = validateFile(f);
      if (err) rejected.push({ name: f.name, error: err.message });
      else accepted.push(f);
    }
    store.setState({
      queue: [...snap.queue, ...accepted],
      // A new batch after a finished one starts its tally from zero.
      finished: startingFresh && snap.status !== 'uploading' ? rejected : [...snap.finished, ...rejected],
      batchTotal: (startingFresh ? 0 : snap.batchTotal) + files.length,
      ...(startingFresh ? { status: 'idle' as const, error: null, asset: null } : {}),
    });
    advance();
  }

  /** Cancel the current file; the rest of the batch carries on. */
  async function cancel() {
    const snap = store.getState();
    for (const ac of Object.values(snap.abortControllers)) ac.abort();
    if (snap.uploadId) {
      try {
        await api(`/uploads/${snap.uploadId}`, { method: 'DELETE' });
      } catch {
        /* ignore */
      }
    }
    clearPersistedSession(projectId);
    const inBatch = snap.batchTotal > 1 || snap.queue.length > 0;
    const keep = inBatch
      ? {
          queue: snap.queue,
          batchTotal: snap.batchTotal,
          finished: snap.file
            ? [...snap.finished, { name: snap.file.name, error: 'Cancelled' }]
            : snap.finished,
        }
      : null;
    store.getState().reset();
    windowRef.current = [];
    if (keep) {
      store.setState(keep);
      advance();
    }
  }

  /** Cancel the current file and drop everything still queued. */
  async function cancelAll() {
    store.setState({ queue: [] });
    await cancel();
    store.getState().reset();
  }

  function retry() {
    const snap = store.getState();
    if (snap.uploadId) {
      void resume();
    } else if (snap.file) {
      void start();
    }
  }

  async function runChunkPool(receivedIndices: number[]) {
    const snap = store.getState();
    if (!snap.file || !snap.uploadId) return;
    const file = snap.file;
    const allChunks = chunkBoundaries(snap.totalBytes, snap.chunkSize);

    // Skip exactly the chunks the server reports on disk. (Chunks upload in
    // parallel, so "first floor(bytes/chunkSize) chunks" is wrong — chunk 1
    // can be missing while 0 and 2 landed.)
    const receivedSet = new Set(receivedIndices);
    const pending = allChunks.filter((c) => !receivedSet.has(c.index));

    // Seed chunk-states from the server's view.
    const seeded: Record<number, ChunkState> = {};
    for (const c of allChunks) {
      seeded[c.index] = receivedSet.has(c.index) ? 'done' : 'idle';
    }
    store.setState({ chunkStates: seeded });

    let nextIdx = 0;
    async function worker() {
      // Each worker claims chunks from `pending` in order until none are left
      // or we're asked to pause.
      while (true) {
        const i = nextIdx;
        nextIdx += 1;
        const chunk = pending[i];
        if (!chunk) return;
        if (store.getState().pauseRequested) return;
        await uploadOneWithRetry(chunk.index, chunk.start, chunk.end);
      }
    }

    async function uploadOneWithRetry(idx: number, start: number, end: number) {
      for (let attempt = 0; attempt < MAX_RETRIES_PER_CHUNK; attempt += 1) {
        if (store.getState().pauseRequested) return;
        const controller = new AbortController();
        store.setState((state) => ({
          abortControllers: { ...state.abortControllers, [idx]: controller },
          chunkStates: { ...state.chunkStates, [idx]: 'uploading' },
        }));
        try {
          const slice = file.slice(start, end);
          const buf = await slice.arrayBuffer();
          const session = await putChunk(snap.uploadId!, idx, buf, controller.signal);
          store.setState((state) => {
            const nextStates = { ...state.chunkStates, [idx]: 'done' as ChunkState };
            const nextAcs = { ...state.abortControllers };
            delete nextAcs[idx];
            const nowBytes = session.received_bytes;
            // Update a 5s rolling speed window
            windowRef.current.push({ t: Date.now(), bytes: nowBytes });
            const cutoff = Date.now() - 5000;
            while (windowRef.current.length > 1 && windowRef.current[0].t < cutoff) {
              windowRef.current.shift();
            }
            let speed = 0;
            let eta: number | null = null;
            if (windowRef.current.length >= 2) {
              const first = windowRef.current[0];
              const last = windowRef.current[windowRef.current.length - 1];
              const dt = (last.t - first.t) / 1000;
              if (dt > 0.05) {
                speed = (last.bytes - first.bytes) / dt;
                if (speed > 0) eta = (state.totalBytes - nowBytes) / speed;
              }
            }
            return {
              chunkStates: nextStates,
              abortControllers: nextAcs,
              bytesUploaded: nowBytes,
              progress: state.totalBytes ? nowBytes / state.totalBytes : 0,
              speedBps: speed,
              etaSeconds: eta,
            };
          });
          return;
        } catch (err) {
          const aborted =
            err instanceof Error && (err.name === 'AbortError' || err.message.includes('aborted'));
          if (aborted) {
            store.setState((state) => ({
              chunkStates: { ...state.chunkStates, [idx]: 'idle' },
            }));
            return;
          }
          const apiErr = err instanceof APIError ? err : new APIError('NETWORK_ERROR', 0, String(err));
          if (attempt + 1 < MAX_RETRIES_PER_CHUNK) {
            await new Promise((r) => setTimeout(r, 500 * 2 ** attempt));
            continue;
          }
          store.setState((state) => ({
            chunkStates: { ...state.chunkStates, [idx]: 'failed' },
            status: 'failed',
            error: apiErr,
          }));
          return;
        }
      }
    }

    const workerCount = Math.min(PARALLELISM, pending.length);
    await Promise.all(Array.from({ length: workerCount }, () => worker()));

    store.setState({ running: false });
    const latest = store.getState();
    if (latest.status === 'failed') return;
    if (latest.pauseRequested) {
      store.setState({ status: 'paused', pauseRequested: false });
      return;
    }
    if (latest.bytesUploaded >= latest.totalBytes) {
      await completeSession(latest.uploadId!);
    }
  }

  async function completeSession(uploadId: string) {
    store.setState({ status: 'completing' });
    try {
      const asset = await api<Asset>(`/uploads/${uploadId}/complete`, {
        method: 'POST',
        schema: AssetSchema,
      });
      clearPersistedSession(projectId);
      store.setState((state) => ({
        status: 'done',
        asset,
        running: false,
        finished: [...state.finished, { name: asset.original_filename, assetId: asset.id }],
      }));
      // Next file in the batch, if any.
      advance();
    } catch (err) {
      store.setState({
        status: 'failed',
        running: false,
        error: err instanceof APIError ? err : new APIError('NETWORK_ERROR', 0, String(err)),
      });
    }
  }

  function reset() {
    // Abort anything in flight, then return the store to a clean idle state
    // so the dropzone renders again for the next upload.
    const snap = store.getState();
    for (const ac of Object.values(snap.abortControllers)) ac.abort();
    store.getState().reset();
    windowRef.current = [];
  }

  return {
    state: s,
    actions: {
      selectFile,
      start,
      enqueue,
      pause,
      resume,
      attachAndResume,
      cancel,
      cancelAll,
      retry,
      reset,
    },
  };
}
