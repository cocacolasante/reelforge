'use client';

import { useEffect, useRef, useState } from 'react';
import { create } from 'zustand';
import { api, API_BASE } from '@/lib/api/client';
import { APIError } from '@/lib/api/errors';
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

type ChunkState = 'idle' | 'uploading' | 'done' | 'failed';

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
}

interface InternalState extends UploaderStatusSnapshot {
  pauseRequested: boolean;
  abortControllers: Record<number, AbortController>;
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
  pauseRequested: false,
  abortControllers: {},
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
  const runningRef = useRef(false);
  const windowRef = useRef<Array<{ t: number; bytes: number }>>([]);

  // Crash-recovery: on mount, check for a persisted session and show it.
  useEffect(() => {
    const persisted = readPersistedSession(projectId);
    if (!persisted) return;
    // Probe the server for its status.
    (async () => {
      try {
        const session = await api<UploadSession>(`/uploads/${persisted.uploadId}`, {
          schema: UploadSessionSchema,
        });
        if (session.status !== 'active') {
          clearPersistedSession(projectId);
          return;
        }
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
        clearPersistedSession(projectId);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  function selectFile(file: File) {
    if (!file.type.startsWith('video/')) {
      store.setState({
        status: 'failed',
        error: new APIError(
          'UPLOAD_UNSUPPORTED_TYPE',
          400,
          `Selected file has type ${file.type || 'unknown'}; only video files are supported.`,
        ),
      });
      return;
    }
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
    if (runningRef.current) return;
    runningRef.current = true;
    store.setState({ status: 'creatingSession', error: null });
    try {
      const session = await api<UploadSession>(
        `/projects/${projectId}/uploads`,
        {
          method: 'POST',
          body: {
            filename: snap.file.name,
            content_type: snap.file.type || 'video/mp4',
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
      await runChunkPool();
    } catch (err) {
      runningRef.current = false;
      store.setState({
        status: 'failed',
        error: err instanceof APIError ? err : new APIError('NETWORK_ERROR', 0, String(err)),
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
    if (runningRef.current) return;
    runningRef.current = true;
    store.setState({ status: 'resuming', pauseRequested: false, error: null });
    try {
      // Pull canonical state.
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
      await runChunkPool();
    } catch (err) {
      runningRef.current = false;
      store.setState({
        status: 'failed',
        error: err instanceof APIError ? err : new APIError('NETWORK_ERROR', 0, String(err)),
      });
    }
  }

  async function pause() {
    const snap = store.getState();
    if (snap.status !== 'uploading') return;
    store.setState({ status: 'pausing', pauseRequested: true });
    for (const ac of Object.values(snap.abortControllers)) {
      ac.abort();
    }
  }

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
    store.getState().reset();
    runningRef.current = false;
  }

  function retry() {
    const snap = store.getState();
    if (snap.uploadId) {
      void resume();
    } else if (snap.file) {
      void start();
    }
  }

  async function runChunkPool() {
    const snap = store.getState();
    if (!snap.file || !snap.uploadId) return;
    const file = snap.file;
    const allChunks = chunkBoundaries(snap.totalBytes, snap.chunkSize);
    const chunkSessionState: UploadSession = {
      id: snap.uploadId,
      project_id: projectId,
      filename: file.name,
      content_type: file.type || 'video/mp4',
      total_bytes: snap.totalBytes,
      received_bytes: snap.bytesUploaded,
      chunk_size: snap.chunkSize,
      status: 'active',
      asset_id: null,
      created_at: '',
    };

    // Ask the server which chunks are already present (by recomputing the
    // server's received_bytes; for an uninterrupted first run this is 0).
    // If we're resuming and the server has some bytes, we skip the first N
    // full-chunk-sized bytes' worth of chunks.
    const alreadyBytes = chunkSessionState.received_bytes;
    const skipBeforeIndex = Math.floor(alreadyBytes / snap.chunkSize);
    const pending = allChunks.filter((c) => c.index >= skipBeforeIndex);

    // Seed chunk-states
    const seeded: Record<number, ChunkState> = { ...snap.chunkStates };
    for (const c of allChunks) {
      seeded[c.index] = seeded[c.index] ?? (c.index < skipBeforeIndex ? 'done' : 'idle');
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

    runningRef.current = false;
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
      store.setState({ status: 'done', asset });
    } catch (err) {
      store.setState({
        status: 'failed',
        error: err instanceof APIError ? err : new APIError('NETWORK_ERROR', 0, String(err)),
      });
    }
  }

  return {
    state: s,
    actions: {
      selectFile,
      start,
      pause,
      resume,
      cancel,
      retry,
    },
  };
}
