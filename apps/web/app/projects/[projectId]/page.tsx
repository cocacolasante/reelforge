'use client';

import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowRight, Sparkles, Wand2 } from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { Uploader } from '@/components/app/uploader';
import { JobProgress } from '@/components/app/job-progress';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
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
import {
  useAnalysis,
  useEnqueueAnalyze,
  useEnqueueSelect,
  useProject,
  useProjectAssets,
  useReelsForAsset,
} from '@/lib/api/hooks';
import { humanMessage } from '@/lib/api/errors';
import { formatBytes, formatDuration } from '@/lib/format';
import { APIError } from '@/lib/api/errors';

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
  const project = useProject(projectId);
  const assets = useProjectAssets(projectId);
  const primaryAsset = assets.data?.assets[0];

  const analysis = useAnalysis(primaryAsset?.id, !!primaryAsset);
  const reels = useReelsForAsset(primaryAsset?.id);

  const [analyzeJobId, setAnalyzeJobId] = React.useState<string | null>(null);
  const [selectJobId, setSelectJobId] = React.useState<string | null>(null);

  if (project.isLoading) {
    return (
      <div className="container py-10">
        <div className="h-8 w-48 animate-pulse rounded bg-card/40" />
      </div>
    );
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
        {reels.data && reels.data.reels.length > 0 ? (
          <Button onClick={() => router.push(`/projects/${projectId}/reels`)}>
            View reels
            <ArrowRight className="h-4 w-4" />
          </Button>
        ) : null}
      </header>

      {/* Zone A — Source */}
      <Card>
        <CardHeader>
          <CardTitle>Source</CardTitle>
        </CardHeader>
        <CardContent>
          {primaryAsset ? (
            <AssetSummary asset={primaryAsset} />
          ) : (
            <Uploader projectId={projectId} />
          )}
        </CardContent>
      </Card>

      {/* Zone B — Analysis */}
      <Card>
        <CardHeader>
          <CardTitle>Analysis</CardTitle>
        </CardHeader>
        <CardContent>
          {!primaryAsset ? (
            <p className="text-sm text-muted-foreground">
              Upload a source video to enable analysis.
            </p>
          ) : analysis.data ? (
            <AnalysisSummary analysis={analysis.data} />
          ) : analyzeJobId ? (
            <JobProgress
              jobId={analyzeJobId}
              variant="analyze"
              onDone={() => {
                setAnalyzeJobId(null);
                void analysis.refetch();
              }}
              onFail={() => setAnalyzeJobId(null)}
            />
          ) : (
            <AnalyzeTrigger
              assetId={primaryAsset.id}
              onQueued={(id) => setAnalyzeJobId(id)}
            />
          )}
        </CardContent>
      </Card>

      {/* Zone C — Selection */}
      <Card>
        <CardHeader>
          <CardTitle>Selection</CardTitle>
        </CardHeader>
        <CardContent>
          {!analysis.data ? (
            <p className="text-sm text-muted-foreground">
              Analyze the source first to unlock reel selection.
            </p>
          ) : reels.data && reels.data.reels.length > 0 ? (
            <div className="flex items-center justify-between text-sm">
              <span>
                {reels.data.reels.length} reel
                {reels.data.reels.length === 1 ? '' : 's'} ranked.
              </span>
              <Button
                size="sm"
                onClick={() => router.push(`/projects/${projectId}/reels`)}
              >
                Open reels
                <ArrowRight className="h-4 w-4" />
              </Button>
            </div>
          ) : selectJobId ? (
            <JobProgress
              jobId={selectJobId}
              variant="select"
              onDone={() => {
                setSelectJobId(null);
                void reels.refetch();
              }}
              onFail={() => setSelectJobId(null)}
            />
          ) : (
            <SelectTrigger_
              assetId={primaryAsset!.id}
              onQueued={(id) => setSelectJobId(id)}
              disabled={!primaryAsset}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function AssetSummary({ asset }: { asset: NonNullable<ReturnType<typeof useProjectAssets>['data']>['assets'][number] }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <div className="font-medium">{asset.original_filename}</div>
        <div className="text-sm text-muted-foreground">
          {formatDuration(asset.duration_sec)} • {asset.width}×{asset.height} •{' '}
          {asset.fps.toFixed(0)} fps • {formatBytes(asset.size_bytes)}
        </div>
      </div>
      <Badge variant="secondary">{asset.has_audio ? 'has audio' : 'silent'}</Badge>
    </div>
  );
}

function AnalyzeTrigger({
  assetId,
  onQueued,
}: {
  assetId: string;
  onQueued: (jobId: string) => void;
}) {
  const enqueue = useEnqueueAnalyze();
  const [model, setModel] = React.useState('base.en');
  const [threshold, setThreshold] = React.useState<number[]>([27]);

  const trigger = async () => {
    try {
      const job = await enqueue.mutateAsync({
        assetId,
        config: { whisper_model: model, scene_threshold: threshold[0] },
      });
      onQueued(job.id);
    } catch {
      /* surfaced via enqueue.error */
    }
  };

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>Whisper model</Label>
          <Select value={model} onValueChange={setModel}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {['base.en', 'small', 'medium', 'large-v3'].map((m) => (
                <SelectItem key={m} value={m}>
                  {m}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Scene threshold: {threshold[0]}</Label>
          <Slider value={threshold} min={10} max={50} step={1} onValueChange={setThreshold} />
          <p className="text-xs text-muted-foreground">
            Higher = fewer, longer scenes. Default 27.
          </p>
        </div>
      </div>
      {enqueue.error ? <ErrorInline err={enqueue.error} /> : null}
      <Button onClick={trigger} disabled={enqueue.isPending}>
        <Sparkles className="h-4 w-4" />
        {enqueue.isPending ? 'Starting…' : 'Analyze footage'}
      </Button>
    </div>
  );
}

function AnalysisSummary({
  analysis,
}: {
  analysis: NonNullable<ReturnType<typeof useAnalysis>['data']>;
}) {
  const wordCount =
    analysis.transcript && 'segments' in (analysis.transcript as object)
      ? ((analysis.transcript as { segments?: Array<{ words?: unknown[] }> }).segments ?? []).reduce(
          (acc, s) => acc + (s.words?.length ?? 0),
          0,
        )
      : 0;
  const moods = new Map<string, number>();
  for (const s of analysis.semantics) moods.set(s.mood, (moods.get(s.mood) ?? 0) + 1);
  const topMoods = [...moods.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4);
  return (
    <div className="space-y-3 text-sm">
      <div className="grid gap-2 sm:grid-cols-3">
        <Stat label="Scenes" value={analysis.scenes.length} />
        <Stat
          label="Transcript"
          value={
            analysis.transcript
              ? `${wordCount.toLocaleString()} words`
              : 'No speech'
          }
        />
        <Stat
          label="Duration"
          value={formatDuration(analysis.duration)}
        />
      </div>
      <div className="flex flex-wrap gap-1.5">
        {topMoods.map(([m, count]) => (
          <Badge key={m} variant="secondary">
            {m} ×{count}
          </Badge>
        ))}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border bg-card/60 p-3">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1 text-base font-medium">{value}</div>
    </div>
  );
}

function SelectTrigger_({
  assetId,
  onQueued,
  disabled,
}: {
  assetId: string;
  onQueued: (jobId: string) => void;
  disabled?: boolean;
}) {
  const enqueue = useEnqueueSelect();
  const [topK, setTopK] = React.useState<number[]>([10]);

  const trigger = async () => {
    try {
      const job = await enqueue.mutateAsync({
        assetId,
        config: { top_k: topK[0] },
      });
      onQueued(job.id);
    } catch {
      /* surfaced */
    }
  };

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label>Top reels to keep: {topK[0]}</Label>
        <Slider value={topK} min={3} max={20} step={1} onValueChange={setTopK} />
      </div>
      {enqueue.error ? <ErrorInline err={enqueue.error} /> : null}
      <Button onClick={trigger} disabled={disabled || enqueue.isPending}>
        <Wand2 className="h-4 w-4" />
        {enqueue.isPending ? 'Starting…' : 'Select top reels'}
      </Button>
    </div>
  );
}

function ErrorInline({ err }: { err: unknown }) {
  const isAlreadyRunning = err instanceof APIError && err.code === 'JOB_ALREADY_RUNNING';
  return (
    <Alert variant={isAlreadyRunning ? 'info' : 'destructive'}>
      <AlertDescription>{humanMessage(err)}</AlertDescription>
    </Alert>
  );
}
