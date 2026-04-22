'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { Clapperboard, Heart } from 'lucide-react';
import { useProjects, useHealth } from '@/lib/api/hooks';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const projectsQuery = useProjects();
  const healthQuery = useHealth();

  const activeId = pathname?.match(/^\/projects\/([^/]+)/)?.[1];
  const projects = projectsQuery.data?.projects ?? [];

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-border bg-card/40 backdrop-blur">
        <div className="container flex h-14 items-center justify-between gap-4">
          <Link
            href="/"
            className="flex items-center gap-2 font-semibold tracking-tight text-foreground"
          >
            <Clapperboard className="h-5 w-5 text-primary" />
            ReelForge
          </Link>

          <div className="flex-1 max-w-xs">
            {projects.length > 0 ? (
              <Select
                value={activeId ?? ''}
                onValueChange={(v) => {
                  if (v) router.push(`/projects/${v}`);
                }}
              >
                <SelectTrigger aria-label="Switch project">
                  <SelectValue placeholder="Select a project…" />
                </SelectTrigger>
                <SelectContent>
                  {projects.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : null}
          </div>

          <HealthPill ok={healthQuery.data?.status === 'ok'} />
        </div>
      </header>
      <main className="flex-1">{children}</main>
    </div>
  );
}

function HealthPill({ ok }: { ok: boolean }) {
  return (
    <div
      className="flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs text-muted-foreground"
      title={ok ? 'API reachable' : 'API unreachable'}
    >
      <Heart
        className={
          'h-3 w-3 ' + (ok ? 'text-emerald-400' : 'text-destructive animate-pulse')
        }
      />
      {ok ? 'API ready' : 'API offline'}
    </div>
  );
}
