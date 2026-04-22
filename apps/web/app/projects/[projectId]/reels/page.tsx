'use client';

import * as React from 'react';
import Link from 'next/link';
import { ArrowRight } from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { useProject, useProjectAssets, useReelsForAsset } from '@/lib/api/hooks';
import { humanMessage } from '@/lib/api/errors';
import { formatDuration } from '@/lib/format';
import { API_BASE } from '@/lib/api/client';

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
  const assets = useProjectAssets(projectId);
  const asset = assets.data?.assets[0];
  const reels = useReelsForAsset(asset?.id);

  if (project.isLoading) {
    return <div className="container py-10"><div className="h-8 w-40 animate-pulse rounded bg-card/40" /></div>;
  }
  if (project.error) {
    return <div className="container py-10"><Alert variant="destructive"><AlertDescription>{humanMessage(project.error)}</AlertDescription></Alert></div>;
  }
  if (!asset) {
    return (
      <div className="container py-10">
        <Alert>
          <AlertTitle>Upload a source first</AlertTitle>
          <AlertDescription>
            <Link className="underline" href={`/projects/${projectId}`}>Return to project</Link>
          </AlertDescription>
        </Alert>
      </div>
    );
  }
  if (reels.isLoading) {
    return (
      <div className="container py-10 space-y-3">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="h-24 animate-pulse rounded-lg border bg-card/40" />
        ))}
      </div>
    );
  }
  if (reels.error) {
    return (
      <div className="container py-10">
        <Alert variant="destructive"><AlertDescription>{humanMessage(reels.error)}</AlertDescription></Alert>
      </div>
    );
  }
  const data = reels.data!;
  if (data.reels.length === 0) {
    return (
      <div className="container py-10">
        <Alert>
          <AlertTitle>No 30-60s spans were found</AlertTitle>
          <AlertDescription>
            Try lowering the scene threshold or uploading a longer source.
            {' '}
            <Link className="underline" href={`/projects/${projectId}`}>Go back</Link>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <div className="container space-y-4 py-8">
      <header>
        <Link href={`/projects/${projectId}`} className="text-xs text-muted-foreground hover:text-foreground">
          ← {project.data?.name}
        </Link>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          {data.reels.length} reels ranked
        </h1>
      </header>
      <div className="space-y-3">
        {data.reels.map((r) => (
          <Link
            key={r.id}
            href={`/projects/${projectId}/reels/${r.id}`}
            className="block rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Card className="transition-colors hover:border-primary/60">
              <CardContent className="flex items-center gap-5 py-4">
                <div className="flex shrink-0 items-center justify-center rounded-md bg-primary/20 px-3 text-xl font-bold text-primary">
                  {r.rank}
                </div>
                <ThumbStrip asset_id={asset.id} scene_indices={r.scene_indices} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <h3 className="truncate font-semibold">{r.title}</h3>
                    <Badge variant="secondary">{r.suggested_mood}</Badge>
                  </div>
                  <p className="line-clamp-2 text-sm text-muted-foreground">{r.hook}</p>
                  <p className="mt-1 text-xs text-muted-foreground/80">
                    {formatDuration(r.start_sec)} – {formatDuration(r.end_sec)} · {r.duration_sec.toFixed(1)}s
                  </p>
                </div>
                <div className="flex w-28 flex-col items-end gap-1">
                  <div className="text-2xl font-semibold leading-none tabular-nums">
                    {r.overall_score.toFixed(0)}
                  </div>
                  <div className="text-xs text-muted-foreground">score</div>
                  <ScoreBars scores={r.scores} />
                </div>
                <ArrowRight className="h-4 w-4 opacity-50" />
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
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
        />
      ))}
    </div>
  );
}

function pickPreview(indices: number[]): number[] {
  if (indices.length <= 3) return indices;
  const mid = Math.floor(indices.length / 2);
  return [indices[0], indices[mid], indices[indices.length - 1]];
}

function ScoreBars({ scores }: { scores: { narrative_coherence: number; hook_strength: number; emotional_payoff: number; standalone_clarity: number } }) {
  const parts: Array<[string, number]> = [
    ['hook', scores.hook_strength],
    ['nar', scores.narrative_coherence],
    ['emo', scores.emotional_payoff],
    ['clr', scores.standalone_clarity],
  ];
  return (
    <div className="flex items-center gap-1">
      {parts.map(([k, v]) => (
        <div key={k} title={`${k}: ${v}`} className="flex flex-col items-center gap-0.5">
          <div className="h-8 w-1.5 rounded-full bg-muted overflow-hidden flex flex-col-reverse">
            <div style={{ height: `${v}%` }} className="bg-primary" />
          </div>
        </div>
      ))}
    </div>
  );
}
