'use client';

import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api/client';
import { cn } from '@/lib/utils';

const DRAG_THRESHOLD_PX = 4;

/**
 * Audio peak envelope for a range of an asset, drawn on a canvas. Peaks come
 * from the API (ffmpeg-decoded server-side), so this works for a 4K source
 * file and a 3-second voiceover take alike without downloading either.
 *
 * Interactive when given handlers: click (`onSeek`) jumps to that point;
 * dragging (`onSelect`) selects a range, e.g. dead air to cut out. All times
 * are seconds into [start, end].
 */
export function WaveformBar({
  assetId,
  start,
  end,
  playhead,
  muted = false,
  height = 36,
  buckets = 160,
  className,
  onSeek,
  selection = null,
  onSelect,
}: {
  assetId: string;
  start: number;
  end: number;
  /** Seconds into this range; null hides the playhead. */
  playhead?: number | null;
  muted?: boolean;
  height?: number;
  buckets?: number;
  className?: string;
  /** Click → seconds into this range. */
  onSeek?: (sec: number) => void;
  /** Highlighted range, seconds into this range. */
  selection?: [number, number] | null;
  /** Drag → a range, seconds into this range (clicks still seek). */
  onSelect?: (from: number, to: number) => void;
}) {
  const canvasRef = React.useRef<HTMLCanvasElement>(null);
  const span = Math.max(0.05, end - start);
  const drag = React.useRef<{ x0: number; moved: boolean } | null>(null);
  const [draft, setDraft] = React.useState<[number, number] | null>(null);
  const q = useQuery({
    queryKey: ['waveform', assetId, Math.round(start * 10), Math.round(end * 10), buckets],
    queryFn: () =>
      api<{ peaks: number[]; silent?: boolean }>(
        `/assets/${assetId}/waveform?start=${start.toFixed(2)}&end=${end.toFixed(2)}&buckets=${buckets}`,
      ),
    staleTime: 5 * 60_000,
    enabled: end > start,
  });

  const shown = draft ?? selection;

  React.useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 300;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, height);
    if (shown) {
      const x0 = (Math.max(0, Math.min(shown[0], span)) / span) * cssW;
      const x1 = (Math.max(0, Math.min(shown[1], span)) / span) * cssW;
      ctx.fillStyle = 'rgba(248,113,113,0.28)';
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0), height);
      ctx.fillStyle = 'rgba(248,113,113,0.9)';
      ctx.fillRect(x0 - 0.5, 0, 1.5, height);
      ctx.fillRect(x1 - 1, 0, 1.5, height);
    }
    const peaks = q.data?.peaks ?? [];
    const n = peaks.length;
    const mid = height / 2;
    const styles = getComputedStyle(canvas);
    const accent = styles.getPropertyValue('--primary').trim();
    const bar = muted ? 'rgba(148,163,184,0.35)' : accent ? `hsl(${accent} / 0.75)` : 'rgba(99,102,241,0.75)';
    if (n === 0) {
      ctx.fillStyle = 'rgba(148,163,184,0.25)';
      ctx.fillRect(0, mid - 0.5, cssW, 1);
    } else {
      const w = cssW / n;
      ctx.fillStyle = bar;
      for (let i = 0; i < n; i++) {
        const h = Math.max(1, peaks[i] * (height - 2));
        ctx.fillRect(i * w + w * 0.15, mid - h / 2, Math.max(1, w * 0.7), h);
      }
    }
    if (playhead !== null && playhead !== undefined && playhead >= 0 && playhead <= span) {
      const x = (playhead / span) * cssW;
      ctx.fillStyle = 'rgba(248,250,252,0.95)';
      ctx.fillRect(x - 0.5, 0, 1.5, height);
    }
  }, [q.data, playhead, span, height, muted, shown]);

  const interactive = !!(onSeek || onSelect);

  const secAt = (clientX: number): number => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return 0;
    const f = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return f * span;
  };

  const ordered = (a: number, b: number): [number, number] => (a <= b ? [a, b] : [b, a]);

  return (
    <canvas
      ref={canvasRef}
      style={{ height, touchAction: interactive ? 'none' : undefined }}
      className={cn(
        'block w-full rounded-sm bg-black/30',
        onSelect ? 'cursor-crosshair' : onSeek ? 'cursor-pointer' : '',
        className,
      )}
      aria-label="waveform"
      title={
        onSelect
          ? 'Click to jump here · drag to select a section'
          : onSeek
            ? 'Click to jump here'
            : undefined
      }
      onPointerDown={(e) => {
        if (!interactive || e.button !== 0) return;
        e.currentTarget.setPointerCapture(e.pointerId);
        drag.current = { x0: e.clientX, moved: false };
      }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        if (!d.moved && Math.abs(e.clientX - d.x0) > DRAG_THRESHOLD_PX) d.moved = true;
        if (!d.moved) return;
        if (onSelect) setDraft(ordered(secAt(d.x0), secAt(e.clientX)));
        else onSeek?.(secAt(e.clientX)); // no selection: dragging scrubs
      }}
      onPointerUp={(e) => {
        const d = drag.current;
        drag.current = null;
        if (!d) return;
        if (d.moved && onSelect) {
          const [a, b] = ordered(secAt(d.x0), secAt(e.clientX));
          setDraft(null);
          if (b - a >= 0.05) onSelect(a, b);
          return;
        }
        onSeek?.(secAt(e.clientX));
      }}
      onPointerCancel={() => {
        drag.current = null;
        setDraft(null);
      }}
    />
  );
}
