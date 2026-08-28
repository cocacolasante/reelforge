'use client';

import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, Plus, Sparkles, Trash2, Wand2 } from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { Uploader } from '@/components/app/uploader';
import { JobProgress } from '@/components/app/job-progress';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Label } from '@/components/ui/label';
import { Slider } from '@/components/ui/slider';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import {
  useDeleteAsset,
  useEnqueueAnalyze,
  useEnqueueSelect,
  useProject,
  useProjectAssets,
} from '@/lib/api/hooks';
import { humanMessage } from '@/lib/api/errors';
import { APIError } from '@/lib/api/errors';
import { resetUploaderStore } from '@/lib/upload/uploader';
import { api, API_BASE } from '@/lib/api/client';
import { formatBytes, formatDuration } from '@/lib/format';

export default function ProjectDetailPage({
  params,
}: {
  params: { projectId: string };
}) {
  return (
    <AppShell>
      <ProjectDetail projectId={params.projectId} />
    </AppShell>
  );
}

function ProjectDetail({ projectId }: { projectId: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const project = useProject(projectId);
  const assets = useProjectAssets(projectId);
  const projectReels = useQuery({
    queryKey: ['project-reels', projectId],
    queryFn: () => api<{ reels: unknown[]; asset_count: number }>(`/projects/${projectId}/reels`),
  });

  const [showUploader, setShowUploader] = React.useState(false);
  const [jobIds, setJobIds] = React.useState<Record<string, string>>({});
  const [selectJobs, setSelectJobs] = React.useState<string[]>([]);
  const [selectOutcome, setSelectOutcome] = React.useState<
    { reelCount: number; failed: number } | null
  >(null);
  // Per-job settle bookkeeping. Keyed by job id so a re-fired SSE `done`
  // event (StrictMode remounts, stream reconnects) can't double-count.
  const settledJobs = React.useRef(
    new Map<string, { ok: boolean; reelCount: number }>(),
  );

  const onUploadComplete = React.useCallback(() => {
    setShowUploader(false);
  }, []);

  const handleSelectQueued = React.useCallback((jids: string[]) => {
    settledJobs.current = new Map();
    setSelectOutcome(null);
    setSelectJobs(jids);
  }, []);

  const handleSelectJobSettled = React.useCallback(
    (jobId: string, outcome: { ok: boolean; reelCount: number }) => {
      if (settledJobs.current.has(jobId)) return;
      settledJobs.current.set(jobId, outcome);
      setSelectJobs((jobs) => {
        if (settledJobs.current.size < jobs.length) return jobs;
        // Every select job has settled — now decide where to go.
        const outcomes = [...settledJobs.current.values()];
        const failed = outcomes.filter((o) => !o.ok).length;
        const reelCount = outcomes.reduce((n, o) => n + o.reelCount, 0);
        void (async () => {
          await queryClient.invalidateQueries({
            queryKey: ['project-reels', projectId],
          });
          await projectReels.refetch();
          if (reelCount > 0) {
            setSelectJobs([]);
            router.push(`/projects/${projectId}/reels`);
          } else {
            // Stay put: navigating to an empty reels page is a dead end.
            // Keep failed JobProgress cards mounted so their errors stay visible.
            setSelectOutcome({ reelCount, failed });
            if (failed === 0) setSelectJobs([]);
          }
        })();
        return jobs;
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [projectId, queryClient, router],
  );

  if (project.isLoading) {
    return <div className="container py-10"><div className="h-8 w-48 animate-pulse rounded bg-card/40" /></div>;
  }
  if (project.error) {
    return (
      <div className="container py-10">
        <Alert variant="destructive">
          <AlertTitle>Project unavailable</AlertTitle>
          <AlertDescription>{humanMessage(project.error)}</AlertDescription>
        </Alert>
      </div>
    );
  }

  const allAssets = assets.data?.assets ?? [];
  // Footage drives analysis and reel selection; photos are stills that get
  // woven into a reel at compose time, so they're listed separately.
  const assetList = allAssets.filter((a) => a.kind === 'video');
  const photoList = allAssets.filter((a) => a.kind === 'photo');
  const reelCount = (projectReels.data?.reels as unknown[] | undefined)?.length ?? 0;

  return (
    <div className="container space-y-6 py-8">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link href="/" className="text-xs text-muted-foreground hover:text-foreground">
            ← All projects
          </Link>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">
            {project.data?.name}
          </h1>
        </div>
        {reelCount > 0 ? (
          <Button onClick={() => router.push(`/projects/${projectId}/reels`)}>
            View {reelCount} reel{reelCount === 1 ? '' : 's'}
            <ArrowRight className="h-4 w-4" />
          </Button>
        ) : null}
      </header>

      {/* Source clips */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Source clips</CardTitle>
          {allAssets.length > 0 ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                // Clear any finished/failed upload left in the per-project
                // store before reopening, so the dropzone is always live.
                resetUploaderStore(projectId);
                setShowUploader((v) => !v);
              }}
            >
              <Plus className="h-4 w-4" />
              {showUploader ? 'Cancel' : 'Add clip or photo'}
            </Button>
          ) : null}
        </CardHeader>
        <CardContent className="space-y-4">
          {allAssets.length === 0 || showUploader ? (
            <Uploader projectId={projectId} onComplete={onUploadComplete} />
          ) : null}
          {assetList.length > 0 ? (
            <div className="space-y-3">
              {assetList.map((a) => (
                <AssetRow
                  key={a.id}
                  projectId={projectId}
                  asset={a}
                  activeJobId={jobIds[a.id] ?? null}
                  onDeleted={() =>
                    setJobIds((prev) => {
                      const { [a.id]: _omit, ...rest } = prev;
                      return rest;
                    })
                  }
                  onQueued={(jid) =>
                    setJobIds((prev) => ({ ...prev, [a.id]: jid }))
                  }
                  onSettled={() => {
                    setJobIds((prev) => {
                      const { [a.id]: _omit, ...rest } = prev;
                      return rest;
                    });
                    // analysis_ready comes from the asset list — refetch it
                    // so the badge and the Select button update together.
                    void queryClient.invalidateQueries({
                      queryKey: ['assets', projectId],
                    });
                  }}
                />
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/* Photos */}
      {photoList.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Photos</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">
              Add these as still shots when composing a reel — open a reel and
              pick them in the Photos section.
            </p>
            <div className="flex flex-wrap gap-3">
              {photoList.map((p) => (
                <PhotoTile key={p.id} projectId={projectId} photo={p} />
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* Selection panel */}
      <Card>
        <CardHeader>
          <CardTitle>Select reels</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <SelectionPanel
            assets={assetList.map((a) => ({
              id: a.id,
              analysisReady: a.analysis_ready,
            }))}
            activeJobIds={selectJobs}
            existingReelCount={reelCount}
            onQueued={handleSelectQueued}
            onJobSettled={handleSelectJobSettled}
          />
          {selectOutcome ? (
            <Alert variant={selectOutcome.failed > 0 ? 'destructive' : 'info'}>
              <AlertTitle>
                {selectOutcome.failed > 0
                  ? 'Clip selection had errors'
                  : 'No clips found in this footage'}
              </AlertTitle>
              <AlertDescription>
                {selectOutcome.failed > 0
                  ? `${selectOutcome.failed} selection job${selectOutcome.failed === 1 ? '' : 's'} failed — see the error above. `
                  : ''}
                {selectOutcome.reelCount === 0
                  ? 'Selection finished but no clip in your duration range could be cut from this footage. Try widening the min/max duration, or re-run Analyze so long continuous takes are split into usable segments.'
                  : ''}
              </AlertDescription>
            </Alert>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}

// ---- per-asset row ------------------------------------------------------

function AssetRow({
  projectId,
  asset,
  activeJobId,
  onQueued,
  onSettled,
  onDeleted,
}: {
  projectId: string;
  asset: NonNullable<ReturnType<typeof useProjectAssets>['data']>['assets'][number];
  activeJobId: string | null;
  onQueued: (jobId: string) => void;
  onSettled: () => void;
  onDeleted: () => void;
}) {
  const enqueue = useEnqueueAnalyze();
  const remove = useDeleteAsset(projectId);
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const ready = asset.analysis_ready;

  const trigger = async () => {
    try {
      const job = await enqueue.mutateAsync({ assetId: asset.id, config: {} });
      onQueued(job.id);
    } catch {
      /* surfaced */
    }
  };

  const confirmDelete = async () => {
    try {
      await remove.mutateAsync(asset.id);
      setConfirmOpen(false);
      onDeleted();
    } catch {
      /* surfaced inline */
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border bg-card/60 p-3 text-sm">
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium">{asset.original_filename}</div>
        <div className="text-xs text-muted-foreground">
          {formatDuration(asset.duration_sec)} · {asset.width}×{asset.height}
          @ {asset.fps.toFixed(0)}fps · {formatBytes(asset.size_bytes)}
        </div>
      </div>
      <Badge variant={ready ? 'secondary' : 'muted'}>
        {ready ? 'analyzed' : activeJobId ? 'analyzing…' : 'pending'}
      </Badge>
      {!ready && !activeJobId ? (
        <Button size="sm" onClick={trigger} disabled={enqueue.isPending}>
          <Sparkles className="h-4 w-4" />
          Analyze
        </Button>
      ) : null}
      <Button
        size="sm"
        variant="ghost"
        title="Delete this clip"
        onClick={() => setConfirmOpen(true)}
        disabled={remove.isPending}
        className="text-muted-foreground hover:text-destructive"
      >
        <Trash2 className="h-4 w-4" />
        {remove.isPending ? 'Deleting…' : null}
      </Button>
      {activeJobId ? (
        <div className="w-full">
          <JobProgress
            jobId={activeJobId}
            variant="analyze"
            onDone={onSettled}
            onFail={onSettled}
          />
        </div>
      ) : null}
      {enqueue.error ? (
        <Alert variant="destructive">
          <AlertDescription>{humanMessage(enqueue.error)}</AlertDescription>
        </Alert>
      ) : null}
      {remove.error ? (
        <Alert variant="destructive">
          <AlertDescription>{humanMessage(remove.error)}</AlertDescription>
        </Alert>
      ) : null}

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete this clip?</DialogTitle>
            <DialogDescription>
              <span className="font-semibold text-foreground">
                {asset.original_filename}
              </span>{' '}
              and everything derived from it — analysis, reels, composed videos
              and exports — will be permanently removed from disk.
              {activeJobId ? ' Any processing still running will be stopped.' : ''}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-2">
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Keep it
            </Button>
            <Button
              variant="destructive"
              onClick={() => void confirmDelete()}
              disabled={remove.isPending}
            >
              <Trash2 className="h-4 w-4" />
              {remove.isPending ? 'Deleting…' : 'Delete clip'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---- selection panel ----------------------------------------------------

function SelectionPanel({
  assets,
  activeJobIds,
  existingReelCount,
  onQueued,
  onJobSettled,
}: {
  assets: Array<{ id: string; analysisReady: boolean }>;
  activeJobIds: string[];
  existingReelCount: number;
  onQueued: (ids: string[]) => void;
  onJobSettled: (
    jobId: string,
    outcome: { ok: boolean; reelCount: number },
  ) => void;
}) {
  const enqueue = useEnqueueSelect();
  const [form, setForm] = React.useState<'short' | 'long_single' | 'long_montage'>('short');
  const [minSec, setMinSec] = React.useState<number[]>([30]);
  const [maxSec, setMaxSec] = React.useState<number[]>([60]);
  const [longTarget, setLongTarget] = React.useState<number[]>([300]);
  const [count, setCount] = React.useState(10);
  const [prompt, setPrompt] = React.useState('');
  const [advancedOpen, setAdvancedOpen] = React.useState(false);
  const [shortlistSize, setShortlistSize] = React.useState(40);
  const [diversity, setDiversity] = React.useState<number[]>([8]);
  const [refine, setRefine] = React.useState(true);
  const [zeroMatchNote, setZeroMatchNote] = React.useState(false);
  const [errorMsg, setErrorMsg] = React.useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = React.useState(false);

  // Snap min/max sliders to short-form defaults when toggling back from a long mode.
  React.useEffect(() => {
    if (form === 'short') {
      if (minSec[0] > 120) setMinSec([30]);
      if (maxSec[0] > 180) setMaxSec([60]);
    }
  }, [form, minSec, maxSec]);

  const readyAssetIds = assets.filter((a) => a.analysisReady).map((a) => a.id);

  const startSelect = () => {
    setErrorMsg(null);
    if (readyAssetIds.length === 0) {
      setErrorMsg('Analyze at least one source clip first.');
      return;
    }
    if (existingReelCount > 0) {
      setConfirmOpen(true);
      return;
    }
    void runSelect();
  };

  const runSelect = async () => {
    setConfirmOpen(false);
    setZeroMatchNote(false);
    const config: Record<string, unknown> = {
      output_form: form,
      top_k: count,
    };
    if (prompt.trim()) config.prompt = prompt.trim();
    // Advanced (Selection v2) knobs — only sent when changed from defaults so
    // the job config stays minimal.
    if (shortlistSize !== 40) config.shortlist_size = shortlistSize;
    if (diversity[0] !== 8) config.diversity_lambda = diversity[0];
    if (!refine) config.refine = false;
    if (form === 'long_single') {
      config.long_target_duration_sec = longTarget[0];
    } else {
      config.target_min_sec = minSec[0];
      config.target_max_sec = maxSec[0];
    }

    const newJobIds: string[] = [];
    for (const aid of readyAssetIds) {
      try {
        const job = await enqueue.mutateAsync({ assetId: aid, config });
        newJobIds.push(job.id);
      } catch (err) {
        if (err instanceof APIError && err.code === 'JOB_ALREADY_RUNNING') {
          continue; // already in flight; UI shows progress elsewhere
        }
        setErrorMsg(humanMessage(err));
      }
    }
    onQueued(newJobIds);
  };

  if (assets.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        Upload at least one source clip to start.
      </p>
    );
  }

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <Label>Output form</Label>
        <div className="flex flex-wrap gap-2">
          {([
            ['short', 'Short reels'],
            ['long_single', 'Long single span'],
            ['long_montage', 'Long montage'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setForm(key)}
              className={
                'rounded-md border px-3 py-1.5 text-sm transition ' +
                (form === key
                  ? 'border-primary bg-primary/10 text-foreground'
                  : 'border-border text-muted-foreground hover:border-muted-foreground/60')
              }
            >
              {label}
            </button>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          {form === 'short'
            ? 'Multiple short reels, each in the chosen duration range.'
            : form === 'long_single'
            ? 'One long span per source clip, sized near the target duration.'
            : 'Top short reels stitched into one longer video (per source).'}
        </p>
      </div>

      {form === 'long_single' ? (
        <div className="space-y-1.5">
          <Label>Target duration: {Math.floor(longTarget[0] / 60)}:{String(longTarget[0] % 60).padStart(2, '0')}</Label>
          <Slider value={longTarget} min={120} max={1800} step={30} onValueChange={setLongTarget} />
          <p className="text-xs text-muted-foreground">2:00 – 30:00</p>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Min duration: {minSec[0]}s</Label>
            <Slider
              value={minSec}
              min={10}
              max={300}
              step={5}
              onValueChange={(v) => {
                setMinSec(v);
                if (v[0] > maxSec[0]) setMaxSec([v[0]]);
              }}
            />
          </div>
          <div className="space-y-1.5">
            <Label>Max duration: {maxSec[0]}s</Label>
            <Slider
              value={maxSec}
              min={20}
              max={600}
              step={5}
              onValueChange={(v) => {
                setMaxSec(v);
                if (v[0] < minSec[0]) setMinSec([v[0]]);
              }}
            />
          </div>
        </div>
      )}

      <div className="space-y-1.5">
        <Label htmlFor="select-direction">Direction (optional)</Label>
        <textarea
          id="select-direction"
          value={prompt}
          maxLength={500}
          rows={2}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="e.g. clips of falls, big jumps, make it feel intense"
          className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
        <p className="text-xs text-muted-foreground">
          Describe what you want and only matching clips are returned, styled to fit.
        </p>
      </div>

      {form !== 'long_single' ? (
        <div className="space-y-1.5">
          <Label>How many clips: {count}</Label>
          <Input
            type="number"
            min={1}
            max={30}
            value={count}
            onChange={(e) => setCount(Math.max(1, Math.min(30, Number(e.target.value) || 1)))}
            className="w-24"
          />
          {form === 'long_montage' ? (
            <p className="text-xs text-muted-foreground">
              All {count} clips will be stitched into one montage.
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="space-y-3">
        <button
          type="button"
          onClick={() => setAdvancedOpen((v) => !v)}
          className="text-xs font-medium text-muted-foreground underline-offset-2 hover:underline"
        >
          {advancedOpen ? '▾ Advanced' : '▸ Advanced'}
        </button>
        {advancedOpen ? (
          <div className="space-y-4 rounded-md border border-border p-3">
            <div className="space-y-1.5">
              <Label>Shortlist size: {shortlistSize}</Label>
              <Input
                type="number"
                min={5}
                max={80}
                value={shortlistSize}
                onChange={(e) =>
                  setShortlistSize(Math.max(5, Math.min(80, Number(e.target.value) || 40)))
                }
                className="w-24"
              />
              <p className="text-xs text-muted-foreground">
                How many pre-scored candidates the AI ranker compares (more = better
                coverage, higher cost).
              </p>
            </div>
            <div className="space-y-1.5">
              <Label>Variety: {diversity[0]}</Label>
              <Slider
                min={0}
                max={20}
                step={1}
                value={diversity}
                onValueChange={(v: number[]) => setDiversity(v)}
              />
              <p className="text-xs text-muted-foreground">
                Pushes same-topic near-duplicates down the list. 0 = pure score order.
              </p>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={refine}
                onChange={(e) => setRefine(e.target.checked)}
                className="h-4 w-4 accent-primary"
              />
              Fine-tune cut points (one extra small AI call)
            </label>
          </div>
        ) : null}
      </div>

      {errorMsg ? (
        <Alert variant="destructive">
          <AlertDescription>{errorMsg}</AlertDescription>
        </Alert>
      ) : null}

      {zeroMatchNote ? (
        <p className="text-sm text-muted-foreground">
          No clips matched your direction — try broader wording or run without a
          prompt.
        </p>
      ) : null}

      <Button onClick={startSelect} disabled={enqueue.isPending || readyAssetIds.length === 0}>
        <Wand2 className="h-4 w-4" />
        {enqueue.isPending
          ? 'Starting…'
          : existingReelCount > 0
          ? `Re-select reels from ${readyAssetIds.length} clip${readyAssetIds.length === 1 ? '' : 's'}`
          : `Select reels from ${readyAssetIds.length} clip${readyAssetIds.length === 1 ? '' : 's'}`}
      </Button>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Replace existing reels?</DialogTitle>
            <DialogDescription>
              Re-running Select overwrites the ranking for this project. The{' '}
              <span className="font-semibold text-foreground">
                {existingReelCount} existing reel{existingReelCount === 1 ? '' : 's'}
              </span>{' '}
              will be replaced with a fresh selection.
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-foreground/80">
            Composed mezzanines on disk are kept (re-selecting the same span will
            reuse them). Only <code className="font-mono">reels.json</code> and
            the DB rows get rewritten.
          </div>
          <DialogFooter className="gap-2 sm:gap-2">
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button onClick={() => void runSelect()}>
              <Wand2 className="h-4 w-4" />
              Replace & re-select
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {activeJobIds.length > 0 ? (
        <div className="space-y-2">
          {activeJobIds.map((jid) => (
            <JobProgress
              key={jid}
              jobId={jid}
              variant="select"
              onDone={(result) => {
                const reelCount =
                  result && typeof result === 'object' && 'reel_count' in result
                    ? Number((result as { reel_count?: unknown }).reel_count) || 0
                    : 0;
                if (reelCount === 0 && prompt.trim()) setZeroMatchNote(true);
                onJobSettled(jid, { ok: true, reelCount });
              }}
              onFail={() => onJobSettled(jid, { ok: false, reelCount: 0 })}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

// ---- photo tile ---------------------------------------------------------

function PhotoTile({
  projectId,
  photo,
}: {
  projectId: string;
  photo: NonNullable<ReturnType<typeof useProjectAssets>['data']>['assets'][number];
}) {
  const remove = useDeleteAsset(projectId);
  return (
    <div className="group relative">
      <img
        alt={photo.original_filename}
        src={`${API_BASE}/api/v1/assets/${photo.id}/photo`}
        className="h-24 w-24 rounded-md border object-cover bg-muted"
        loading="lazy"
        onError={(e) => {
          (e.target as HTMLImageElement).style.visibility = 'hidden';
        }}
      />
      <button
        title={`Delete ${photo.original_filename}`}
        onClick={() => void remove.mutateAsync(photo.id).catch(() => {})}
        disabled={remove.isPending}
        className="absolute -right-2 -top-2 rounded-full border bg-background p-1 text-muted-foreground opacity-0 transition group-hover:opacity-100 hover:text-destructive"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
      <div className="mt-1 max-w-24 truncate text-[10px] text-muted-foreground">
        {photo.original_filename}
      </div>
    </div>
  );
}
