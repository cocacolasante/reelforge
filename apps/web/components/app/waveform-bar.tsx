'use client';

import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api/client';
import { cn } from '@/lib/utils';

/**
 * Audio peak envelope for a range of an asset, drawn on a canvas. Peaks come
 * from the API (ffmpeg-decoded server-side), so this works for a 4K source
 * file and a 3-second voiceover take alike without downloading either.
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
}) {
  const canvasRef = React.useRef<HTMLCanvasElement>(null);
  const span = Math.max(0.05, end - start);
  const q = useQuery({
    queryKey: ['waveform', assetId, Math.round(start * 10), Math.round(end * 10), buckets],
    queryFn: () =>
      api<{ peaks: number[]; silent?: boolean }>(
        `/assets/${assetId}/waveform?start=${start.toFixed(2)}&end=${end.toFixed(2)}&buckets=${buckets}`,
      ),
    staleTime: 5 * 60_000,
    enabled: end > start,
  });

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
  }, [q.data, playhead, span, height, muted]);

  return (
    <canvas
      ref={canvasRef}
      style={{ height }}
      className={cn('block w-full rounded-sm bg-black/30', className)}
      aria-label="waveform"
    />
  );
}
