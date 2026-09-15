'use client';

import * as React from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Upload, Pause, Play, X, AlertTriangle, CheckCircle2, Plus, SkipForward } from 'lucide-react';
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
  // The store outlives this component (module-level, per-project). If we
  // mount while a *previous* upload's `done` state is still in the store,
  // that completion was already handled — seed the ref so we don't re-fire
  // onComplete (which closes the panel) for it, and clear the stale state so
  // the dropzone renders.
  const lastDoneRef = React.useRef<string | null>(
    state.status === 'done' ? state.asset?.id ?? 'done' : null,
  );
  const mountedWithStaleDone = React.useRef(state.status === 'done' && state.queue.length === 0);

  React.useEffect(() => {
    if (mountedWithStaleDone.current) {
      mountedWithStaleDone.current = false;
      actions.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const uploaded = state.finished.filter((o) => o.assetId);
  const skipped = state.finished.filter((o) => o.error);
  const batchDone =
    state.status === 'done' && state.queue.length === 0 && !state.running;

  // Every finished file refreshes the clip list; the panel only closes when
  // the whole batch went through cleanly (otherwise the summary stays up).
  React.useEffect(() => {
    if (state.status !== 'done') return;
    const tag = state.asset?.id ?? 'done';
    if (lastDoneRef.current === tag) return;
    lastDoneRef.current = tag;
    qc.invalidateQueries({ queryKey: ['assets', projectId] });
    qc.invalidateQueries({ queryKey: ['project', projectId] });
    if (state.queue.length === 0 && !state.finished.some((o) => o.error)) onComplete?.();
  }, [state.status, state.asset, state.queue.length, state.finished, projectId, qc, onComplete]);

  const handleFiles = async (files: File[]) => {
    if (files.length === 0) return;
    // A persisted session with no file means "pick the same file to resume".
    // If the first picked file matches, continue it and queue the rest;
    // otherwise drop the stale session and upload everything fresh.
    if (state.status === 'paused' && !state.file && state.uploadId) {
      const [first, ...rest] = files;
      if (await actions.attachAndResume(first)) {
        actions.enqueue(rest);
        return;
      }
      await actions.cancel();
    }
    actions.enqueue(files);
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    void handleFiles(Array.from(e.dataTransfer.files));
  };

  const onChoose = (e: React.ChangeEvent<HTMLInputElement>) => {
    void handleFiles(Array.from(e.target.files ?? []));
    // Allow re-picking the same filenames later.
    e.target.value = '';
  };

  const picker = (
    // Always mounted so the resume card and "Add more" can open it too.
    // Extensions are listed alongside video/* because the OS reports no MIME
    // type for plenty of real camera files, and the picker greys out anything
    // the accept list doesn't match.
    <input
      ref={inputRef}
      type="file"
      multiple
      accept="video/*,image/*,.mp4,.m4v,.mov,.webm,.mkv,.avi,.mpg,.mpeg,.wmv,.flv,.3gp,.mts,.m2ts,.ts,.mxf,.360,.insv,.lrv,.jpg,.jpeg,.png,.webp,.gif,.bmp,.tif,.tiff"
      className="hidden"
      onChange={onChoose}
    />
  );

  const skippedList =
    skipped.length > 0 ? (
      <Alert variant="destructive">
        <AlertTriangle className="h-4 w-4" />
        <AlertTitle>
          {skipped.length} file{skipped.length === 1 ? '' : 's'} not uploaded
        </AlertTitle>
        <AlertDescription>
          <ul className="mt-1 space-y-1 text-xs">
            {skipped.map((o, i) => (
              <li key={`${o.name}-${i}`}>
                <span className="font-medium">{o.name}</span> — {o.error}
              </li>
            ))}
          </ul>
        </AlertDescription>
      </Alert>
    ) : null;

  if (batchDone) {
    return (
      <div className="space-y-3">
        <Alert variant="info" className="border-emerald-500/40">
          <CheckCircle2 className="h-4 w-4 text-emerald-400" />
          <AlertTitle>
            {state.batchTotal > 1
              ? `Uploaded ${uploaded.length} of ${state.batchTotal} files`
              : 'Upload complete'}
          </AlertTitle>
          <AlertDescription>
            {state.batchTotal > 1 ? (
              <ul className="mt-1 space-y-0.5 text-xs">
                {uploaded.map((o) => (
                  <li key={o.assetId}>{o.name}</li>
                ))}
              </ul>
            ) : (
              <>
                {state.asset?.original_filename} ·{' '}
                {state.asset ? formatBytes(state.asset.size_bytes) : ''}
              </>
            )}
          </AlertDescription>
          <div className="mt-3">
            <Button size="sm" variant="secondary" onClick={() => actions.reset()}>
              <Upload className="h-4 w-4" />
              Upload more
            </Button>
          </div>
        </Alert>
        {skippedList}
        {picker}
      </div>
    );
  }

  const resumable = state.status === 'paused' && !state.file;
  const inBatch = state.batchTotal > 1;
  const position = Math.min(state.batchTotal, state.finished.length + 1);

  return (
    <div className="space-y-4">
      {state.status === 'idle' || (state.status === 'failed' && !state.uploadId) ? (
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
          <div className="text-lg font-medium">
            Drop videos or photos, or click to choose
          </div>
          <p className="max-w-sm text-sm text-muted-foreground">
            Select as many as you like — they upload one after another. Video: MP4, MOV, MKV,
            AVI, MTS. Photos: JPEG, PNG, WebP — added as still shots inside your reels. Up to 5
            GB per file.
          </p>
          <Button
            variant="secondary"
            className="mt-2"
            onClick={() => inputRef.current?.click()}
          >
            Choose files
          </Button>
        </div>
      ) : null}

      {picker}

      {skippedList}

      {state.error && state.uploadId ? (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Upload error{state.file ? ` · ${state.file.name}` : ''}</AlertTitle>
          <AlertDescription>{humanMessage(state.error)}</AlertDescription>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" onClick={() => actions.retry()}>
              Retry
            </Button>
            {state.queue.length > 0 ? (
              <Button size="sm" variant="outline" onClick={() => void actions.cancel()}>
                <SkipForward className="h-4 w-4" />
                Skip this file
              </Button>
            ) : null}
            <Button size="sm" variant="ghost" onClick={() => void actions.cancelAll()}>
              Cancel{state.queue.length > 0 ? ' all' : ''}
            </Button>
          </div>
        </Alert>
      ) : null}

      {state.status !== 'idle' && !(state.status === 'failed' && !state.uploadId) ? (
        <div className="space-y-3 rounded-lg border bg-card p-4">
          {inBatch ? (
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>
                File {position} of {state.batchTotal}
                {state.queue.length > 0 ? ` · ${state.queue.length} waiting` : ''}
              </span>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 px-2"
                onClick={() => inputRef.current?.click()}
              >
                <Plus className="h-3.5 w-3.5" />
                Add more
              </Button>
            </div>
          ) : null}
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
                : state.status === 'done'
                ? 'Done — starting next…'
                : formatSpeed(state.speedBps)}
            </span>
            <span>{state.etaSeconds !== null ? `ETA ${formatEta(state.etaSeconds)}` : null}</span>
          </div>
          <ChunkGrid states={state.chunkStates} count={state.chunkCount} />
          <div className="flex flex-wrap gap-2">
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
            {state.queue.length > 0 ? (
              <>
                <Button size="sm" variant="ghost" onClick={() => void actions.cancel()}>
                  <SkipForward className="h-4 w-4" />
                  Skip this file
                </Button>
                <Button size="sm" variant="ghost" onClick={() => void actions.cancelAll()}>
                  <X className="h-4 w-4" />
                  Cancel all
                </Button>
              </>
            ) : (
              <Button size="sm" variant="ghost" onClick={() => void actions.cancel()}>
                <X className="h-4 w-4" />
                Cancel
              </Button>
            )}
          </div>
          {state.queue.length > 0 ? (
            <div className="text-xs text-muted-foreground">
              Up next:{' '}
              {state.queue
                .slice(0, 3)
                .map((f) => f.name)
                .join(', ')}
              {state.queue.length > 3 ? ` +${state.queue.length - 3} more` : ''}
            </div>
          ) : null}
          {resumable ? (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">
                An upload was interrupted. Choose the same file again to pick up
                where it left off — already-uploaded parts are kept.
              </p>
              <Button size="sm" onClick={() => inputRef.current?.click()}>
                <Upload className="h-4 w-4" />
                Choose file to resume
              </Button>
            </div>
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
