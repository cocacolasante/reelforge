'use client';

import * as React from 'react';
import { Trash2 } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useDeleteProject, useProjectAssets } from '@/lib/api/hooks';
import { humanMessage } from '@/lib/api/errors';
import { formatBytes } from '@/lib/format';

type ProjectRef = { id: string; name: string };

/** Trash button + confirmation for deleting a whole project. `compact`
 * renders an icon-only button (project cards). */
export function DeleteProjectButton({
  project,
  onDeleted,
  compact = false,
}: {
  project: ProjectRef;
  onDeleted?: () => void;
  compact?: boolean;
}) {
  const [open, setOpen] = React.useState(false);
  return (
    <>
      <Button
        size={compact ? 'sm' : 'default'}
        variant="ghost"
        title={`Delete ${project.name}`}
        aria-label={`Delete ${project.name}`}
        onClick={() => setOpen(true)}
        className="text-muted-foreground hover:text-destructive"
      >
        <Trash2 className="h-4 w-4" />
        {compact ? null : 'Delete project'}
      </Button>
      <DeleteProjectDialog
        project={project}
        open={open}
        onOpenChange={setOpen}
        onDeleted={onDeleted}
      />
    </>
  );
}

export function DeleteProjectDialog({
  project,
  open,
  onOpenChange,
  onDeleted,
}: {
  project: ProjectRef;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDeleted?: () => void;
}) {
  const remove = useDeleteProject();
  // Only look inside the project once the dialog is actually open.
  const assets = useProjectAssets(open ? project.id : undefined);
  const list = assets.data?.assets ?? [];
  const clips = list.filter((a) => a.kind === 'video').length;
  const extras = list.length - clips;
  const bytes = list.reduce((sum, a) => sum + a.size_bytes, 0);

  let contents = 'no uploaded media yet';
  if (assets.isLoading) {
    contents = 'counting its media…';
  } else if (list.length > 0) {
    contents = `${clips} clip${clips === 1 ? '' : 's'}`;
    if (extras > 0) contents += ` and ${extras} photo/voiceover file${extras === 1 ? '' : 's'}`;
    contents += ` (${formatBytes(bytes)} of uploads)`;
  }

  const confirm = async () => {
    try {
      await remove.mutateAsync(project.id);
      onOpenChange(false);
      onDeleted?.();
    } catch {
      /* surfaced inline */
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (remove.isPending) return;
        remove.reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Delete this project?</DialogTitle>
          <DialogDescription>
            <span className="font-semibold text-foreground">{project.name}</span> will be
            permanently removed from disk: {contents}, plus every analysis, reel,
            composed video, export and AI mix made from them. Anything still
            processing is stopped. This can&apos;t be undone.
          </DialogDescription>
        </DialogHeader>
        {remove.error ? (
          <Alert variant="destructive">
            <AlertDescription>{humanMessage(remove.error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter className="gap-2 sm:gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={remove.isPending}>
            Keep it
          </Button>
          <Button
            variant="destructive"
            onClick={() => void confirm()}
            disabled={remove.isPending}
          >
            <Trash2 className="h-4 w-4" />
            {remove.isPending ? 'Deleting…' : 'Delete project'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
