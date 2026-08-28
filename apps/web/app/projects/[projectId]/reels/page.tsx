'use client';

import * as React from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, Film, Layers } from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Slider } from '@/components/ui/slider';
import { JobProgress } from '@/components/app/job-progress';
import { useProject } from '@/lib/api/hooks';
import { humanMessage } from '@/lib/api/errors';
import { formatDuration } from '@/lib/format';
import { api, API_BASE } from '@/lib/api/client';

interface ProjectReel {
  id: string;
  project_id: string;
  asset_id: string;
  asset_filename?: string;
  rank: number;
  project_rank: number;
  title: string;
  hook: string;
  justification: string;
  start_sec: number;
  end_sec: number;
  duration_sec: number;
  overall_score: number;
  suggested_mood: string;
  scene_indices: number[];
  scores: Record<string, number>;
  mezzanine_ready: boolean;
  prompt_relevance?: number | null;
  source?: string | null;
  opening_description?: string | null;
}

interface MontageOut {
  id: string;
  title: string;
  duration_sec: number;
  child_reel_ids: string[];
  mezzanine_ready: boolean;
  created_at: string;
}

export default function ReelsListPage({
  params,
}: {
  params: { projectId: string };
}) {
  return (
    <AppShell>
      <Body projectId={params.projectId} />
    </AppShell>
  );
}

function Body({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const reelsQuery = useQuery({
    queryKey: ['project-reels', projectId],
    queryFn: () =>
      api<{ reels: ProjectReel[]; asset_count: number }>(`/projects/${projectId}/reels`),
    // The project page pre-populates this key (often with an empty list) —
    // always refetch on mount so a just-finished select job is picked up.
    refetchOnMount: 'always',
  });
  const montagesQuery = useQuery({
    queryKey: ['project-montages', projectId],
    queryFn: () => api<{ montages: MontageOut[] }>(`/projects/${projectId}/montages`),
    refetchOnMount: 'always',
  });

  if (project.isLoading) {
    return <div className="container py-10"><div className="h-8 w-40 animate-pulse rounded bg-card/40" /></div>;
  }
  if (project.error) {
    return <div className="container py-10"><Alert variant="destructive"><AlertDescription>{humanMessage(project.error)}</AlertDescription></Alert></div>;
  }
  const reels = reelsQuery.data?.reels ?? [];
  const montages = montagesQuery.data?.montages ?? [];

  // Show the skeleton during the initial load AND while a refetch is in
  // flight with nothing to show yet — the cache is often pre-seeded with an
  // empty list by the project page, which makes `isLoading` false.
  if (reelsQuery.isLoading || (reelsQuery.isFetching && reels.length === 0)) {
    return (
      <div className="container py-10 space-y-3">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="h-24 animate-pulse rounded-lg border bg-card/40" />
        ))}
      </div>
    );
  }

  if (reelsQuery.error) {
    return (
      <div className="container py-10">
        <Alert variant="destructive">
          <AlertTitle>Couldn&apos;t load reels</AlertTitle>
          <AlertDescription>
            {humanMessage(reelsQuery.error)}
            <div className="mt-3">
              <Button size="sm" variant="outline" onClick={() => void reelsQuery.refetch()}>
                Try again
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  if (reels.length === 0 && montages.length === 0) {
    return (
      <div className="container py-10">
        <Alert>
          <AlertTitle>No reels yet</AlertTitle>
          <AlertDescription>
            <p>
              If you just ran clip selection, it found no spans in your duration
              range — go back, widen the min/max duration, or re-run Analyze so
              long continuous takes get split into usable segments.
            </p>
            <div className="mt-3 flex gap-2">
              <Button size="sm" variant="outline" asChild>
                <Link href={`/projects/${projectId}`}>Back to project</Link>
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void reelsQuery.refetch()}>
                Refresh
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  const composedReadyReels = reels.filter((r) => r.mezzanine_ready);

  return (
    <div className="container space-y-6 py-8">
      <header>
        <Link href={`/projects/${projectId}`} className="text-xs text-muted-foreground hover:text-foreground">
          ← {project.data?.name}
        </Link>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          {reels.length} reels ranked
        </h1>
        <p className="text-sm text-muted-foreground">
          Aggregated across {reelsQuery.data?.asset_count ?? 0} source clip
          {reelsQuery.data?.asset_count === 1 ? '' : 's'}.
        </p>
      </header>

      {montages.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Long-form montages</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {montages.map((m) => (
              <Link
                key={m.id}
                href={`/projects/${projectId}/reels/${m.id}`}
                className="flex items-center justify-between rounded-lg border bg-card/60 px-4 py-3 transition-colors hover:border-primary/60"
              >
                <div>
                  <div className="font-medium">{m.title}</div>
                  <div className="text-xs text-muted-foreground">
                    {formatDuration(m.duration_sec)} ·{' '}
                    {m.child_reel_ids.length} chapter
                    {m.child_reel_ids.length === 1 ? '' : 's'}
                  </div>
                </div>
                <Badge variant={m.mezzanine_ready ? 'secondary' : 'muted'}>
                  {m.mezzanine_ready ? 'ready' : 'composing…'}
                </Badge>
              </Link>
            ))}
          </CardContent>
        </Card>
      ) : null}

      <MontageBuilder
        projectId={projectId}
        candidateReels={composedReadyReels}
        onCreated={() => {
          void montagesQuery.refetch();
          void reelsQuery.refetch();
        }}
      />

      <div className="space-y-3">
        {reels.map((r) => (
          <ReelCard key={r.id} projectId={projectId} reel={r} />
        ))}
      </div>
    </div>
  );
}

function ReelCard({ projectId, reel }: { projectId: string; reel: ProjectReel }) {
  return (
    <Link
      href={`/projects/${projectId}/reels/${reel.id}`}
      className="block rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <Card className="transition-colors hover:border-primary/60">
        <CardContent className="flex items-center gap-5 py-4">
          <div className="flex shrink-0 items-center justify-center rounded-md bg-primary/20 px-3 text-xl font-bold text-primary">
            {reel.project_rank}
          </div>
          <ThumbStrip asset_id={reel.asset_id} scene_indices={reel.scene_indices} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="truncate font-semibold">{reel.title}</h3>
              <Badge variant="secondary">{reel.suggested_mood}</Badge>
              {reel.source ? <Badge variant="muted">{reel.source}</Badge> : null}
              {typeof reel.prompt_relevance === 'number' ? (
                <Badge variant="outline">Match {reel.prompt_relevance}%</Badge>
              ) : null}
              {reel.mezzanine_ready ? (
                <Badge variant="muted">composed</Badge>
              ) : null}
            </div>
            <p className="line-clamp-2 text-sm text-muted-foreground">{reel.hook}</p>
            {reel.opening_description ? (
              <p className="mt-0.5 line-clamp-1 text-xs italic text-muted-foreground/70">
                Opens on: {reel.opening_description}
              </p>
            ) : null}
            <p className="mt-1 text-xs text-muted-foreground/80">
              {formatDuration(reel.start_sec)} – {formatDuration(reel.end_sec)} ·{' '}
              {reel.duration_sec.toFixed(1)}s
              {reel.asset_filename ? ` · ${reel.asset_filename}` : ''}
            </p>
          </div>
          <div className="flex w-28 flex-col items-end gap-1">
            <div className="text-2xl font-semibold leading-none tabular-nums">
              {reel.overall_score.toFixed(0)}
            </div>
            <div className="text-xs text-muted-foreground">score</div>
          </div>
          <ArrowRight className="h-4 w-4 opacity-50" />
        </CardContent>
      </Card>
    </Link>
  );
}

function MontageBuilder({
  projectId,
  candidateReels,
  onCreated,
}: {
  projectId: string;
  candidateReels: ProjectReel[];
  onCreated: () => void;
}) {
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [title, setTitle] = React.useState('Long montage');
  const [transition, setTransition] = React.useState<number[]>([0.6]);
  const [jobId, setJobId] = React.useState<string | null>(null);
  const qc = useQueryClient();

  const createMontage = useMutation({
    mutationFn: (payload: { reel_ids: string[]; title: string; transition_duration_sec: number }) =>
      api<{ id: string }>(`/projects/${projectId}/montages`, {
        method: 'POST',
        body: payload,
      }),
    onSuccess: (job) => {
      // The API returns a JobOut shape (id is the job id).
      setJobId(job.id);
      setSelected(new Set());
    },
  });

  if (candidateReels.length < 2) {
    // The "two or more composed reels" gate prevents 1-element montages, which
    // would just be a re-encode with no benefit — but say so instead of
    // silently hiding the feature.
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Layers className="h-4 w-4" />
            Compose montage
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Compose at least two reels (open a reel and hit Compose) to stitch
            them into one longer montage video.
          </p>
        </CardContent>
      </Card>
    );
  }

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <Card className="border-dashed">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Layers className="h-4 w-4 text-primary" />
          Compose montage
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Pick 2+ composed reels and stitch them into one long-form video with
          transitions. Each reel keeps its own captions, music, and effects.
        </p>
        <div className="flex flex-wrap gap-2">
          {candidateReels.map((r) => (
            <button
              key={r.id}
              onClick={() => toggle(r.id)}
              className={
                'rounded-md border px-3 py-1.5 text-xs transition ' +
                (selected.has(r.id)
                  ? 'border-primary bg-primary/15 text-foreground'
                  : 'border-border text-muted-foreground hover:border-muted-foreground/60')
              }
            >
              #{r.project_rank} · {r.duration_sec.toFixed(0)}s
            </button>
          ))}
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Title</Label>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>Transition: {transition[0].toFixed(2)}s</Label>
            <Slider value={transition} min={0} max={2} step={0.1} onValueChange={setTransition} />
          </div>
        </div>
        {createMontage.error ? (
          <Alert variant="destructive">
            <AlertDescription>{humanMessage(createMontage.error)}</AlertDescription>
          </Alert>
        ) : null}
        <div className="flex items-center gap-3">
          <Button
            onClick={() =>
              createMontage.mutate({
                reel_ids: Array.from(selected),
                title: title || 'Long montage',
                transition_duration_sec: transition[0],
              })
            }
            disabled={selected.size < 2 || createMontage.isPending || !!jobId}
          >
            <Film className="h-4 w-4" />
            {createMontage.isPending
              ? 'Queueing…'
              : `Compose montage (${selected.size} reels)`}
          </Button>
          <span className="text-xs text-muted-foreground">
            {selected.size < 2
              ? 'Select at least 2 reels'
              : `Will produce ~${candidateReels
                  .filter((r) => selected.has(r.id))
                  .reduce((acc, r) => acc + r.duration_sec, 0)
                  .toFixed(0)}s of output`}
          </span>
        </div>
        {jobId ? (
          <JobProgress
            jobId={jobId}
            variant="compose"
            onDone={() => {
              setJobId(null);
              onCreated();
              void qc.invalidateQueries({ queryKey: ['project-montages', projectId] });
            }}
            onFail={() => setJobId(null)}
          />
        ) : null}
      </CardContent>
    </Card>
  );
}

function ThumbStrip({ asset_id, scene_indices }: { asset_id: string; scene_indices: number[] }) {
  const picks = pickPreview(scene_indices);
  return (
    <div className="flex gap-1">
      {picks.map((i) => (
        <img
          key={i}
          alt=""
          src={`${API_BASE}/api/v1/assets/${asset_id}/thumbnails/${i}`}
          className="h-14 w-20 rounded-sm object-cover bg-muted"
          loading="lazy"
          onError={(e) => {
            // Hide the broken-image icon; the muted background reads as a
            // placeholder tile.
            (e.target as HTMLImageElement).style.visibility = 'hidden';
          }}
        />
      ))}
    </div>
  );
}

function pickPreview(indices: number[]): number[] {
  if (indices.length === 0) {
    // Montage reels carry no scene_indices; fall back to the first thumbnail.
    return [0];
  }
  if (indices.length <= 3) return indices;
  const mid = Math.floor(indices.length / 2);
  return [indices[0], indices[mid], indices[indices.length - 1]];
}
