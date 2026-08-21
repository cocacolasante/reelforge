'use client';

import * as React from 'react';
import Link from 'next/link';
import { useQueryClient } from '@tanstack/react-query';
import { Download, Film, Pencil, Play, RotateCcw, Sparkles, Upload, Wand2 } from 'lucide-react';
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
  useProjectPhotos,
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
  // Photo inserts: which project photos to weave in, where, and for how long.
  const [photoIds, setPhotoIds] = React.useState<string[]>([]);
  const [photoPlacement, setPhotoPlacement] =
    React.useState<'start' | 'end' | 'spread'>('end');
  const [photoSeconds, setPhotoSeconds] = React.useState<number[]>([3]);
  // When a saved edit exists it renders by default; this opts out per render.
  const [renderAiCut, setRenderAiCut] = React.useState(false);
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
    // Positions index the shot sequence: 0 = before the first clip,
    // sceneCount = after the last. "spread" distributes them between clips.
    const sceneCount = r.scene_indices.length;
    const photo_inserts = photoIds.map((assetId, i) => {
      let position = sceneCount; // end
      if (photoPlacement === 'start') position = 0;
      else if (photoPlacement === 'spread') {
        position = Math.max(
          1,
          Math.round(((i + 1) * sceneCount) / (photoIds.length + 1)),
        );
      }
      return {
        asset_id: assetId,
        position,
        duration_sec: photoSeconds[0],
        ken_burns: true,
      };
    });
    const config = {
      ...baseConfig,
      ...trimOffsets,
      photo_inserts,
      ...(r.has_edits && renderAiCut ? { ignore_edits: true } : {}),
    };
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
          {r.has_edits ? (
            <Badge variant="secondary" title="A saved edit overrides the AI cut">
              edited · {formatDuration(r.edited_duration_sec ?? r.duration_sec)}
            </Badge>
          ) : null}
          <Link href={`/projects/${projectId}/reels/${reelId}/edit`}>
            <Button size="sm" variant="outline">
              <Pencil className="h-4 w-4" />
              {r.has_edits ? 'Edit timeline' : 'Edit'}
            </Button>
          </Link>
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
                projectId={projectId}
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

            {r.has_edits ? (
              <Alert>
                <AlertTitle>This reel has edits</AlertTitle>
                <AlertDescription>
                  Rendering uses your edited timeline (shots, transitions, text).
                  Photos are managed in the editor.
                  <label className="mt-2 flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={renderAiCut}
                      onChange={(e) => setRenderAiCut(e.target.checked)}
                      className="h-3.5 w-3.5 accent-primary"
                    />
                    Render the original AI cut instead (keeps your edits saved)
                  </label>
                </AlertDescription>
              </Alert>
            ) : null}

            {/* Photos */}
            {!r.has_edits ? (
            <PhotoPicker
              projectId={projectId}
              selected={photoIds}
              onToggle={(id) =>
                setPhotoIds((prev) =>
                  prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
                )
              }
              placement={photoPlacement}
              onPlacement={setPhotoPlacement}
              seconds={photoSeconds}
              onSeconds={setPhotoSeconds}
            />
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

// ---------- Publish (YouTube / Instagram / TikTok) ----------

const PLATFORMS = [
  { id: 'youtube', label: 'YouTube' },
  { id: 'instagram', label: 'Instagram' },
  { id: 'tiktok', label: 'TikTok' },
] as const;
type PlatformId = (typeof PLATFORMS)[number]['id'];

function PublishPanel({
  projectId,
  reelId,
  defaultTitle,
  defaultDescription,
  socialExportReady,
}: {
  projectId: string;
  reelId: string;
  defaultTitle: string;
  defaultDescription: string;
  socialExportReady: boolean;
}) {
  const accounts = useSocialAccounts();
  const pubs = usePublications(reelId);
  const publish = usePublishReel();
  const queryClient = useQueryClient();
  const [platform, setPlatform] = React.useState<PlatformId>('youtube');
  const [title, setTitle] = React.useState(defaultTitle);
  const [description, setDescription] = React.useState(defaultDescription);
  const [privacy, setPrivacy] = React.useState<'private' | 'unlisted' | 'public'>('private');
  const [publishJobId, setPublishJobId] = React.useState<string | null>(null);
  const [channelId, setChannelId] = React.useState<string | null>(null);
  // Result of an OAuth connect round-trip, delivered via query params by the
  // API's callback redirect. Read once on mount, then strip from the URL.
  const [connectNotice, setConnectNotice] = React.useState<
    { kind: 'ok' | 'error'; text: string } | null
  >(null);

  React.useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get('connected');
    const err = params.get('youtube_error');
    if (connected) {
      setConnectNotice({ kind: 'ok', text: `Connected “${connected}”.` });
    } else if (err) {
      setConnectNotice({ kind: 'error', text: err });
    }
    if (connected || err) {
      params.delete('connected');
      params.delete('youtube_error');
      const qs = params.toString();
      window.history.replaceState(
        null,
        '',
        window.location.pathname + (qs ? `?${qs}` : ''),
      );
    }
  }, []);

  const connectHref = (p: PlatformId) =>
    `${API_BASE}/api/v1/social/${p}/connect?next=${encodeURIComponent(
      `/projects/${projectId}/reels/${reelId}`,
    )}`;

  const channels = (accounts.data?.accounts ?? []).filter(
    (a) => a.platform === platform,
  );
  // Auto-select when there's exactly one account on the active platform;
  // otherwise the user must pick explicitly.
  React.useEffect(() => {
    if (channels.length === 1 && channelId !== channels[0].id) {
      setChannelId(channels[0].id);
    } else if (channelId && !channels.some((c) => c.id === channelId)) {
      setChannelId(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform, channels.map((c) => c.id).join(',')]);

  const selectedChannel = channels.find((c) => c.id === channelId) ?? null;
  const publications = pubs.data?.publications ?? [];

  const onSettled = () => {
    setPublishJobId(null);
    void queryClient.invalidateQueries({ queryKey: ['publications', reelId] });
  };

  const disconnect = async (accountId: string) => {
    await fetch(`${API_BASE}/api/v1/social/accounts/${accountId}`, {
      method: 'DELETE',
    });
    void queryClient.invalidateQueries({ queryKey: ['social-accounts'] });
  };

  const trigger = async () => {
    if (!channelId) return;
    try {
      const job = await publish.mutateAsync({
        reelId,
        body: {
          platform,
          account_id: channelId,
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

  const platformLabel = PLATFORMS.find((p) => p.id === platform)!.label;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Publish</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {connectNotice ? (
          <Alert variant={connectNotice.kind === 'error' ? 'destructive' : 'info'}>
            <AlertTitle>
              {connectNotice.kind === 'error' ? "Couldn't connect account" : 'Account connected'}
            </AlertTitle>
            <AlertDescription>{connectNotice.text}</AlertDescription>
          </Alert>
        ) : null}

        {/* Platform tabs */}
        <div className="flex gap-2">
          {PLATFORMS.map((p) => (
            <button
              key={p.id}
              onClick={() => setPlatform(p.id)}
              className={
                'rounded-md border px-3 py-1 text-sm transition ' +
                (platform === p.id
                  ? 'border-primary bg-primary/10 text-foreground'
                  : 'border-border text-muted-foreground hover:border-muted-foreground/60')
              }
            >
              {p.label}
            </button>
          ))}
        </div>

        {channels.length === 0 ? (
          <div className="space-y-2">
            <p className="text-sm text-muted-foreground">
              Connect your {platformLabel} account to publish this reel directly.
            </p>
            <a href={connectHref(platform)}>
              <Button variant="secondary" size="sm">
                Connect {platformLabel}
              </Button>
            </a>
            <p className="text-xs text-muted-foreground">
              {platform === 'youtube'
                ? 'Requires GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in your .env. Multiple channels? Connect them one at a time.'
                : platform === 'instagram'
                ? 'Requires INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET in .env, an Instagram Business/Creator account, and the tunnel (REELFORGE_PUBLIC_MEDIA_BASE).'
                : 'Requires TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env.'}{' '}
              See docs/publishing.md.
            </p>
          </div>
        ) : (
          <>
            <div className="space-y-1.5">
              <Label>Account</Label>
              <div className="flex flex-wrap items-center gap-2">
                {channels.map((c) => (
                  <span key={c.id} className="inline-flex items-center">
                    <button
                      onClick={() => setChannelId(c.id)}
                      className={
                        'rounded-l-md border px-3 py-1 text-sm transition ' +
                        (channelId === c.id
                          ? 'border-primary bg-primary/10 text-foreground'
                          : 'border-border text-muted-foreground hover:border-muted-foreground/60')
                      }
                    >
                      {c.display_name ?? c.external_id}
                    </button>
                    <button
                      title="Disconnect this account"
                      onClick={() => void disconnect(c.id)}
                      className="rounded-r-md border border-l-0 border-border px-1.5 py-1 text-xs text-muted-foreground hover:text-destructive"
                    >
                      ×
                    </button>
                  </span>
                ))}
                <a
                  href={connectHref(platform)}
                  className="text-xs text-muted-foreground underline hover:text-foreground"
                >
                  + connect another
                </a>
              </div>
              {channels.length > 1 && !selectedChannel ? (
                <p className="text-xs text-amber-500">
                  Pick which account this video posts to.
                </p>
              ) : null}
            </div>

            {!socialExportReady ? (
              <Alert>
                <AlertDescription>
                  Export “MP4 · H.264 (social)” first — publishing uploads that
                  file.
                </AlertDescription>
              </Alert>
            ) : null}

            {platform !== 'tiktok' ? (
              <>
                <div className="space-y-1.5">
                  <Label>{platform === 'instagram' ? 'Caption (first line)' : 'Title'}</Label>
                  <Input
                    value={title}
                    maxLength={100}
                    onChange={(e) => setTitle(e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>{platform === 'instagram' ? 'Caption (rest)' : 'Description'}</Label>
                  <textarea
                    value={description}
                    rows={3}
                    maxLength={2000}
                    onChange={(e) => setDescription(e.target.value)}
                    className="w-full rounded-md border bg-background px-3 py-2 text-sm"
                  />
                </div>
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                TikTok uploads land in your <span className="font-medium text-foreground">TikTok inbox</span> —
                open the TikTok app, tap the notification, and add your caption
                there before posting. (Direct public posting needs TikTok’s app
                audit; the inbox flow works immediately.)
              </p>
            )}

            {platform === 'youtube' ? (
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
            ) : platform === 'instagram' ? (
              <p className="text-xs text-amber-500">
                Instagram Reels publish publicly to your profile immediately.
              </p>
            ) : null}

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
                disabled={
                  publish.isPending ||
                  !socialExportReady ||
                  (platform !== 'tiktok' && !title.trim()) ||
                  !selectedChannel
                }
              >
                <Upload className="h-4 w-4" />
                {publish.isPending
                  ? 'Starting…'
                  : selectedChannel
                  ? `${platform === 'tiktok' ? 'Send to' : 'Publish to'} ${selectedChannel.display_name ?? platformLabel}`
                  : `Publish to ${platformLabel}`}
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
                    {p.channel_title ?? p.platform} · {p.platform === 'youtube' ? `${p.privacy} · ` : ''}
                    {p.status === 'failed' ? (
                      <span className="text-destructive">{p.error_message ?? 'failed'}</span>
                    ) : p.status === 'done' && p.platform === 'tiktok' && !p.video_url ? (
                      'sent to TikTok inbox — finish in the app'
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

// ---------- Photo picker (compose panel) ----------

function PhotoPicker({
  projectId,
  selected,
  onToggle,
  placement,
  onPlacement,
  seconds,
  onSeconds,
}: {
  projectId: string;
  selected: string[];
  onToggle: (assetId: string) => void;
  placement: 'start' | 'end' | 'spread';
  onPlacement: (p: 'start' | 'end' | 'spread') => void;
  seconds: number[];
  onSeconds: (v: number[]) => void;
}) {
  const { photos } = useProjectPhotos(projectId);
  if (photos.length === 0) return null;

  return (
    <section className="space-y-2">
      <Label>
        Photos{selected.length > 0 ? ` · ${selected.length} selected` : ''}
      </Label>
      <div className="flex flex-wrap gap-2">
        {photos.map((p) => {
          const isOn = selected.includes(p.id);
          const order = selected.indexOf(p.id) + 1;
          return (
            <button
              key={p.id}
              onClick={() => onToggle(p.id)}
              title={p.original_filename}
              className={
                'relative h-16 w-16 overflow-hidden rounded-md border-2 transition ' +
                (isOn ? 'border-primary' : 'border-transparent opacity-60 hover:opacity-100')
              }
            >
              <img
                alt=""
                src={`${API_BASE}/api/v1/assets/${p.id}/photo`}
                className="h-full w-full object-cover"
                loading="lazy"
              />
              {isOn ? (
                <span className="absolute right-0.5 top-0.5 rounded-full bg-primary px-1.5 text-[10px] font-semibold text-primary-foreground">
                  {order}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
      {selected.length > 0 ? (
        <div className="space-y-2 rounded-md border bg-card/40 p-3">
          <div className="space-y-1.5">
            <Label className="text-xs text-muted-foreground">Placement</Label>
            <div className="flex gap-2">
              {([
                ['start', 'At start'],
                ['end', 'At end'],
                ['spread', 'Spread through'],
              ] as const).map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => onPlacement(key)}
                  className={
                    'rounded-md border px-2.5 py-1 text-xs transition ' +
                    (placement === key
                      ? 'border-primary bg-primary/10 text-foreground'
                      : 'border-border text-muted-foreground hover:border-muted-foreground/60')
                  }
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs text-muted-foreground">
              Each photo on screen: {seconds[0]}s
            </Label>
            <Slider
              value={seconds}
              min={1}
              max={8}
              step={0.5}
              onValueChange={onSeconds}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            Photos are added as still shots with a slow drift, and lengthen the
            reel by {(selected.length * seconds[0]).toFixed(1)}s.
          </p>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          Tap a photo to weave it into this reel.
        </p>
      )}
    </section>
  );
}
