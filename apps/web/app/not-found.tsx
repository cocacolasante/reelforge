import Link from 'next/link';
import { Button } from '@/components/ui/button';

export default function NotFound() {
  return (
    <div className="container flex min-h-[60vh] flex-col items-center justify-center gap-4 py-16 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">Page not found</h1>
      <p className="max-w-md text-sm text-muted-foreground">
        This page doesn&apos;t exist — the project or reel may have been deleted.
      </p>
      <Button asChild>
        <Link href="/">Back to projects</Link>
      </Button>
    </div>
  );
}
