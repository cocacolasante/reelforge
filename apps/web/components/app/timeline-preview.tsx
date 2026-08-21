'use client';

import * as React from 'react';
import { AlertTriangle, Pause, Play } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { API_BASE } from '@/lib/api/client';
import type { ReelTimeline, SourceAudio, SourcePhoto, SourceVideo, TimelineShot } from '@/lib/api/schemas';
import { formatDuration } from '@/lib/format';

/**
 * Scrubbable preview of an edited timeline, played straight from the source
 * media — no render needed. Each video shot seeks into its source file
 * (served with Range support); photos are stills; text overlays are drawn as
 * DOM elements scaled to the frame. Transitions are approximated as
 * crossfades between the two media elements. Not previewed: captions, music,
 * color grade and subject-tracked reframing — the render is the truth.
 */

export type TimelinePreviewHandle = {
  seek: (t: number) => void;
  play: () => void;
  pause: () => void;
};

export type Segment = {
  index: number;
  shot: TimelineShot;
  start: number; // mezzanine seconds
  end: number;
  dur: number;
  xfadeAfter: number;
};

const DEFAULT_XFADE = 0.4;
const PLAYRES_Y = 1920; // ASS PlayResY the overlays are authored against

export function shotDuration(s: TimelineShot): number {
  return s.kind === 'photo' ? Math.max(0.2, s.duration_sec) : Math.max(0.1, s.out_ts - s.in_ts);
}

function transitionAfter(s: TimelineShot): number {
  if (!s.transition_after) return DEFAULT_XFADE;
  return s.transition_after.kind === 'cut' ? 0.04 : Math.max(0.04, s.transition_after.duration_sec);
}

/** Mezzanine placement of every shot, mirroring the render graph's xfade math. */
export function buildSegments(shots: TimelineShot[]): Segment[] {
  const segs: Segment[] = [];
  let cursor = 0;
  shots.forEach((shot, index) => {
    const dur = shotDuration(shot);
    const start = cursor;
    const end = start + dur;
    const xfadeAfter = index < shots.length - 1 ? Math.min(transitionAfter(shot), dur / 2) : 0;
    segs.push({ index, shot, start, end, dur, xfadeAfter });
    cursor = end - xfadeAfter;
  });
  return segs;
}

export function timelineTotal(segs: Segment[]): number {
  return segs.length ? segs[segs.length - 1].end : 0;
}

type Located = { primary: Segment; outgoing: Segment | null; alpha: number };

function locate(segs: Segment[], t: number): Located | null {
  if (segs.length === 0) return null;
  let primary = segs[0];
  for (const s of segs) if (s.start <= t) primary = s;
  const prev = segs[primary.index - 1];
  if (prev && t < prev.end) {
    const span = Math.max(0.001, prev.end - primary.start);
    return { primary, outgoing: prev, alpha: Math.min(1, Math.max(0, (t - primary.start) / span)) };
  }
  return { primary, outgoing: null, alpha: 1 };
}

function assToHex(ass: string): string {
  const h = ass.replace(/^&H/i, '').replace(/&$/, '').padStart(8, '0');
  return `#${h.slice(-2)}${h.slice(-4, -2)}${h.slice(-6, -4)}`;
}

export const TimelinePreview = React.forwardRef<
  TimelinePreviewHandle,
  {
    timeline: ReelTimeline;
    videos: SourceVideo[];
    photos: SourcePhoto[];
    audios?: SourceAudio[];
    /** While recording a voiceover: keep playing but silence every source so
     * the mic doesn't pick up the footage. */
    silenced?: boolean;
    /** Throttled playhead updates (~15 Hz) so the editor can draw playheads. */
    onTimeChange?: (t: number) => void;
  }
>(function TimelinePreview(
  { timeline, videos, photos, audios = [], silenced = false, onTimeChange },
  ref,
) {
  const segs = React.useMemo(() => buildSegments(timeline.shots), [timeline.shots]);
  const total = timelineTotal(segs);
  const segsRef = React.useRef(segs);
  segsRef.current = segs;

  const [t, setT] = React.useState(0);
  const [playing, setPlaying] = React.useState(false);
  const [frameH, setFrameH] = React.useState(480);
  const [brokenAssets, setBrokenAssets] = React.useState<Record<string, string>>({});
  const tRef = React.useRef(0);
  const playingRef = React.useRef(false);
  const frameRef = React.useRef<HTMLDivElement>(null);
  const videoEls = React.useRef(new Map<string, HTMLVideoElement>());
  const audioEls = React.useRef(new Map<string, HTMLAudioElement>());
  const clock = React.useRef<{ wall: number; t: number } | null>(null);
  const silencedRef = React.useRef(silenced);
  silencedRef.current = silenced;
  const onTimeRef = React.useRef(onTimeChange);
  onTimeRef.current = onTimeChange;
  const lastEmitRef = React.useRef(0);
  const emitTime = React.useCallback((time: number, force = false) => {
    const now = performance.now();
    if (force || now - lastEmitRef.current > 66) {
      lastEmitRef.current = now;
      onTimeRef.current?.(time);
    }
  }, []);
  const timelineRef = React.useRef(timeline);
  timelineRef.current = timeline;

  const videoAssetIds = React.useMemo(
    () => Array.from(new Set(timeline.shots.filter((s) => s.kind === 'video').map((s) => s.asset_id))),
    [timeline.shots],
  );
  const photoById = React.useMemo(() => new Map(photos.map((p) => [p.asset_id, p])), [photos]);
  const videoById = React.useMemo(() => new Map(videos.map((v) => [v.asset_id, v])), [videos]);

  // Frame height drives overlay font scaling.
  React.useEffect(() => {
    const el = frameRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setFrameH(el.clientHeight || 480));
    ro.observe(el);
    setFrameH(el.clientHeight || 480);
    return () => ro.disconnect();
  }, []);

  /** Push the state of every media element to match time `time`. */
  const applyFrame = React.useCallback((time: number, isPlaying: boolean) => {
    const loc = locate(segsRef.current, time);
    const wanted = new Map<string, { opacity: number; srcTime: number | null; gain: number }>();
    if (loc) {
      const place = (seg: Segment, opacity: number) => {
        if (seg.shot.kind !== 'video') return;
        const srcTime = seg.shot.in_ts + Math.max(0, Math.min(seg.dur, time - seg.start));
        const gain = seg.shot.muted ? 0 : Math.max(0, Math.min(1, seg.shot.volume));
        const prev = wanted.get(seg.shot.asset_id);
        // Same asset in both crossfade halves: the incoming shot wins.
        if (!prev || opacity >= prev.opacity) wanted.set(seg.shot.asset_id, { opacity, srcTime, gain });
      };
      if (loc.outgoing) place(loc.outgoing, 1 - loc.alpha);
      place(loc.primary, loc.outgoing ? loc.alpha : 1);
    }
    videoEls.current.forEach((el, assetId) => {
      const w = wanted.get(assetId);
      if (!w) {
        el.style.opacity = '0';
        if (!el.paused) el.pause();
        return;
      }
      el.style.opacity = String(w.opacity);
      // Per-shot volume/mute (HTML media caps at 1.0; the render allows up to 3x).
      el.volume = silencedRef.current ? 0 : w.gain * w.opacity;
      el.muted = silencedRef.current || w.gain <= 0;
      if (w.srcTime !== null) {
        const drift = Math.abs(el.currentTime - w.srcTime);
        if (!isPlaying || drift > 0.35) {
          // Seeking while paused shows the sought frame; while playing it
          // corrects accumulated drift against the wall clock.
          if (drift > 0.05) el.currentTime = w.srcTime;
        }
      }
      if (isPlaying) {
        if (el.paused) void el.play().catch(() => {});
      } else if (!el.paused) {
        el.pause();
      }
    });

    // Voiceover takes: each is an <audio> offset to its start on the timeline.
    const takes = timelineRef.current.voiceovers;
    audioEls.current.forEach((el, takeId) => {
      const take = takes.find((v) => v.id === takeId);
      if (!take) {
        if (!el.paused) el.pause();
        return;
      }
      const within = time >= take.start_sec && time < take.start_sec + Math.max(0.05, take.duration_sec);
      const gain = take.muted ? 0 : Math.max(0, Math.min(1, take.volume));
      el.volume = silencedRef.current ? 0 : gain;
      el.muted = silencedRef.current || gain <= 0;
      if (!within) {
        if (!el.paused) el.pause();
        return;
      }
      const srcTime = time - take.start_sec;
      const drift = Math.abs(el.currentTime - srcTime);
      if (!isPlaying || drift > 0.35) {
        if (drift > 0.05) el.currentTime = srcTime;
      }
      if (isPlaying) {
        if (el.paused) void el.play().catch(() => {});
      } else if (!el.paused) {
        el.pause();
      }
    });
  }, []);

  // Master clock: wall time, so photos and crossfades advance uniformly.
  React.useEffect(() => {
    playingRef.current = playing;
    if (!playing) {
      clock.current = null;
      applyFrame(tRef.current, false);
      return;
    }
    clock.current = { wall: performance.now(), t: tRef.current };
    let raf = 0;
    const tick = () => {
      if (!playingRef.current || !clock.current) return;
      const now = performance.now();
      let next = clock.current.t + (now - clock.current.wall) / 1000;
      const tot = timelineTotal(segsRef.current);
      if (next >= tot) {
        next = tot;
        tRef.current = next;
        setT(next);
        setPlaying(false);
        applyFrame(next, false);
        return;
      }
      tRef.current = next;
      setT(next);
      emitTime(next);
      applyFrame(next, true);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, applyFrame, emitTime]);

  React.useEffect(() => {
    applyFrame(tRef.current, playingRef.current);
  }, [silenced, applyFrame]);

  // Timeline edits: clamp and re-sync without restarting playback.
  React.useEffect(() => {
    const tot = timelineTotal(segs);
    if (tRef.current > tot) {
      tRef.current = tot;
      setT(tot);
    }
    if (!playingRef.current) applyFrame(tRef.current, false);
    else if (clock.current) clock.current = { wall: performance.now(), t: tRef.current };
  }, [segs, applyFrame]);

  const seek = React.useCallback(
    (time: number) => {
      const tot = timelineTotal(segsRef.current);
      const clamped = Math.max(0, Math.min(tot, time));
      tRef.current = clamped;
      setT(clamped);
      emitTime(clamped, true);
      if (clock.current) clock.current = { wall: performance.now(), t: clamped };
      applyFrame(clamped, playingRef.current);
    },
    [applyFrame, emitTime],
  );

  React.useImperativeHandle(
    ref,
    () => ({
      seek,
      play: () => total > 0 && setPlaying(true),
      pause: () => setPlaying(false),
    }),
    [seek, total],
  );

  const togglePlay = () => {
    if (total <= 0) return;
    if (!playing && tRef.current >= total - 0.01) seek(0);
    setPlaying((p) => !p);
  };

  const loc = locate(segs, t);
  const scale = frameH / PLAYRES_Y;
  const visibleOverlays = timeline.overlays
    .filter((o) => o.text.trim() && t >= o.start_sec && t <= o.end_sec)
    .map((o) => {
      const fade = Math.max(0, o.fade_ms) / 1000;
      let opacity = 1;
      if (fade > 0) {
        opacity = Math.min(1, (t - o.start_sec) / fade, (o.end_sec - t) / fade);
      }
      return { o, opacity: Math.max(0, opacity) };
    });

  const broken = loc && brokenAssets[loc.primary.shot.asset_id];

  return (
    <div className="space-y-2">
      <div
        ref={frameRef}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === ' ') {
            e.preventDefault();
            togglePlay();
          } else if (e.key === 'ArrowLeft') seek(tRef.current - (e.shiftKey ? 1 : 0.1));
          else if (e.key === 'ArrowRight') seek(tRef.current + (e.shiftKey ? 1 : 0.1));
        }}
        onClick={togglePlay}
        className="relative mx-auto aspect-[9/16] max-h-[460px] w-auto cursor-pointer overflow-hidden rounded-md bg-black outline-none ring-primary/40 focus:ring-2"
        style={{ width: 'min(100%, calc(460px * 9 / 16))' }}
      >
        {videoAssetIds.map((id) => (
          <video
            key={id}
            ref={(el) => {
              if (el) videoEls.current.set(id, el);
              else videoEls.current.delete(id);
            }}
            src={`${API_BASE}/api/v1/assets/${id}/media`}
            preload="auto"
            playsInline
            className="absolute inset-0 h-full w-full object-cover"
            style={{ opacity: 0 }}
            onError={() =>
              setBrokenAssets((b) => ({
                ...b,
                [id]: `${videoById.get(id)?.filename ?? 'This clip'} can't be decoded by your browser (likely HEVC). The render still works — try Safari to preview it.`,
              }))
            }
          />
        ))}
        {timeline.voiceovers.map((take) => {
          const src = audios.find((a) => a.asset_id === take.asset_id);
          return src ? (
            <audio
              key={take.id}
              ref={(el) => {
                if (el) audioEls.current.set(take.id, el);
                else audioEls.current.delete(take.id);
              }}
              src={`${API_BASE}${src.url}`}
              preload="auto"
            />
          ) : null;
        })}
        {/* Photo shots */}
        {loc
          ? [loc.outgoing, loc.primary]
              .filter((s): s is Segment => !!s && s.shot.kind === 'photo')
              .map((seg) => {
                const p = photoById.get(seg.shot.asset_id);
                const opacity = seg === loc.primary ? (loc.outgoing ? loc.alpha : 1) : 1 - loc.alpha;
                return p ? (
                  <img
                    key={`photo-${seg.index}`}
                    alt=""
                    src={`${API_BASE}${p.url}`}
                    className="absolute inset-0 h-full w-full object-cover"
                    style={{ opacity }}
                  />
                ) : null;
              })
          : null}
        {/* Text overlays */}
        {visibleOverlays.map(({ o, opacity }) => {
          const fontPx = Math.max(8, o.font_size_px * scale);
          const stroke = Math.max(1, 3 * scale);
          const pos: React.CSSProperties =
            o.position === 'top'
              ? { top: '8%' }
              : o.position === 'bottom'
              ? { bottom: '22%' }
              : { top: '50%', transform: 'translateY(-50%)' };
          return (
            <div
              key={o.id || o.text}
              className="pointer-events-none absolute left-0 right-0 px-[6%] text-center"
              style={{
                ...pos,
                opacity,
                color: assToHex(o.color),
                fontSize: `${fontPx}px`,
                fontWeight: o.bold ? 700 : 400,
                lineHeight: 1.15,
                whiteSpace: 'pre-line',
                textShadow: `-${stroke}px 0 #000, ${stroke}px 0 #000, 0 -${stroke}px #000, 0 ${stroke}px #000, ${stroke}px ${stroke}px 4px rgba(0,0,0,.6)`,
                fontFamily: 'Inter, system-ui, sans-serif',
              }}
            >
              {o.text}
            </div>
          );
        })}
        {total <= 0 ? (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
            Add a shot to preview
          </div>
        ) : null}
        {broken ? (
          <div className="absolute inset-x-0 bottom-0 flex items-start gap-2 bg-black/80 p-3 text-xs text-amber-300">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{broken}</span>
          </div>
        ) : null}
        {!playing && total > 0 ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <div className="rounded-full bg-black/50 p-3">
              <Play className="h-6 w-6 text-white" />
            </div>
          </div>
        ) : null}
      </div>

      {/* Transport */}
      <div className="flex items-center gap-2">
        <Button size="sm" variant="secondary" onClick={togglePlay} disabled={total <= 0}>
          {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
        </Button>
        <span className="w-[7.5rem] shrink-0 font-mono text-xs text-muted-foreground">
          {formatDuration(t)} / {formatDuration(total)}
        </span>
        <input
          type="range"
          min={0}
          max={Math.max(0.05, total)}
          step={0.05}
          value={Math.min(t, total)}
          onChange={(e) => seek(Number(e.target.value))}
          className="w-full accent-primary"
          aria-label="Scrub"
        />
      </div>

      {/* Shot strip */}
      {segs.length > 0 ? (
        <div className="flex h-5 w-full gap-px overflow-hidden rounded-sm">
          {segs.map((seg) => {
            const active = loc?.primary.index === seg.index;
            return (
              <button
                key={seg.index}
                title={`Shot ${seg.index + 1}`}
                onClick={() => seek(seg.start)}
                style={{ flexGrow: Math.max(0.2, seg.dur), flexBasis: 0 }}
                className={
                  'truncate px-1 text-[10px] leading-5 transition ' +
                  (active ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/70')
                }
              >
                {seg.index + 1}
                {seg.shot.kind === 'photo' ? ' ▣' : ''}
              </button>
            );
          })}
        </div>
      ) : null}
      <p className="text-[11px] text-muted-foreground">
        Preview plays your source footage and voiceover takes directly — transitions shown as
        crossfades; captions, music, color grade, subject reframing and ducking appear in the
        render. Space to play, ←/→ to step.
      </p>
    </div>
  );
});
