'use client';

import * as React from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Upload, Pause, Play, X, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { useUploader } from '@/lib/upload/uploader';
import { formatBytes, formatSpeed, formatEta } from '@/lib/format';
import { humanMessage } from '@/lib/api/errors';
import { cn } from '@/lib/utils';

interface UploaderProps {
  projectId: string;
  onComplete?: () => void;
}

export function Uploader({ projectId, onComplete }: UploaderProps) {
  const qc = useQueryClient();
  const { state, actions } = useUploader(projectId);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = React.useState(false);
  const lastDoneRef = React.useRef<string | null>(null);

  React.useEffect(() => {
    if (state.status === 'done') {
      qc.invalidateQueries({ queryKey: ['assets', projectId] });
      qc.invalidateQueries({ queryKey: ['project', projectId] });
      const tag = state.asset?.id ?? 'done';
      if (lastDoneRef.current !== tag) {
        lastDoneRef.current = tag;
        onComplete?.();
      }
    }
  }, [state.status, state.asset, projectId, qc, onComplete]);

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) {
      actions.selectFile(f);
      void actions.start();
    }
  };

  const onChoose = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) {
      actions.selectFile(f);
      void actions.start();
    }
  };

  if (state.status === 'done') {
    return (
      <Alert variant="info" className="border-emerald-500/40">
        <CheckCircle2 className="h-4 w-4 text-emerald-400" />
        <AlertTitle>Upload complete</AlertTitle>
        <AlertDescription>
          {state.asset?.original_filename} ·{' '}
          {state.asset ? formatBytes(state.asset.size_bytes) : ''}
        </AlertDescription>
      </Alert>
    );
  }

  const resumable = state.status === 'paused' && !state.file;

  return (
    <div className="space-y-4">
      {state.status === 'idle' || state.status === 'failed' ? (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            'flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-6 py-16 text-center transition-colors',
            dragging ? 'border-primary bg-primary/5' : 'border-border bg-card/40',
          )}
        >
          <Upload className="h-10 w-10 text-muted-foreground" />
          <div className="text-lg font-medium">Drop a video, or click to choose</div>
          <p className="max-w-sm text-sm text-muted-foreground">
            Supported: MP4, MOV, WebM. Up to 5 GB.
          </p>
          <input
            ref={inputRef}
            type="file"
            accept="video/*"
            className="hidden"
            onChange={onChoose}
          />
          <Button
            variant="secondary"
            className="mt-2"
            onClick={() => inputRef.current?.click()}
          >
            Choose file
          </Button>
        </div>
      ) : null}

      {state.error ? (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Upload error</AlertTitle>
          <AlertDescription>{humanMessage(state.error)}</AlertDescription>
          <div className="mt-3 flex gap-2">
            <Button size="sm" onClick={() => actions.retry()}>
              Retry
            </Button>
            <Button size="sm" variant="ghost" onClick={() => actions.cancel()}>
              Cancel
            </Button>
          </div>
        </Alert>
      ) : null}

      {state.status !== 'idle' && state.status !== 'failed' ? (
        <div className="space-y-3 rounded-lg border bg-card p-4">
          <div className="flex items-center justify-between text-sm">
            <span className="font-medium">
              {state.file?.name ??
                (resumable ? 'Previous upload available' : 'Uploading…')}
            </span>
            <span className="text-muted-foreground">
              {formatBytes(state.bytesUploaded)} / {formatBytes(state.totalBytes || state.file?.size || 0)}
            </span>
          </div>
          <Progress value={Math.round(state.progress * 100)} />
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>
              {state.status === 'creatingSession'
                ? 'Starting upload…'
                : state.status === 'completing'
                ? 'Finalizing…'
                : state.status === 'resuming'
                ? 'Resuming…'
                : state.status === 'paused'
                ? 'Paused'
                : formatSpeed(state.speedBps)}
            </span>
            <span>{state.etaSeconds !== null ? `ETA ${formatEta(state.etaSeconds)}` : null}</span>
          </div>
          <ChunkGrid states={state.chunkStates} count={state.chunkCount} />
          <div className="flex gap-2">
            {state.status === 'uploading' || state.status === 'resuming' ? (
              <Button size="sm" variant="outline" onClick={() => actions.pause()}>
                <Pause className="h-4 w-4" />
                Pause
              </Button>
            ) : null}
            {state.status === 'paused' ? (
              <Button size="sm" onClick={() => actions.resume()}>
                <Play className="h-4 w-4" />
                Resume
              </Button>
            ) : null}
            <Button size="sm" variant="ghost" onClick={() => actions.cancel()}>
              <X className="h-4 w-4" />
              Cancel
            </Button>
          </div>
          {resumable ? (
            <p className="text-xs text-muted-foreground">
              An upload was interrupted. Choose the same file again to resume.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ChunkGrid({
  states,
  count,
}: {
  states: Record<number, 'idle' | 'uploading' | 'done' | 'failed'>;
  count: number;
}) {
  if (count < 2) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {Array.from({ length: count }, (_, i) => {
        const s = states[i] ?? 'idle';
        return (
          <div
            key={i}
            title={`Chunk ${i + 1}: ${s}`}
            className={cn(
              'h-2 w-2 rounded-sm',
              s === 'done' && 'bg-emerald-500/80',
              s === 'uploading' && 'bg-primary animate-pulse',
              s === 'failed' && 'bg-destructive',
              s === 'idle' && 'bg-muted',
            )}
          />
        );
      })}
    </div>
  );
}
