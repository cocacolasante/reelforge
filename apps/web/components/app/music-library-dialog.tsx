'use client';

import * as React from 'react';
import { Music, Trash2, Upload } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { API_BASE } from '@/lib/api/client';
import { useDeleteMusic, useMusicLibrary, useUploadMusic } from '@/lib/api/hooks';

const MOODS = [
  'energetic', 'joyful', 'calm', 'triumphant', 'tense',
  'mysterious', 'romantic', 'somber', 'melancholic', 'neutral',
] as const;

export function prettyTrackName(id: string): string {
  return id
    .replace(/^(user|sb|lfm)-/, '')
    .replace(/-[0-9a-f]{8}$/, '')
    .replace(/-/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function MusicLibraryDialog() {
  const music = useMusicLibrary();
  const upload = useUploadMusic();
  const remove = useDeleteMusic();
  const [file, setFile] = React.useState<File | null>(null);
  const [title, setTitle] = React.useState('');
  const [mood, setMood] = React.useState<string>('energetic');
  const [error, setError] = React.useState<string | null>(null);
  const fileRef = React.useRef<HTMLInputElement>(null);

  const doUpload = async () => {
    if (!file) return;
    setError(null);
    try {
      await upload.mutateAsync({
        file,
        title: title.trim() || file.name.replace(/\.[a-z0-9]+$/i, ''),
        mood,
        license: 'Pixabay/royalty-free',
      });
      setFile(null);
      setTitle('');
      if (fileRef.current) fileRef.current.value = '';
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upload failed');
    }
  };

  const tracks = music.data?.tracks ?? [];

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Music className="h-3.5 w-3.5" />
          Manage library
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Music library</DialogTitle>
          <DialogDescription>
            Tracks are matched to each reel&apos;s mood. CC-BY tracks get their
            credit line auto-added to descriptions when you publish.
          </DialogDescription>
        </DialogHeader>

        <div className="rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
          Want specific songs? Grab free, no-attribution tracks from{' '}
          <a
            href="https://pixabay.com/music/"
            target="_blank"
            rel="noreferrer"
            className="underline text-foreground"
          >
            pixabay.com/music
          </a>{' '}
          (or the YouTube Audio Library), then upload them here with a mood tag.
        </div>

        <div className="grid gap-2 sm:grid-cols-[1fr_auto_auto_auto] items-end">
          <div className="space-y-1">
            <Label className="text-xs">Audio file</Label>
            <Input
              ref={fileRef}
              type="file"
              accept="audio/*"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">Title</Label>
            <Input
              value={title}
              placeholder="(from filename)"
              onChange={(e) => setTitle(e.target.value)}
              className="w-36"
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">Mood</Label>
            <Select value={mood} onValueChange={setMood}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MOODS.map((m) => (
                  <SelectItem key={m} value={m}>{m}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button onClick={() => void doUpload()} disabled={!file || upload.isPending}>
            <Upload className="h-3.5 w-3.5" />
            {upload.isPending ? 'Uploading…' : 'Upload'}
          </Button>
        </div>
        {error ? <p className="text-xs text-destructive">{error}</p> : null}

        <div className="max-h-72 space-y-1 overflow-y-auto pr-1">
          {tracks.map((t) => (
            <div
              key={t.id}
              className="flex items-center gap-2 rounded-md border px-2 py-1.5 text-sm"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{prettyTrackName(t.id)}</div>
                <div className="text-xs text-muted-foreground">
                  {t.mood} · {Math.round(t.duration_sec)}s · {t.license}
                  {t.license.toUpperCase().startsWith('CC-BY') ? ' · credit auto-added' : ''}
                </div>
              </div>
              <audio
                controls
                preload="none"
                src={`${API_BASE}/api/v1/music/${t.id}/audio`}
                className="h-8 w-44"
              />
              {t.source === 'user' ? (
                <button
                  title="Delete track"
                  onClick={() => void remove.mutateAsync(t.id).catch(() => {})}
                  className="text-muted-foreground hover:text-destructive"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              ) : null}
            </div>
          ))}
          {tracks.length === 0 ? (
            <p className="text-sm text-muted-foreground">No tracks yet.</p>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
