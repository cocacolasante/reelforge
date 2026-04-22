'use client';

import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Plus, FolderOpen } from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { useCreateProject, useProjects } from '@/lib/api/hooks';
import { humanMessage } from '@/lib/api/errors';
import { formatTimestamp } from '@/lib/format';

export default function HomePage() {
  return (
    <AppShell>
      <div className="container py-10">
        <header className="mb-8 flex items-center justify-between gap-4">
          <div>
            <h1 className="text-3xl font-semibold tracking-tight">Projects</h1>
            <p className="text-sm text-muted-foreground">
              Each project holds one source video and the reels you extract from it.
            </p>
          </div>
          <NewProjectButton />
        </header>
        <ProjectList />
      </div>
    </AppShell>
  );
}

function ProjectList() {
  const { data, isLoading, error, refetch } = useProjects();

  if (isLoading) {
    return (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 6 }, (_, i) => (
          <div
            key={i}
            className="h-32 animate-pulse rounded-lg border bg-card/40"
          />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Couldn&apos;t load projects</AlertTitle>
        <AlertDescription>{humanMessage(error)}</AlertDescription>
        <div className="mt-3">
          <Button size="sm" onClick={() => refetch()}>
            Try again
          </Button>
        </div>
      </Alert>
    );
  }

  if (!data || data.projects.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed bg-card/40 px-6 py-20 text-center">
        <FolderOpen className="h-10 w-10 text-muted-foreground" />
        <p className="text-lg font-medium">No projects yet</p>
        <p className="max-w-sm text-sm text-muted-foreground">
          Create a project to upload a video and generate reels.
        </p>
        <NewProjectButton label="Create your first project" size="lg" />
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {data.projects.map((p) => (
        <Link
          key={p.id}
          href={`/projects/${p.id}`}
          className="group rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Card className="h-full transition-colors group-hover:border-primary/60">
            <CardHeader>
              <CardTitle>{p.name}</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              <div>Created {formatTimestamp(p.created_at)}</div>
              {p.source_asset_id ? (
                <div className="mt-1 text-xs text-emerald-400/80">has source</div>
              ) : (
                <div className="mt-1 text-xs text-muted-foreground/70">
                  no source yet
                </div>
              )}
            </CardContent>
          </Card>
        </Link>
      ))}
    </div>
  );
}

function NewProjectButton({
  label = 'New project',
  size = 'default',
}: {
  label?: string;
  size?: 'default' | 'lg';
}) {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState('');
  const create = useCreateProject();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const p = await create.mutateAsync({ name: name.trim() || 'Untitled' });
      setOpen(false);
      router.push(`/projects/${p.id}`);
    } catch {
      /* error surfaced below */
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size={size}>
          <Plus className="h-4 w-4" />
          {label}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New project</DialogTitle>
          <DialogDescription>
            Give it a name; you&apos;ll upload a source video next.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
          <Label htmlFor="project-name">Project name</Label>
          <Input
            id="project-name"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="E.g. Conference talk cutdown"
          />
          {create.error ? (
            <Alert variant="destructive">
              <AlertDescription>{humanMessage(create.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? 'Creating…' : 'Create'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
