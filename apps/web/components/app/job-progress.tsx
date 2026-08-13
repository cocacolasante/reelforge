'use client';

import * as React from 'react';
import { AlertTriangle, CheckCircle2, Clock, Loader2 } from 'lucide-react';
import { useJobStream } from '@/lib/sse/job-stream';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Progress } from '@/components/ui/progress';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

export type JobVariant = 'analyze' | 'select' | 'compose' | 'export' | 'publish';

const ANALYZE_STAGES = ['probe', 'scenes', 'transcribe', 'loudness', 'semantics'];
const COMPOSE_STAGES = ['prepare', 'clips', 'captions', 'music', 'render', 'finalize'];

export function JobProgress({
  jobId,
  variant,
  onDone,
  onFail,
}: {
  jobId: string | null;
  variant: JobVariant;
  onDone?: (result: unknown) => void;
  onFail?: (error: string) => void;
}) {
  const live = useJobStream(jobId);

  React.useEffect(() => {
    if (live.status === 'done') onDone?.(live.result);
    if (live.status === 'failed') onFail?.(live.error ?? 'unknown error');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live.status]);

  if (!jobId) return null;

  if (live.status === 'failed') {
    return (
      <Alert variant="destructive">
        <AlertTriangle className="h-4 w-4" />
        <AlertTitle>Job failed</AlertTitle>
        <AlertDescription>
          {live.error ?? 'The job reported an unknown error.'}
        </AlertDescription>
      </Alert>
    );
  }

  const stages =
    variant === 'analyze' ? ANALYZE_STAGES : variant === 'compose' ? COMPOSE_STAGES : null;

  return (
    <div className="space-y-3 rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {live.status === 'done' ? (
            <CheckCircle2 className="h-4 w-4 text-emerald-400" />
          ) : (
            <Loader2 className="h-4 w-4 animate-spin text-primary" />
          )}
          <span className="text-sm font-medium capitalize">
            {live.stage ?? live.status}
          </span>
        </div>
        <Badge variant="secondary">{Math.round((live.progress ?? 0) * 100)}%</Badge>
      </div>
      <Progress value={Math.round((live.progress ?? 0) * 100)} />
      {stages ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {stages.map((s) => {
            const idx = stages.indexOf(live.stage ?? '');
            const myIdx = stages.indexOf(s);
            let state: 'pending' | 'active' | 'done';
            if (live.status === 'done') state = 'done';
            else if (idx < 0) state = 'pending';
            else if (myIdx < idx) state = 'done';
            else if (myIdx === idx) state = 'active';
            else state = 'pending';
            return (
              <span
                key={s}
                className={cn(
                  'flex items-center gap-1 rounded-full border px-2 py-0.5',
                  state === 'done' && 'border-emerald-400/40 text-emerald-400',
                  state === 'active' && 'border-primary/50 text-primary',
                  state === 'pending' && 'border-border text-muted-foreground',
                )}
              >
                {state === 'done' ? (
                  <CheckCircle2 className="h-3 w-3" />
                ) : state === 'active' ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Clock className="h-3 w-3" />
                )}
                {s}
              </span>
            );
          })}
        </div>
      ) : null}
      {live.message ? (
        <p aria-live="polite" className="text-xs text-muted-foreground">
          {live.message}
        </p>
      ) : null}
      {live.fellBackToPolling ? (
        <p className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
          polling mode
        </p>
      ) : null}
    </div>
  );
}
