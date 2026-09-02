'use client';

import * as React from 'react';
import Link from 'next/link';
import { useQueryClient } from '@tanstack/react-query';
import {
  ArrowDown,
  ArrowUp,
  Film,
  Image as ImageIcon,
  Mic,
  Play,
  Plus,
  Square,
  Volume2,
  VolumeX,
  RotateCcw,
  Save,
  Scissors,
  Trash2,
  Type,
  Wand2,
} from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { JobProgress } from '@/components/app/job-progress';
import {
  TimelinePreview,
  buildSegments,
  type TimelinePreviewHandle,
} from '@/components/app/timeline-preview';
import { WaveformBar } from '@/components/app/waveform-bar';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { API_BASE } from '@/lib/api/client';
import { humanMessage } from '@/lib/api/errors';
import {
  useDeleteAsset,
  useEnqueueCompose,
  useReel,
  useReelEdit,
  useResetReelEdit,
  useSaveReelEdit,
  useUploadVoiceover,
} from '@/lib/api/hooks';
import type {
  ReelTimeline,
  SourceAudio,
  SourcePhoto,
  SourceVideo,
  TextOverlay,
  TimelineShot,
  VoiceoverTake,
} from '@/lib/api/schemas';
import { formatDuration } from '@/lib/format';

// ---------- helpers ----------

const TRANSITIONS = [
  'cut',
  'fade',
  'fadeblack',
  'fadewhite',
  'dissolve',
  'slideleft',
  'slideright',
  'slideup',
  'slidedown',
  'wipeleft',
  'wiperight',
  'smoothleft',
  'smoothright',
  'circleopen',
  'circleclose',
] as const;
const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3] as const;
const PUNCH_INS = [1.15, 1.3, 1.5] as const;

/** ASS &HAABBGGRR -> #RRGGBB */
function assToHex(ass: string): string {
  const h = ass.replace(/^&H/i, '').replace(/&$/, '').padStart(8, '0');
  const bb = h.slice(-6, -4);
  const gg = h.slice(-4, -2);
  const rr = h.slice(-2);
  return `#${rr}${gg}${bb}`.toUpperCase();
}

/** #RRGGBB -> ASS &H00BBGGRR */
function hexToAss(hex: string): string {
  const h = hex.replace('#', '').padEnd(6, '0').slice(0, 6);
  const rr = h.slice(0, 2);
  const gg = h.slice(2, 4);
  const bb = h.slice(4, 6);
  return `&H00${bb}${gg}${rr}`.toUpperCase();
}

function shotDuration(s: TimelineShot): number {
  if (s.kind === 'photo') return Math.max(0.2, s.duration_sec);
  return Math.max(0.1, (s.out_ts - s.in_ts) / Math.max(0.25, s.speed || 1));
}

function totalDuration(shots: TimelineShot[]): number {
  return shots.reduce((n, s) => n + shotDuration(s), 0);
}

function round1(n: number): number {
  return Math.round(n * 10) / 10;
}

function newOverlayId(): string {
  return Math.random().toString(36).slice(2, 10);
}

function videoShot(asset_id: string, in_ts: number, out_ts: number): TimelineShot {
  return {
    kind: 'video',
    asset_id,
    in_ts: round1(in_ts),
    out_ts: round1(out_ts),
    duration_sec: 0,
    ken_burns: true,
    transition_after: null,
    volume: 1,
    speed: 1,
    punch_in: null,
    punch_in_animated: false,
    muted: false,
  };
}

function photoShot(asset_id: string): TimelineShot {
  return {
    kind: 'photo',
    asset_id,
    in_ts: 0,
    out_ts: 0,
    duration_sec: 3,
    ken_burns: true,
    transition_after: null,
    volume: 1,
    speed: 1,
    punch_in: null,
    punch_in_animated: false,
    muted: true,
  };
}

// ---------- page ----------

export default function ReelEditPage({
  params,
}: {
  params: { projectId: string; reelId: string };
}) {
  return (
    <AppShell>
      <Editor projectId={params.projectId} reelId={params.reelId} />
    </AppShell>
  );
}

function Editor({ projectId, reelId }: { projectId: string; reelId: string }) {
  const queryClient = useQueryClient();
  const reel = useReel(reelId);
  const edit = useReelEdit(reelId);
  const save = useSaveReelEdit(reelId);
  const reset = useResetReelEdit(reelId);
  const compose = useEnqueueCompose();

  const [timeline, setTimeline] = React.useState<ReelTimeline | null>(null);
  const [dirty, setDirty] = React.useState(false);
  const [composeJobId, setComposeJobId] = React.useState<string | null>(null);
  const [rendered, setRendered] = React.useState(false);
  const [resetOpen, setResetOpen] = React.useState(false);
  const [addClipOpen, setAddClipOpen] = React.useState(false);
  const [addPhotoOpen, setAddPhotoOpen] = React.useState(false);
  const previewRef = React.useRef<TimelinePreviewHandle>(null);
  const [recording, setRecording] = React.useState(false);
  const [previewT, setPreviewT] = React.useState(0);

  // Seed local state from the server once (and again after a reset).
  React.useEffect(() => {
    if (edit.data && (!timeline || !dirty)) {
      setTimeline(edit.data.timeline);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edit.data]);

  const update = (fn: (t: ReelTimeline) => ReelTimeline) => {
    setTimeline((t) => (t ? fn(t) : t));
    setDirty(true);
    setRendered(false);
  };

  if (edit.isLoading || reel.isLoading || !timeline) {
    return <div className="container py-10"><div className="h-8 w-64 animate-pulse rounded bg-card/40" /></div>;
  }
  if (edit.error) {
    return (
      <div className="container py-10">
        <Alert variant="destructive"><AlertDescription>{humanMessage(edit.error)}</AlertDescription></Alert>
      </div>
    );
  }

  const videos = edit.data?.videos ?? [];
  const photos = edit.data?.photos ?? [];
  const audios = edit.data?.audios ?? [];
  const total = totalDuration(timeline.shots);

  const doSave = async () => {
    await save.mutateAsync(timeline);
    setDirty(false);
  };

  const doRender = async () => {
    try {
      if (dirty) await doSave();
      const job = await compose.mutateAsync({
        reelId,
        config: { captions: { mode: 'karaoke' } },
      });
      setComposeJobId(job.id);
    } catch {
      /* surfaced inline */
    }
  };

  const doReset = async () => {
    await reset.mutateAsync();
    setResetOpen(false);
    setDirty(false);
    setTimeline(null); // re-seed from the refetched AI cut
  };

  // ---- shot operations ----
  const moveShot = (i: number, dir: -1 | 1) =>
    update((t) => {
      const j = i + dir;
      if (j < 0 || j >= t.shots.length) return t;
      const shots = [...t.shots];
      [shots[i], shots[j]] = [shots[j], shots[i]];
      return { ...t, shots };
    });
  const deleteShot = (i: number) =>
    update((t) => ({ ...t, shots: t.shots.filter((_, k) => k !== i) }));
  const splitShot = (i: number) =>
    update((t) => {
      const s = t.shots[i];
      if (s.kind !== 'video' || s.out_ts - s.in_ts < 1.0) return t;
      const mid = round1((s.in_ts + s.out_ts) / 2);
      const a = { ...s, out_ts: mid, transition_after: null };
      const b = { ...s, in_ts: mid };
      return { ...t, shots: [...t.shots.slice(0, i), a, b, ...t.shots.slice(i + 1)] };
    });
  const patchShot = (i: number, patch: Partial<TimelineShot>) =>
    update((t) => ({
      ...t,
      shots: t.shots.map((s, k) => (k === i ? { ...s, ...patch } : s)),
    }));

  const sourceFor = (id: string) => videos.find((v) => v.asset_id === id);
  const segments = buildSegments(timeline.shots);
  const jumpToShot = (i: number) => previewRef.current?.seek(segments[i]?.start ?? 0);

  return (
    <div className="container space-y-4 py-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link
            href={`/projects/${projectId}/reels/${reelId}`}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            ← Back to reel
          </Link>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">
            Edit · {reel.data?.title}
          </h1>
          <p className="text-sm text-muted-foreground">
            {timeline.shots.length} shot{timeline.shots.length === 1 ? '' : 's'} ·{' '}
            {formatDuration(total)} total
            {edit.data?.has_edits ? (
              <Badge variant="secondary" className="ml-2">edited</Badge>
            ) : (
              <Badge variant="muted" className="ml-2">AI cut</Badge>
            )}
            {dirty ? <span className="ml-2 text-amber-500">unsaved changes</span> : null}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {/* An AI mix has no scene-derived AI cut to fall back to — the
              timeline IS the mix (and the API 400s the reset). */}
          {!reelId.startsWith('mix-') ? (
            <Button
              variant="outline"
              onClick={() => setResetOpen(true)}
              disabled={!edit.data?.has_edits && !dirty}
            >
              <RotateCcw className="h-4 w-4" />
              Reset to AI cut
            </Button>
          ) : null}
          <Button variant="secondary" onClick={() => void doSave()} disabled={!dirty || save.isPending}>
            <Save className="h-4 w-4" />
            {save.isPending ? 'Saving…' : 'Save'}
          </Button>
          <Button onClick={() => void doRender()} disabled={compose.isPending || !!composeJobId}>
            <Wand2 className="h-4 w-4" />
            {dirty ? 'Save & render' : 'Render'}
          </Button>
        </div>
      </header>

      {save.error ? (
        <Alert variant="destructive">
          <AlertTitle>Couldn&apos;t save</AlertTitle>
          <AlertDescription>{humanMessage(save.error)}</AlertDescription>
        </Alert>
      ) : null}
      {compose.error ? (
        <Alert variant="destructive">
          <AlertDescription>{humanMessage(compose.error)}</AlertDescription>
        </Alert>
      ) : null}
      {composeJobId ? (
        <JobProgress
          jobId={composeJobId}
          variant="compose"
          onDone={() => {
            setComposeJobId(null);
            setRendered(true);
            void queryClient.invalidateQueries({ queryKey: ['reel', reelId] });
            void queryClient.invalidateQueries({ queryKey: ['exports', reelId] });
            void queryClient.invalidateQueries({ queryKey: ['project-reels', projectId] });
          }}
          onFail={() => setComposeJobId(null)}
        />
      ) : null}
      {rendered ? (
        <Alert variant="info">
          <AlertTitle>Rendered with your edits</AlertTitle>
          <AlertDescription>
            <Link className="underline" href={`/projects/${projectId}/reels/${reelId}`}>
              Open the reel
            </Link>{' '}
            to preview, export, or publish.
          </AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Preview</CardTitle>
        </CardHeader>
        <CardContent>
          <TimelinePreview
            ref={previewRef}
            timeline={timeline}
            videos={videos}
            photos={photos}
            audios={audios}
            silenced={recording}
            onTimeChange={setPreviewT}
          />
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        {/* ===== Shots ===== */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle>Shots</CardTitle>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => setAddClipOpen(true)}>
                <Film className="h-4 w-4" />
                Add clip
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setAddPhotoOpen(true)}
                disabled={photos.length === 0}
                title={photos.length === 0 ? 'Upload photos to the project first' : undefined}
              >
                <ImageIcon className="h-4 w-4" />
                Add photo
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-2">
            {timeline.shots.length === 0 ? (
              <p className="text-sm text-muted-foreground">No shots — add a clip or photo.</p>
            ) : null}
            {timeline.shots.map((s, i) => (
              <ShotRow
                key={`${s.asset_id}-${i}`}
                index={i}
                shot={s}
                isLast={i === timeline.shots.length - 1}
                source={sourceFor(s.asset_id)}
                photo={photos.find((p) => p.asset_id === s.asset_id)}
                onPatch={(patch) => patchShot(i, patch)}
                onMove={(dir) => moveShot(i, dir)}
                onSplit={() => splitShot(i)}
                onDelete={() => deleteShot(i)}
                onJump={() => jumpToShot(i)}
                playhead={
                  segments[i] && previewT >= segments[i].start && previewT <= segments[i].end
                    ? previewT - segments[i].start
                    : null
                }
              />
            ))}
          </CardContent>
        </Card>

        {/* ===== Text overlays ===== */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle>Text overlays</CardTitle>
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                update((t) => ({
                  ...t,
                  overlays: [
                    ...t.overlays,
                    {
                      id: newOverlayId(),
                      text: 'Your text',
                      start_sec: 0,
                      end_sec: Math.min(3, Math.max(1, round1(total))),
                      position: 'center',
                      font_size_px: 84,
                      color: '&H00FFFFFF',
                      outline_color: '&H00000000',
                      bold: true,
                      fade_ms: 250,
                    },
                  ],
                }))
              }
            >
              <Type className="h-4 w-4" />
              Add text
            </Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {timeline.overlays.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Titles, captions, callouts — burned into the video at the times you set.
              </p>
            ) : null}
            {timeline.overlays.map((o, i) => (
              <OverlayRow
                key={o.id || i}
                overlay={o}
                total={total}
                onPatch={(patch) =>
                  update((t) => ({
                    ...t,
                    overlays: t.overlays.map((x, k) => (k === i ? { ...x, ...patch } : x)),
                  }))
                }
                onDelete={() =>
                  update((t) => ({ ...t, overlays: t.overlays.filter((_, k) => k !== i) }))
                }
                onJump={() => previewRef.current?.seek(o.start_sec)}
              />
            ))}
          </CardContent>
        </Card>

        {/* ===== Voiceover ===== */}
        <VoiceoverPanel
          projectId={projectId}
          takes={timeline.voiceovers}
          audios={audios}
          total={total}
          previewRef={previewRef}
          recording={recording}
          onRecordingChange={setRecording}
          onChange={(takes) => update((t) => ({ ...t, voiceovers: takes }))}
          onRefreshSources={() => void edit.refetch()}
          previewT={previewT}
        />
      </div>

      {/* ===== dialogs ===== */}
      <AddClipDialog
        open={addClipOpen}
        onOpenChange={setAddClipOpen}
        videos={videos}
        onAdd={(shot) => update((t) => ({ ...t, shots: [...t.shots, shot] }))}
      />
      <AddPhotoDialog
        open={addPhotoOpen}
        onOpenChange={setAddPhotoOpen}
        photos={photos}
        onAdd={(shot) => update((t) => ({ ...t, shots: [...t.shots, shot] }))}
      />
      <Dialog open={resetOpen} onOpenChange={setResetOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Reset to the AI cut?</DialogTitle>
            <DialogDescription>
              Your edits to this reel — shots, trims, transitions and text — will be
              discarded. The original AI selection comes back.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-2">
            <Button variant="outline" onClick={() => setResetOpen(false)}>Keep edits</Button>
            <Button variant="destructive" onClick={() => void doReset()} disabled={reset.isPending}>
              Reset
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------- shot row ----------

function ShotRow({
  index,
  shot,
  isLast,
  source,
  photo,
  onPatch,
  onMove,
  onSplit,
  onDelete,
  onJump,
  playhead = null,
}: {
  index: number;
  shot: TimelineShot;
  isLast: boolean;
  source?: SourceVideo;
  photo?: SourcePhoto;
  onPatch: (p: Partial<TimelineShot>) => void;
  onMove: (dir: -1 | 1) => void;
  onSplit: () => void;
  onDelete: () => void;
  onJump: () => void;
  playhead?: number | null;
}) {
  const isPhoto = shot.kind === 'photo';
  const srcDur = source?.duration_sec ?? Infinity;
  const thumb = isPhoto
    ? photo
      ? `${API_BASE}${photo.url}`
      : null
    : (() => {
        const scene =
          source?.scenes.find((sc) => sc.start_sec <= shot.in_ts && shot.in_ts < sc.end_sec) ??
          source?.scenes[0];
        return scene ? `${API_BASE}${scene.thumbnail_url}` : null;
      })();

  const nudge = (field: 'in_ts' | 'out_ts', delta: number) => {
    let v = round1(shot[field] + delta);
    if (field === 'in_ts') v = Math.max(0, Math.min(v, shot.out_ts - 0.15));
    else v = Math.max(shot.in_ts + 0.15, Math.min(v, srcDur));
    onPatch({ [field]: v } as Partial<TimelineShot>);
  };

  const tr = shot.transition_after;

  return (
    <div className="rounded-lg border bg-card/60 p-3 text-sm">
      <div className="flex items-start gap-3">
        <button
          type="button"
          onClick={onJump}
          title="Jump preview to this shot"
          className="flex flex-col items-center gap-1 rounded-sm outline-none ring-primary/40 hover:opacity-90 focus:ring-2"
        >
          <span className="text-xs font-semibold text-muted-foreground">{index + 1}</span>
          {thumb ? (
            <img
              alt=""
              src={thumb}
              className={'rounded-sm object-cover bg-muted ' + (isPhoto ? 'h-14 w-14' : 'h-14 w-20')}
              loading="lazy"
              onError={(e) => ((e.target as HTMLImageElement).style.visibility = 'hidden')}
            />
          ) : (
            <div className="h-14 w-20 rounded-sm bg-muted" />
          )}
        </button>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="muted">{isPhoto ? 'photo' : 'clip'}</Badge>
            <span className="truncate font-medium">
              {isPhoto ? photo?.filename ?? 'photo' : source?.filename ?? shot.asset_id.slice(0, 8)}
            </span>
            <span className="text-xs text-muted-foreground">{formatDuration(shotDuration(shot))}</span>
          </div>

          {isPhoto ? (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Label className="text-xs text-muted-foreground">On screen</Label>
              <Input
                type="number"
                step={0.5}
                min={0.5}
                max={30}
                value={shot.duration_sec}
                onChange={(e) =>
                  onPatch({ duration_sec: Math.max(0.2, Math.min(30, Number(e.target.value) || 3)) })
                }
                className="h-7 w-20"
              />
              <span className="text-muted-foreground">s</span>
              <label className="ml-2 flex items-center gap-1 text-muted-foreground">
                <input
                  type="checkbox"
                  checked={shot.ken_burns}
                  onChange={(e) => onPatch({ ken_burns: e.target.checked })}
                  className="h-3.5 w-3.5 accent-primary"
                />
                drift
              </label>
            </div>
          ) : (
            <div className="grid gap-1.5 sm:grid-cols-2">
              <TrimControl
                label="In"
                value={shot.in_ts}
                onNudge={(d) => nudge('in_ts', d)}
                onSet={(v) => nudge('in_ts', v - shot.in_ts)}
              />
              <TrimControl
                label="Out"
                value={shot.out_ts}
                onNudge={(d) => nudge('out_ts', d)}
                onSet={(v) => nudge('out_ts', v - shot.out_ts)}
              />
            </div>
          )}

          {!isPhoto ? (
            <WaveformBar
              assetId={shot.asset_id}
              start={shot.in_ts}
              end={shot.out_ts}
              playhead={playhead}
              muted={shot.muted}
              height={32}
            />
          ) : null}

          {!isPhoto ? (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <button
                type="button"
                title={shot.muted ? 'Unmute this shot' : 'Mute this shot'}
                onClick={() => onPatch({ muted: !shot.muted })}
                className={
                  'rounded-md border p-1 transition ' +
                  (shot.muted ? 'border-destructive/50 text-destructive' : 'border-border text-muted-foreground hover:text-foreground')
                }
              >
                {shot.muted ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
              </button>
              <input
                type="range"
                min={0}
                max={200}
                step={5}
                value={Math.round(shot.volume * 100)}
                disabled={shot.muted}
                onChange={(e) => onPatch({ volume: Number(e.target.value) / 100 })}
                className="w-28 accent-primary disabled:opacity-40"
                aria-label="Shot volume"
              />
              <span className={'w-10 text-muted-foreground ' + (shot.muted ? 'line-through' : '')}>
                {Math.round(shot.volume * 100)}%
              </span>
            </div>
          ) : null}

          {!isPhoto ? (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Label className="text-xs text-muted-foreground">Speed</Label>
              <select
                value={String(shot.speed || 1)}
                onChange={(e) => onPatch({ speed: Number(e.target.value) })}
                className="h-7 rounded-md border bg-background px-2 text-xs"
                title="Playback speed (non-1x shots render muted)"
              >
                {SPEEDS.map((v) => (
                  <option key={v} value={String(v)}>{v}x</option>
                ))}
              </select>
              <Label className="ml-2 text-xs text-muted-foreground">Punch-in</Label>
              <select
                value={shot.punch_in ? String(shot.punch_in) : '__off__'}
                onChange={(e) =>
                  onPatch({ punch_in: e.target.value === '__off__' ? null : Number(e.target.value) })
                }
                className="h-7 rounded-md border bg-background px-2 text-xs"
                title="Digital zoom applied at render (not shown in preview)"
              >
                <option value="__off__">off</option>
                {PUNCH_INS.map((v) => (
                  <option key={v} value={String(v)}>{Math.round((v - 1) * 100)}%</option>
                ))}
              </select>
              {shot.punch_in ? (
                <label className="flex items-center gap-1 text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={shot.punch_in_animated}
                    onChange={(e) => onPatch({ punch_in_animated: e.target.checked })}
                    className="h-3.5 w-3.5 accent-primary"
                  />
                  drift
                </label>
              ) : null}
              {(shot.speed || 1) !== 1 ? (
                <span className="text-muted-foreground">audio muted at {shot.speed}x</span>
              ) : null}
            </div>
          ) : null}

          {!isLast ? (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Label className="text-xs text-muted-foreground">Then</Label>
              <select
                value={tr?.kind ?? '__default__'}
                onChange={(e) =>
                  onPatch({
                    transition_after:
                      e.target.value === '__default__'
                        ? null
                        : { kind: e.target.value, duration_sec: tr?.duration_sec ?? 0.4 },
                  })
                }
                className="h-7 rounded-md border bg-background px-2 text-xs"
              >
                <option value="__default__">reel default</option>
                {TRANSITIONS.map((k) => (
                  <option key={k} value={k}>{k}</option>
                ))}
              </select>
              {tr && tr.kind !== 'cut' ? (
                <>
                  <Input
                    type="number"
                    step={0.1}
                    min={0.1}
                    max={2}
                    value={tr.duration_sec}
                    onChange={(e) =>
                      onPatch({
                        transition_after: {
                          kind: tr.kind,
                          duration_sec: Math.max(0.1, Math.min(2, Number(e.target.value) || 0.4)),
                        },
                      })
                    }
                    className="h-7 w-20"
                  />
                  <span className="text-muted-foreground">s</span>
                </>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-col gap-1">
          <Button size="sm" variant="ghost" title="Move up" onClick={() => onMove(-1)} disabled={index === 0}>
            <ArrowUp className="h-4 w-4" />
          </Button>
          <Button size="sm" variant="ghost" title="Move down" onClick={() => onMove(1)} disabled={isLast}>
            <ArrowDown className="h-4 w-4" />
          </Button>
          {!isPhoto ? (
            <Button size="sm" variant="ghost" title="Split in half" onClick={onSplit} disabled={shot.out_ts - shot.in_ts < 1}>
              <Scissors className="h-4 w-4" />
            </Button>
          ) : null}
          <Button size="sm" variant="ghost" title="Remove" onClick={onDelete} className="hover:text-destructive">
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}

function TrimControl({
  label,
  value,
  onNudge,
  onSet,
}: {
  label: string;
  value: number;
  onNudge: (delta: number) => void;
  onSet: (v: number) => void;
}) {
  return (
    <div className="flex items-center gap-1 text-xs">
      <Label className="w-7 text-xs text-muted-foreground">{label}</Label>
      <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => onNudge(-1)}>−1s</Button>
      <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => onNudge(-0.5)}>−½</Button>
      <Input
        type="number"
        step={0.1}
        value={value}
        onChange={(e) => onSet(Number(e.target.value) || 0)}
        className="h-7 w-24"
      />
      <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => onNudge(0.5)}>+½</Button>
      <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => onNudge(1)}>+1s</Button>
    </div>
  );
}

// ---------- overlay row ----------

function OverlayRow({
  overlay,
  total,
  onPatch,
  onDelete,
  onJump,
}: {
  overlay: TextOverlay;
  total: number;
  onPatch: (p: Partial<TextOverlay>) => void;
  onDelete: () => void;
  onJump: () => void;
}) {
  return (
    <div className="space-y-2 rounded-lg border bg-card/60 p-3 text-sm">
      <div className="flex items-start gap-2">
        <textarea
          value={overlay.text}
          rows={2}
          maxLength={200}
          onChange={(e) => onPatch({ text: e.target.value })}
          className="flex-1 rounded-md border bg-background px-2 py-1 text-sm"
        />
        <Button size="sm" variant="ghost" onClick={onJump} title="Preview at this text's start">
          <Play className="h-4 w-4" />
        </Button>
        <Button size="sm" variant="ghost" onClick={onDelete} className="hover:text-destructive" title="Remove">
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Label className="text-xs text-muted-foreground">From</Label>
        <Input
          type="number"
          step={0.1}
          min={0}
          max={total}
          value={overlay.start_sec}
          onChange={(e) => onPatch({ start_sec: Math.max(0, Number(e.target.value) || 0) })}
          className="h-7 w-20"
        />
        <Label className="text-xs text-muted-foreground">to</Label>
        <Input
          type="number"
          step={0.1}
          min={0}
          value={overlay.end_sec}
          onChange={(e) => onPatch({ end_sec: Math.max(0, Number(e.target.value) || 0) })}
          className="h-7 w-20"
        />
        <span className="text-muted-foreground">s</span>
        <select
          value={overlay.position}
          onChange={(e) => onPatch({ position: e.target.value as TextOverlay['position'] })}
          className="h-7 rounded-md border bg-background px-2 text-xs"
        >
          <option value="top">top</option>
          <option value="center">center</option>
          <option value="bottom">bottom</option>
        </select>
        <Label className="text-xs text-muted-foreground">Size</Label>
        <Input
          type="number"
          step={4}
          min={24}
          max={200}
          value={overlay.font_size_px}
          onChange={(e) => onPatch({ font_size_px: Math.max(12, Number(e.target.value) || 84) })}
          className="h-7 w-20"
        />
        <input
          type="color"
          value={assToHex(overlay.color)}
          onChange={(e) => onPatch({ color: hexToAss(e.target.value) })}
          title="Text color"
          className="h-7 w-9 cursor-pointer rounded border bg-background p-0.5"
        />
        <label className="flex items-center gap-1 text-muted-foreground">
          <input
            type="checkbox"
            checked={overlay.bold}
            onChange={(e) => onPatch({ bold: e.target.checked })}
            className="h-3.5 w-3.5 accent-primary"
          />
          bold
        </label>
        <label className="flex items-center gap-1 text-muted-foreground">
          <input
            type="checkbox"
            checked={overlay.fade_ms > 0}
            onChange={(e) => onPatch({ fade_ms: e.target.checked ? 250 : 0 })}
            className="h-3.5 w-3.5 accent-primary"
          />
          fade
        </label>
      </div>
      {overlay.end_sec <= overlay.start_sec ? (
        <p className="text-xs text-destructive">End must be after start.</p>
      ) : overlay.start_sec > total ? (
        <p className="text-xs text-destructive">Starts after the reel ends ({formatDuration(total)}).</p>
      ) : null}
    </div>
  );
}

// ---------- add clip dialog ----------

function AddClipDialog({
  open,
  onOpenChange,
  videos,
  onAdd,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  videos: SourceVideo[];
  onAdd: (shot: TimelineShot) => void;
}) {
  const [assetId, setAssetId] = React.useState<string>(videos[0]?.asset_id ?? '');
  const [customIn, setCustomIn] = React.useState(0);
  const [customOut, setCustomOut] = React.useState(10);
  React.useEffect(() => {
    if (!assetId && videos[0]) setAssetId(videos[0].asset_id);
  }, [videos, assetId]);
  const video = videos.find((v) => v.asset_id === assetId);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add a clip</DialogTitle>
          <DialogDescription>
            Pick a scene from any footage in this project, or enter a custom range.
          </DialogDescription>
        </DialogHeader>
        {videos.length === 0 ? (
          <p className="text-sm text-muted-foreground">No footage in this project yet.</p>
        ) : (
          <div className="space-y-3">
            <select
              value={assetId}
              onChange={(e) => setAssetId(e.target.value)}
              className="h-9 w-full rounded-md border bg-background px-2 text-sm"
            >
              {videos.map((v) => (
                <option key={v.asset_id} value={v.asset_id}>
                  {v.filename} · {formatDuration(v.duration_sec)}
                  {v.analyzed ? '' : ' · not analyzed (no scene list)'}
                </option>
              ))}
            </select>
            {video && video.scenes.length > 0 ? (
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
                {video.scenes.map((sc) => (
                  <button
                    key={sc.index}
                    onClick={() => {
                      onAdd(videoShot(video.asset_id, sc.start_sec, sc.end_sec));
                      onOpenChange(false);
                    }}
                    className="group overflow-hidden rounded-md border text-left transition hover:border-primary"
                  >
                    <img
                      alt=""
                      src={`${API_BASE}${sc.thumbnail_url}`}
                      className="aspect-video w-full object-cover bg-muted"
                      loading="lazy"
                    />
                    <div className="px-1.5 py-1 text-[10px] text-muted-foreground">
                      {formatDuration(sc.start_sec)} – {formatDuration(sc.end_sec)}
                    </div>
                  </button>
                ))}
              </div>
            ) : null}
            <div className="flex flex-wrap items-end gap-2 rounded-md border bg-card/40 p-3 text-xs">
              <div>
                <Label className="text-xs text-muted-foreground">Custom in (s)</Label>
                <Input
                  type="number" step={0.1} min={0} value={customIn}
                  onChange={(e) => setCustomIn(Math.max(0, Number(e.target.value) || 0))}
                  className="h-8 w-24"
                />
              </div>
              <div>
                <Label className="text-xs text-muted-foreground">Out (s)</Label>
                <Input
                  type="number" step={0.1} min={0} value={customOut}
                  onChange={(e) => setCustomOut(Math.max(0, Number(e.target.value) || 0))}
                  className="h-8 w-24"
                />
              </div>
              <Button
                size="sm"
                disabled={!video || customOut - customIn < 0.5 || customOut > (video?.duration_sec ?? 0) + 0.05}
                onClick={() => {
                  if (!video) return;
                  onAdd(videoShot(video.asset_id, customIn, customOut));
                  onOpenChange(false);
                }}
              >
                <Plus className="h-4 w-4" />
                Add range
              </Button>
              {video ? (
                <span className="text-muted-foreground">source is {formatDuration(video.duration_sec)}</span>
              ) : null}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------- add photo dialog ----------

function AddPhotoDialog({
  open,
  onOpenChange,
  photos,
  onAdd,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  photos: SourcePhoto[];
  onAdd: (shot: TimelineShot) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Add a photo</DialogTitle>
          <DialogDescription>Added as a 3-second still with a slow drift. Adjust after adding.</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-4 gap-2 sm:grid-cols-5">
          {photos.map((p) => (
            <button
              key={p.asset_id}
              title={p.filename}
              onClick={() => {
                onAdd(photoShot(p.asset_id));
                onOpenChange(false);
              }}
              className="overflow-hidden rounded-md border transition hover:border-primary"
            >
              <img alt="" src={`${API_BASE}${p.url}`} className="aspect-square w-full object-cover bg-muted" loading="lazy" />
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}


// ---------- voiceover recorder ----------

function pickMimeType(): string {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
  for (const c of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(c)) return c;
  }
  return '';
}

function VoiceoverPanel({
  projectId,
  takes,
  audios,
  total,
  previewRef,
  recording,
  onRecordingChange,
  onChange,
  onRefreshSources,
  previewT,
}: {
  projectId: string;
  takes: VoiceoverTake[];
  audios: SourceAudio[];
  total: number;
  previewRef: React.RefObject<TimelinePreviewHandle>;
  recording: boolean;
  onRecordingChange: (v: boolean) => void;
  onChange: (takes: VoiceoverTake[]) => void;
  onRefreshSources: () => void;
  previewT: number;
}) {
  const upload = useUploadVoiceover(projectId);
  const removeAsset = useDeleteAsset(projectId);
  const [error, setError] = React.useState<string | null>(null);
  const [elapsed, setElapsed] = React.useState(0);
  const [uploading, setUploading] = React.useState(false);
  const recorderRef = React.useRef<MediaRecorder | null>(null);
  const streamRef = React.useRef<MediaStream | null>(null);
  const chunksRef = React.useRef<Blob[]>([]);
  const startAtRef = React.useRef(0);
  const startedWallRef = React.useRef(0);
  const timerRef = React.useRef<number | null>(null);

  const supported = typeof window !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined';

  const stopTimer = () => {
    if (timerRef.current) window.clearInterval(timerRef.current);
    timerRef.current = null;
  };

  const startRecording = async () => {
    setError(null);
    if (!supported) {
      setError('This browser cannot record audio. Chrome, Edge, Firefox and Safari 14+ all can.');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      streamRef.current = stream;
      const mime = pickMimeType();
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      chunksRef.current = [];
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = () => void finishRecording(rec.mimeType || mime || 'audio/webm');
      recorderRef.current = rec;
      // Record at the playhead: the preview keeps playing (silenced) so you
      // narrate over what you see; the take is placed where you started.
      startAtRef.current = currentPreviewTime();
      startedWallRef.current = performance.now();
      onRecordingChange(true);
      rec.start(250);
      previewRef.current?.play();
      setElapsed(0);
      timerRef.current = window.setInterval(
        () => setElapsed((performance.now() - startedWallRef.current) / 1000),
        200,
      );
    } catch (e) {
      setError(
        e instanceof Error && e.name === 'NotAllowedError'
          ? 'Microphone access was blocked — allow it in the browser address bar and try again.'
          : `Could not start recording: ${e instanceof Error ? e.message : String(e)}`,
      );
      onRecordingChange(false);
    }
  };

  // The preview exposes seek/play/pause but not a time getter; read it from
  // the DOM scrub input, which mirrors the master clock.
  const currentPreviewTime = (): number => {
    const input = document.querySelector<HTMLInputElement>('input[aria-label="Scrub"]');
    return input ? Number(input.value) || 0 : 0;
  };

  const stopRecording = () => {
    stopTimer();
    previewRef.current?.pause();
    recorderRef.current?.stop();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    onRecordingChange(false);
  };

  const finishRecording = async (mime: string) => {
    const blob = new Blob(chunksRef.current, { type: mime.split(';')[0] || 'audio/webm' });
    const duration = (performance.now() - startedWallRef.current) / 1000;
    chunksRef.current = [];
    if (blob.size < 1000 || duration < 0.3) {
      setError('That take was too short to keep.');
      return;
    }
    setUploading(true);
    try {
      const label = `take ${takes.length + 1}`;
      const asset = await upload.mutateAsync({ blob, label });
      const take: VoiceoverTake = {
        id: Math.random().toString(36).slice(2, 10),
        asset_id: asset.id,
        start_sec: round1(startAtRef.current),
        duration_sec: asset.duration_sec > 0 ? asset.duration_sec : round1(duration),
        volume: 1,
        muted: false,
        label,
      };
      onChange([...takes, take]);
      onRefreshSources();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  React.useEffect(() => () => stopTimer(), []);

  const patch = (i: number, p: Partial<VoiceoverTake>) =>
    onChange(takes.map((t, k) => (k === i ? { ...t, ...p } : t)));

  const remove = async (i: number) => {
    const take = takes[i];
    onChange(takes.filter((_, k) => k !== i));
    // The recording itself is a project asset; drop it too (best effort).
    try {
      await removeAsset.mutateAsync(take.asset_id);
      onRefreshSources();
    } catch {
      /* already gone or shared; the timeline no longer references it */
    }
  };

  return (
    <Card className="lg:col-start-2">
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Voiceover</CardTitle>
        {recording ? (
          <Button size="sm" variant="destructive" onClick={stopRecording}>
            <Square className="h-4 w-4" />
            Stop · {elapsed.toFixed(1)}s
          </Button>
        ) : (
          <Button size="sm" variant="outline" onClick={() => void startRecording()} disabled={uploading || total <= 0}>
            <Mic className="h-4 w-4" />
            {uploading ? 'Saving take…' : 'Record at playhead'}
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {takes.length > 0 && !recording ? (
          <p className="text-xs text-muted-foreground">
            Takes are transcribed and captioned automatically on render (karaoke or static
            mode); footage captions yield while a take is speaking.
          </p>
        ) : null}
        {recording ? (
          <Alert variant="destructive">
            <AlertTitle>Recording</AlertTitle>
            <AlertDescription>
              The preview is playing silently from {formatDuration(startAtRef.current)} — narrate what
              you see. Press Stop to keep the take.
            </AlertDescription>
          </Alert>
        ) : takes.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Scrub to where the narration should start, hit <span className="font-medium text-foreground">Record</span>,
            and talk over the preview. Record as many takes as you like — one per section, or
            re-do just the part you fluffed. Footage audio automatically ducks under your voice,
            and your words are captioned on render.
          </p>
        ) : null}
        {error ? (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}
        {takes.map((take, i) => {
          const src = audios.find((a) => a.asset_id === take.asset_id);
          return (
            <div key={take.id || i} className="space-y-2 rounded-lg border bg-card/60 p-3 text-sm">
              <div className="flex items-center gap-2">
                <Mic className="h-4 w-4 text-muted-foreground" />
                <Input
                  value={take.label}
                  onChange={(e) => patch(i, { label: e.target.value })}
                  className="h-7 w-40"
                />
                <span className="text-xs text-muted-foreground">
                  {formatDuration(take.duration_sec)}
                  {!src ? ' · file missing' : ''}
                </span>
                <div className="ml-auto flex gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    title="Preview from this take"
                    onClick={() => {
                      previewRef.current?.seek(take.start_sec);
                      previewRef.current?.play();
                    }}
                  >
                    <Play className="h-4 w-4" />
                  </Button>
                  <Button size="sm" variant="ghost" title="Delete take" onClick={() => void remove(i)} className="hover:text-destructive">
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
              <WaveformBar
                assetId={take.asset_id}
                start={0}
                end={Math.max(0.05, take.duration_sec)}
                playhead={
                  previewT >= take.start_sec && previewT <= take.start_sec + take.duration_sec
                    ? previewT - take.start_sec
                    : null
                }
                muted={take.muted}
                height={28}
              />
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <Label className="text-xs text-muted-foreground">Starts at</Label>
                <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => patch(i, { start_sec: Math.max(0, round1(take.start_sec - 0.5)) })}>−½</Button>
                <Input
                  type="number"
                  step={0.1}
                  min={0}
                  max={total}
                  value={take.start_sec}
                  onChange={(e) => patch(i, { start_sec: Math.max(0, Math.min(total, Number(e.target.value) || 0)) })}
                  className="h-7 w-24"
                />
                <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => patch(i, { start_sec: Math.min(total, round1(take.start_sec + 0.5)) })}>+½</Button>
                <span className="text-muted-foreground">s</span>
                <button
                  type="button"
                  title={take.muted ? 'Unmute' : 'Mute'}
                  onClick={() => patch(i, { muted: !take.muted })}
                  className={
                    'ml-2 rounded-md border p-1 transition ' +
                    (take.muted ? 'border-destructive/50 text-destructive' : 'border-border text-muted-foreground hover:text-foreground')
                  }
                >
                  {take.muted ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
                </button>
                <input
                  type="range"
                  min={0}
                  max={200}
                  step={5}
                  value={Math.round(take.volume * 100)}
                  disabled={take.muted}
                  onChange={(e) => patch(i, { volume: Number(e.target.value) / 100 })}
                  className="w-24 accent-primary disabled:opacity-40"
                  aria-label="Take volume"
                />
                <span className="w-10 text-muted-foreground">{Math.round(take.volume * 100)}%</span>
              </div>
              {take.start_sec + take.duration_sec > total + 0.05 ? (
                <p className="text-xs text-amber-500">
                  Runs {formatDuration(take.start_sec + take.duration_sec - total)} past the end of the reel — the extra is cut off.
                </p>
              ) : null}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
