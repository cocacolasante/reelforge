'use client';

import { useEffect, useRef, useState } from 'react';
import { api, API_BASE } from '@/lib/api/client';
import { JobSchema, type Job } from '@/lib/api/schemas';

export type JobLive = {
  status: 'queued' | 'running' | 'done' | 'failed' | 'unknown';
  progress: number;
  stage: string | null;
  message: string | null;
  result: unknown | null;
  error: string | null;
  fellBackToPolling: boolean;
};

const DEFAULT: JobLive = {
  status: 'unknown',
  progress: 0,
  stage: null,
  message: null,
  result: null,
  error: null,
  fellBackToPolling: false,
};

function fromJob(j: Job): JobLive {
  return {
    status: j.status as JobLive['status'],
    progress: j.progress,
    stage: j.stage,
    message: j.message,
    result: (j.result as unknown) ?? null,
    error: j.error,
    fellBackToPolling: false,
  };
}

export function useJobStream(jobId: string | null | undefined): JobLive {
  const [state, setState] = useState<JobLive>(DEFAULT);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!jobId) {
      setState(DEFAULT);
      return;
    }
    let closed = false;
    let es: EventSource | null = null;

    let warned = false;

    async function pollOnce() {
      try {
        const job = await api<Job>(`/jobs/${jobId}`, { schema: JobSchema });
        setState({ ...fromJob(job), fellBackToPolling: true });
        if (job.status === 'done' || job.status === 'failed') {
          if (pollingRef.current) clearInterval(pollingRef.current);
        }
      } catch {
        /* ignore transient errors */
      }
    }

    function startPolling() {
      if (pollingRef.current || closed) return;
      // Streaming failed — don't keep the broken EventSource around or we'd
      // poll AND stream simultaneously once the connection flaps back.
      es?.close();
      void pollOnce();
      pollingRef.current = setInterval(pollOnce, 1500);
    }

    try {
      es = new EventSource(`${API_BASE}/api/v1/jobs/${jobId}/stream`);
      const parse = (raw: string): Partial<JobLive> => {
        try {
          return JSON.parse(raw) as Partial<JobLive>;
        } catch {
          return {};
        }
      };
      es.addEventListener('progress', (e) => {
        const p = parse((e as MessageEvent).data);
        setState((prev) => ({ ...prev, ...p, fellBackToPolling: false }));
      });
      es.addEventListener('done', (e) => {
        const p = parse((e as MessageEvent).data);
        setState((prev) => ({
          ...prev,
          status: 'done',
          progress: 1,
          result: (p.result as unknown) ?? null,
          error: null,
          fellBackToPolling: false,
        }));
        es?.close();
      });
      es.addEventListener('failed', (e) => {
        const p = parse((e as MessageEvent).data);
        setState((prev) => ({
          ...prev,
          status: 'failed',
          error: (p.error as string) ?? prev.error,
          fellBackToPolling: false,
        }));
        es?.close();
      });
      es.onerror = () => {
        // `state` here is a stale closure — track the warning locally instead.
        if (!warned) {
          warned = true;
          // eslint-disable-next-line no-console
          console.warn('[reelforge] SSE errored; falling back to polling');
        }
        startPolling();
      };
    } catch {
      startPolling();
    }

    return () => {
      closed = true;
      es?.close();
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  return state;
}
