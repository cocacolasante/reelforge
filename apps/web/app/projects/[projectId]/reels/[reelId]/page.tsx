'use client';

import * as React from 'react';
import Link from 'next/link';
import { Download, Film, Play, RotateCcw, Wand2 } from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { JobProgress } from '@/components/app/job-progress';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Progress } from '@/components/ui/progress';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Slider } from '@/components/ui/slider';
import { API_BASE } from '@/lib/api/client';
import { humanMessage } from '@/lib/api/errors';
import { formatBytes, formatDuration, formatTimestamp } from '@/lib/format';
import {
  useEnqueueCompose,
  useEnqueueExport,
  useExports,
  useMusicLibrary,
  useReel,
} from '@/lib/api/hooks';

const PRESETS = [
  { id: 'mp4_h264_social', label: 'MP4 · H.264 (social)', description: 'Instagram / TikTok / Shorts', ratio: 0.6 },
  { id: 'mp4_h265_hq', label: 'MP4 · H.265 HQ', description: 'Modern distribution', ratio: 0.35 },
  { id: 'mov_prores_422', label: 'MOV · ProRes 422', description: 'Editorial (Standard)', ratio: 8.0 },
  { id: 'mov_prores_hq', label: 'MOV · ProRes HQ', description: 'Editorial (HQ)', ratio: 12.0 },
] as const;

export default function ReelDetailPage({
  params,
}: {
  params: { projectId: string; reelId: string };
}) {
  return (
    <AppShell>
      <Body projectId={params.projectId} reelId={params.reelId} />
    </AppShell>
  );
}

function Body({ projectId, reelId }: { projectId: string; reelId: string }) {
  const reel = useReel(reelId);
  const exportsQ = useExports(reelId);
  const music = useMusicLibrary();
  const compose = useEnqueueCompose();
  const [composeJobId, setComposeJobId] = React.useState<string | null>(null);

  // Local compose-config state (intentionally not persisted across reload).
  const [aspect, setAspect] = React.useState<'9:16' | '16:9' | '1:1'>('9:16');
  const [captionMode, setCaptionMode] = React.useState<'off' | 'static' | 'karaoke'>('static');
  const [transition, setTransition] = React.useState('fade');
  const [transitionDur, setTransitionDur] = React.useState<number[]>([0.4]);
  const [musicTrack, setMusicTrack] = React.useState<string>('__auto__');
  const [noEffects, setNoEffects] = React.useState(false);
  const [crf, setCrf] = React.useState<number[]>([18]);

  if (reel.isLoading) {
    return <div className="container py-10"><div className="h-8 w-48 animate-pulse rounded bg-card/40" /></div>;
  }
  if (reel.error) {
    return (
      <div className="container py-10">
        <Alert variant="destructive"><AlertDescription>{humanMessage(reel.error)}</AlertDescription></Alert>
      </div>
    );
  }
  const r = reel.data!;
  const previewSrc = r.mezzanine_ready ? `${API_BASE}/api/v1/reels/${r.id}/preview` : null;
  const mezzanineBytesGuess = 1_500_000; // worst-case default for size estimates before we know

  const triggerCompose = async () => {
    try {
      const job = await compose.mutateAsync({
        reelId,
        config: {
          aspect,
          target_fps: 30,
          video_crf: crf[0],
          captions: { mode: captionMode },
          transition: { kind: transition, duration_sec: transitionDur[0] },
          effects: {
            ken_burns_on_low_energy: !noEffects,
            unsharp: !noEffects,
            lut: null,
          },
          music_track_id: musicTrack === '__auto__' ? null : musicTrack,
          no_music: musicTrack === '__none__',
        },
      });
      setComposeJobId(job.id);
    } catch {
      /* surfaced inline */
    }
  };

  return (
    <div className="container space-y-4 py-6">
      <header>
        <Link
          href={`/projects/${projectId}/reels`}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          ← All reels
        </Link>
        <div className="mt-1 flex flex-wrap items-baseline gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{r.title}</h1>
          <Badge variant="secondary">rank {r.rank}</Badge>
          <Badge variant="muted">{r.suggested_mood}</Badge>
          <span className="text-sm text-muted-foreground">
            score {r.overall_score.toFixed(0)} · {formatDuration(r.start_sec)} – {formatDuration(r.end_sec)}
          </span>
        </div>
        <p className="mt-1 max-w-2xl text-sm italic text-muted-foreground">{r.hook}</p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        {/* ===== Left pane: preview ===== */}
        <div className="space-y-3">
          <Card>
            <CardHeader>
              <CardTitle>Preview</CardTitle>
            </CardHeader>
            <CardContent>
              {composeJobId ? (
                <JobProgress
                  jobId={composeJobId}
                  variant="compose"
                  onDone={() => {
                    setComposeJobId(null);
                    void reel.refetch();
                  }}
                  onFail={() => setComposeJobId(null)}
                />
              ) : previewSrc ? (
                <video
                  controls
                  muted
                  playsInline
                  className="w-full rounded-md bg-black"
                  preload="metadata"
                  src={previewSrc}
                />
              ) : (
                <div className="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed bg-card/40 py-16 text-center">
                  <Film className="h-8 w-8 text-muted-foreground" />
                  <p className="text-sm text-muted-foreground">
                    Compose this reel to preview the rendered mezzanine.
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          {r.mezzanine_ready && !composeJobId ? (
            <ExportList
              reelId={reelId}
              mezzanineBytesGuess={mezzanineBytesGuess}
              existing={exportsQ.data?.exports ?? []}
            />
          ) : null}
        </div>

        {/* ===== Right pane: compose config ===== */}
        <Card>
          <CardHeader>
            <CardTitle>Compose</CardTitle>
          </CardHeader>
          <CardContent className="space-y-5">
            {/* Aspect */}
            <section className="space-y-2">
              <Label>Aspect</Label>
              <div className="flex gap-2">
                {(['9:16', '16:9', '1:1'] as const).map((a) => (
                  <button
                    key={a}
                    onClick={() => setAspect(a)}
                    className={
                      'rounded-md border px-3 py-1 text-sm transition ' +
                      (aspect === a
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border text-muted-foreground hover:border-muted-foreground/60')
                    }
                  >
                    {a}
                  </button>
                ))}
              </div>
            </section>

            {/* Transition */}
            <section className="space-y-2">
              <Label>Transition</Label>
              <Select value={transition} onValueChange={setTransition}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['fade', 'fadeblack', 'slideleft', 'wipeleft', 'dissolve', 'cut'].map((t) => (
                    <SelectItem key={t} value={t}>
                      {t}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {transition !== 'cut' ? (
                <div className="pt-2 space-y-1">
                  <Label className="text-xs">Duration: {transitionDur[0].toFixed(2)}s</Label>
                  <Slider
                    value={transitionDur}
                    min={0}
                    max={1.5}
                    step={0.05}
                    onValueChange={setTransitionDur}
                  />
                </div>
              ) : null}
            </section>

            {/* Captions */}
            <section className="space-y-2">
              <Label>Captions</Label>
              <div className="flex gap-2">
                {(['off', 'static', 'karaoke'] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setCaptionMode(m)}
                    className={
                      'rounded-md border px-3 py-1 text-sm transition ' +
                      (captionMode === m
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border text-muted-foreground hover:border-muted-foreground/60')
                    }
                  >
                    {m}
                  </button>
                ))}
              </div>
            </section>

            {/* Music */}
            <section className="space-y-2">
              <Label>Music</Label>
              <Select value={musicTrack} onValueChange={setMusicTrack}>
                <SelectTrigger>
                  <SelectValue placeholder="auto" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__auto__">Auto (match mood)</SelectItem>
                  <SelectItem value="__none__">No music</SelectItem>
                  {music.data?.tracks.map((t) => (
                    <SelectItem key={t.id} value={t.id}>
                      {t.id} · {t.mood} {t.bpm ? `· ${t.bpm} BPM` : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </section>

            {/* Effects */}
            <section className="flex items-center justify-between">
              <Label className="text-sm">Effects (Ken Burns + unsharp)</Label>
              <input
                type="checkbox"
                checked={!noEffects}
                onChange={(e) => setNoEffects(!e.target.checked)}
                className="h-4 w-4 accent-primary"
              />
            </section>

            {/* Advanced */}
            <section className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">
                Quality (CRF): {crf[0]}
              </Label>
              <Slider value={crf} min={15} max={28} step={1} onValueChange={setCrf} />
              <p className="text-xs text-muted-foreground">
                Lower CRF = larger, higher quality. 18 is the default.
              </p>
            </section>

            {compose.error ? (
              <Alert variant="destructive">
                <AlertDescription>{humanMessage(compose.error)}</AlertDescription>
              </Alert>
            ) : null}

            <Button
              size="lg"
              className="w-full"
              onClick={triggerCompose}
              disabled={compose.isPending || !!composeJobId}
            >
              <Wand2 className="h-4 w-4" />
              {composeJobId ? 'Composing…' : r.mezzanine_ready ? 'Re-compose' : 'Compose reel'}
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

// ---------- Export list ----------

function ExportList({
  reelId,
  mezzanineBytesGuess,
  existing,
}: {
  reelId: string;
  mezzanineBytesGuess: number;
  existing: Array<{ id: string; preset_id: string; output_path: string | null; file_size_bytes: number | null; created_at: string }>;
}) {
  const enqueue = useEnqueueExport();
  const [exportJob, setExportJob] = React.useState<{ id: string; presetId: string } | null>(null);

  const trigger = async (presetId: string, force = false) => {
    try {
      const job = await enqueue.mutateAsync({ reelId, presetId, force });
      setExportJob({ id: job.id, presetId });
    } catch {
      /* surfaced inline */
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Export</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {PRESETS.map((preset) => {
          const e = existing.find((x) => x.preset_id === preset.id);
          const isRunning = exportJob?.presetId === preset.id;
          return (
            <div
              key={preset.id}
              className="flex items-center justify-between gap-3 rounded-md border bg-card/60 p-3"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{preset.label}</span>
                  {e ? <Badge variant="muted">ready</Badge> : null}
                </div>
                <div className="text-xs text-muted-foreground">
                  {preset.description}
                  {' · '}
                  {e?.file_size_bytes
                    ? formatBytes(e.file_size_bytes)
                    : `~${formatBytes(Math.round(mezzanineBytesGuess * preset.ratio))}`}
                  {e ? ` · ${formatTimestamp(e.created_at)}` : null}
                </div>
                {isRunning ? (
                  <div className="mt-2 w-full">
                    <JobProgress
                      jobId={exportJob!.id}
                      variant="export"
                      onDone={() => setExportJob(null)}
                      onFail={() => setExportJob(null)}
                    />
                  </div>
                ) : null}
              </div>
              <div className="flex shrink-0 gap-2">
                {e?.output_path ? (
                  <a href={`${API_BASE}/api/v1/exports/${e.id}/download`} download>
                    <Button variant="secondary" size="sm">
                      <Download className="h-4 w-4" />
                      Download
                    </Button>
                  </a>
                ) : null}
                <Button
                  variant={e ? 'outline' : 'default'}
                  size="sm"
                  onClick={() => trigger(preset.id, !!e)}
                  disabled={isRunning}
                >
                  {e ? (
                    <>
                      <RotateCcw className="h-4 w-4" />
                      Re-export
                    </>
                  ) : (
                    <>
                      <Play className="h-4 w-4" />
                      Export
                    </>
                  )}
                </Button>
              </div>
            </div>
          );
        })}
        {enqueue.error ? (
          <Alert variant="destructive">
            <AlertTitle>Export error</AlertTitle>
            <AlertDescription>{humanMessage(enqueue.error)}</AlertDescription>
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  );
}
