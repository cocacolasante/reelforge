'use client';

import * as React from 'react';
import Link from 'next/link';
import { useQueryClient } from '@tanstack/react-query';
import { Download, Film, Play, RotateCcw, Sparkles, Upload, Wand2 } from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { JobProgress } from '@/components/app/job-progress';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
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
  usePublications,
  usePublishReel,
  useReel,
  useSocialAccounts,
} from '@/lib/api/hooks';

const PRESETS = [
  { id: 'mp4_h264_social', label: 'MP4 · H.264 (social)', description: 'Instagram / TikTok / Shorts', ratio: 0.6 },
  { id: 'mp4_h265_hq', label: 'MP4 · H.265 HQ', description: 'Modern distribution', ratio: 0.35 },
  { id: 'mov_prores_422', label: 'MOV · ProRes 422', description: 'Editorial (Standard)', ratio: 8.0 },
  { id: 'mov_prores_hq', label: 'MOV · ProRes HQ', description: 'Editorial (HQ)', ratio: 12.0 },
] as const;

// Mirror of TRANSITION_BY_MOOD / LUT_BY_MOOD in packages/core/reelforge_core/compose/auto.py.
// Used purely to preview the AI's planned picks; the server is the source of truth.
const TRANSITION_BY_MOOD: Record<string, string> = {
  calm: 'fade',
  tense: 'fade',
  joyful: 'dissolve',
  somber: 'fadeblack',
  energetic: 'slideleft',
  mysterious: 'fadeblack',
  romantic: 'dissolve',
  triumphant: 'slideleft',
  melancholic: 'fadeblack',
  neutral: 'fade',
};
const LUT_BY_MOOD: Record<string, string | null> = {
  calm: 'warm',
  tense: 'cinematic',
  joyful: 'vivid',
  somber: 'cool',
  energetic: 'vivid',
  mysterious: 'cool',
  romantic: 'warm',
  triumphant: 'cinematic',
  melancholic: 'cool',
  neutral: null,
};

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
  const queryClient = useQueryClient();
  const reel = useReel(reelId);
  const exportsQ = useExports(reelId);
  const music = useMusicLibrary();
  const compose = useEnqueueCompose();
  const [composeJobId, setComposeJobId] = React.useState<string | null>(null);

  // Local compose-config state (intentionally not persisted across reload).
  const [smart, setSmart] = React.useState(true);
  const [aspect, setAspect] = React.useState<'9:16' | '16:9' | '1:1'>('9:16');
  const [captionMode, setCaptionMode] = React.useState<'off' | 'static' | 'karaoke'>('karaoke');
  const [transition, setTransition] = React.useState('fade');
  const [transitionDur, setTransitionDur] = React.useState<number[]>([0.4]);
  const [musicTrack, setMusicTrack] = React.useState<string>('__auto__');
  const [noEffects, setNoEffects] = React.useState(false);
  const [crf, setCrf] = React.useState<number[]>([18]);
  const [quality, setQuality] = React.useState<'draft' | 'standard' | 'high'>('standard');
  // Max output duration (in seconds). `null` means "no cap" — render the full reel.
  // Mapped to `trim_end_offset_sec = reel_duration - maxDuration` on submit.
  const [maxDuration, setMaxDuration] = React.useState<number[] | null>(null);

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
  if (!reel.data) {
    return <div className="container py-10"><div className="h-8 w-48 animate-pulse rounded bg-card/40" /></div>;
  }
  const r = reel.data;
  const previewSrc = r.mezzanine_ready ? `${API_BASE}/api/v1/reels/${r.id}/preview` : null;
  const mezzanineBytesGuess = 1_500_000; // worst-case default for size estimates before we know
  const cappedDuration = maxDuration?.[0] ?? r.duration_sec;
  const trimEndOffset = Math.max(0, r.duration_sec - cappedDuration);
  // Only send trim keys the user actually set — the API falls back to the
  // reel's saved trim offsets for missing keys, so sending explicit zeros
  // would silently wipe a trim saved via PATCH /reels/{id}/trim.
  const trimOffsets: Record<string, number> =
    maxDuration !== null ? { trim_end_offset_sec: trimEndOffset } : {};

  const triggerCompose = async () => {
    const baseConfig = smart
      ? {
          aspect,
          target_fps: 30,
          video_crf: crf[0],
          quality,
          captions: { mode: captionMode },
          smart_mode: true,
          transition: { kind: 'auto', duration_sec: 0.4 },
          effects: {
            ken_burns_on_low_energy: true,
            unsharp: true,
            lut: 'auto',
          },
          music_track_id: null,
          no_music: false,
        }
      : {
          aspect,
          target_fps: 30,
          video_crf: crf[0],
          quality,
          captions: { mode: captionMode },
          smart_mode: false,
          transition: { kind: transition, duration_sec: transitionDur[0] },
          effects: {
            ken_burns_on_low_energy: !noEffects,
            unsharp: !noEffects,
            lut: null,
          },
          music_track_id: musicTrack === '__auto__' ? null : musicTrack,
          no_music: musicTrack === '__none__',
        };
    const config = { ...baseConfig, ...trimOffsets };
    try {
      const job = await compose.mutateAsync({ reelId, config });
      setComposeJobId(job.id);
    } catch {
      /* surfaced inline */
    }
  };

  const plannedTransition = TRANSITION_BY_MOOD[r.suggested_mood] ?? 'fade';
  const plannedLut = LUT_BY_MOOD[r.suggested_mood] ?? null;

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
                    // The reels list and export grid key off compose state too.
                    void queryClient.invalidateQueries({
                      queryKey: ['project-reels', projectId],
                    });
                    void queryClient.invalidateQueries({
                      queryKey: ['exports', reelId],
                    });
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
            <>
              <ExportList
                reelId={reelId}
                mezzanineBytesGuess={mezzanineBytesGuess}
                existing={exportsQ.data?.exports ?? []}
              />
              <PublishPanel
                reelId={reelId}
                defaultTitle={r.title}
                defaultDescription={r.hook}
                socialExportReady={(exportsQ.data?.exports ?? []).some(
                  (e) => e.preset_id === 'mp4_h264_social' && e.output_path,
                )}
              />
            </>
          ) : null}
        </div>

        {/* ===== Right pane: compose config ===== */}
        <Card>
          <CardHeader>
            <CardTitle>Compose</CardTitle>
          </CardHeader>
          <CardContent className="space-y-5">
            {/* Smart toggle */}
            <section className="rounded-md border border-primary/30 bg-primary/5 p-3">
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  checked={smart}
                  onChange={(e) => setSmart(e.target.checked)}
                  className="mt-1 h-4 w-4 accent-primary"
                />
                <div className="flex-1">
                  <div className="flex items-center gap-1.5 text-sm font-medium">
                    <Sparkles className="h-3.5 w-3.5 text-primary" />
                    AI auto-direction
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Pick transitions, music, color grade & effects from the reel&apos;s
                    mood ({r.suggested_mood}).
                  </p>
                </div>
              </label>
              {smart ? (
                <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 border-t border-primary/20 pt-3 text-xs">
                  <span className="text-muted-foreground">Transition</span>
                  <span className="font-mono">{plannedTransition}</span>
                  <span className="text-muted-foreground">Color grade</span>
                  <span className="font-mono">{plannedLut ?? 'none'}</span>
                  <span className="text-muted-foreground">Music</span>
                  <span className="font-mono">auto-match ({r.suggested_mood})</span>
                  <span className="text-muted-foreground">Effects</span>
                  <span className="font-mono">Ken Burns + unsharp</span>
                </div>
              ) : null}
            </section>

            {/* Max length — hidden for reels too short to meaningfully cap
                (Radix Slider misbehaves when min > max). */}
            {r.duration_sec > 6 ? (
            <section className="space-y-2">
              <div className="flex items-baseline justify-between">
                <Label>
                  Max length · {formatDuration(cappedDuration)}
                </Label>
                {cappedDuration < r.duration_sec ? (
                  <button
                    onClick={() => setMaxDuration(null)}
                    className="text-xs text-muted-foreground hover:text-foreground"
                  >
                    reset
                  </button>
                ) : null}
              </div>
              <Slider
                value={maxDuration ?? [r.duration_sec]}
                min={5}
                max={r.duration_sec}
                step={1}
                onValueChange={(v) => setMaxDuration(v as number[])}
              />
              <p className="text-xs text-muted-foreground">
                Full reel is {r.duration_sec.toFixed(1)}s. Lower this to render a
                shorter clip from the start of the reel.
              </p>
            </section>
            ) : null}

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

            {!smart ? (
              <>
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
              </>
            ) : null}

            {/* Quality */}
            <section className="space-y-2">
              <Label>Render quality</Label>
              <div className="flex gap-2">
                {([
                  ['draft', 'Draft'],
                  ['standard', 'Standard'],
                  ['high', 'High'],
                ] as const).map(([key, label]) => (
                  <button
                    key={key}
                    onClick={() => setQuality(key)}
                    className={
                      'rounded-md border px-3 py-1 text-sm transition ' +
                      (quality === key
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border text-muted-foreground hover:border-muted-foreground/60')
                    }
                  >
                    {label}
                  </button>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                Draft renders fastest; High uses slower, higher-fidelity encoding
                for final delivery.
              </p>
            </section>

            {/* Advanced */}
            <section className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">
                Advanced (CRF): {crf[0]}
              </Label>
              <Slider value={crf} min={15} max={28} step={1} onValueChange={setCrf} />
              <p className="text-xs text-muted-foreground">
                Lower CRF = larger, higher quality. 18 = auto (quality preset
                decides); other values override the preset.
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
  const queryClient = useQueryClient();
  const [exportJob, setExportJob] = React.useState<{ id: string; presetId: string } | null>(null);

  const onExportSettled = () => {
    setExportJob(null);
    // Refetch so the Download button appears without a manual reload.
    void queryClient.invalidateQueries({ queryKey: ['exports', reelId] });
  };

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
                      onDone={onExportSettled}
                      onFail={onExportSettled}
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

// ---------- Publish (YouTube) ----------

function PublishPanel({
  reelId,
  defaultTitle,
  defaultDescription,
  socialExportReady,
}: {
  reelId: string;
  defaultTitle: string;
  defaultDescription: string;
  socialExportReady: boolean;
}) {
  const accounts = useSocialAccounts();
  const pubs = usePublications(reelId);
  const publish = usePublishReel();
  const queryClient = useQueryClient();
  const [title, setTitle] = React.useState(defaultTitle);
  const [description, setDescription] = React.useState(defaultDescription);
  const [privacy, setPrivacy] = React.useState<'private' | 'unlisted' | 'public'>('private');
  const [publishJobId, setPublishJobId] = React.useState<string | null>(null);

  const youtube = accounts.data?.accounts.find((a) => a.platform === 'youtube');
  const publications = pubs.data?.publications ?? [];

  const onSettled = () => {
    setPublishJobId(null);
    void queryClient.invalidateQueries({ queryKey: ['publications', reelId] });
  };

  const trigger = async () => {
    try {
      const job = await publish.mutateAsync({
        reelId,
        body: {
          platform: 'youtube',
          preset_id: 'mp4_h264_social',
          title,
          description,
          privacy,
        },
      });
      setPublishJobId(job.id);
    } catch {
      /* surfaced inline */
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Publish</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!youtube ? (
          <div className="space-y-2">
            <p className="text-sm text-muted-foreground">
              Connect your YouTube channel to publish this reel directly.
            </p>
            <a href={`${API_BASE}/api/v1/social/youtube/connect`}>
              <Button variant="secondary" size="sm">
                Connect YouTube
              </Button>
            </a>
            <p className="text-xs text-muted-foreground">
              Requires GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in your .env —
              see docs/publishing.md.
            </p>
          </div>
        ) : (
          <>
            <p className="text-xs text-muted-foreground">
              Publishing as{' '}
              <span className="font-medium text-foreground">
                {youtube.display_name ?? 'YouTube'}
              </span>{' '}
              · uses the MP4 (social) export
            </p>
            {!socialExportReady ? (
              <Alert>
                <AlertDescription>
                  Export “MP4 · H.264 (social)” first — publishing uploads that
                  file.
                </AlertDescription>
              </Alert>
            ) : null}
            <div className="space-y-1.5">
              <Label>Title</Label>
              <Input
                value={title}
                maxLength={100}
                onChange={(e) => setTitle(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>Description</Label>
              <textarea
                value={description}
                rows={3}
                maxLength={4900}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              />
            </div>
            <div className="space-y-1.5">
              <Label>Visibility</Label>
              <div className="flex gap-2">
                {(['private', 'unlisted', 'public'] as const).map((p) => (
                  <button
                    key={p}
                    onClick={() => setPrivacy(p)}
                    className={
                      'rounded-md border px-3 py-1 text-sm capitalize transition ' +
                      (privacy === p
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border text-muted-foreground hover:border-muted-foreground/60')
                    }
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
            {publish.error ? (
              <Alert variant="destructive">
                <AlertDescription>{humanMessage(publish.error)}</AlertDescription>
              </Alert>
            ) : null}
            {publishJobId ? (
              <JobProgress
                jobId={publishJobId}
                variant="publish"
                onDone={onSettled}
                onFail={onSettled}
              />
            ) : (
              <Button
                onClick={trigger}
                disabled={publish.isPending || !socialExportReady || !title.trim()}
              >
                <Upload className="h-4 w-4" />
                {publish.isPending ? 'Starting…' : 'Publish to YouTube'}
              </Button>
            )}
          </>
        )}

        {publications.length > 0 ? (
          <div className="space-y-2 border-t pt-3">
            {publications.map((p) => (
              <div
                key={p.id}
                className="flex items-center justify-between gap-3 text-sm"
              >
                <div className="min-w-0">
                  <div className="truncate">{p.title}</div>
                  <div className="text-xs text-muted-foreground">
                    {p.platform} · {p.privacy} ·{' '}
                    {p.status === 'failed' ? (
                      <span className="text-destructive">{p.error_message ?? 'failed'}</span>
                    ) : (
                      p.status
                    )}
                  </div>
                </div>
                {p.video_url ? (
                  <a
                    href={p.video_url}
                    target="_blank"
                    rel="noreferrer"
                    className="shrink-0"
                  >
                    <Button variant="secondary" size="sm">
                      View
                    </Button>
                  </a>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
