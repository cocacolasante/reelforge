'use client';

import * as React from 'react';
import Link from 'next/link';
import { AlertTriangle, Check, Copy, KeyRound, Plus } from 'lucide-react';
import { AppShell } from '@/components/layouts/app-shell';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import {
  useAgentConnection,
  useApiKeys,
  useCreateApiKey,
  useRevokeApiKey,
} from '@/lib/api/hooks';
import { humanMessage } from '@/lib/api/errors';
import type { ApiKey } from '@/lib/api/schemas';

function CopyField({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <div className="flex items-center gap-2">
      <code className="flex-1 truncate rounded-md border border-border bg-muted/40 px-3 py-2 font-mono text-xs">
        {value}
      </code>
      <Button
        variant="secondary"
        size="sm"
        aria-label={`Copy ${label}`}
        onClick={() => {
          void navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
      >
        {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
      </Button>
    </div>
  );
}

function formatWhen(iso: string | null): string {
  if (!iso) return 'never';
  return new Date(iso).toLocaleString();
}

export default function AgentAccessPage() {
  const keys = useApiKeys();
  const connection = useAgentConnection();
  const createKey = useCreateApiKey();
  const revokeKey = useRevokeApiKey();

  const [name, setName] = React.useState('Muse on my phone');
  // Shown once, in memory only: the server keeps a hash, so once this is
  // dismissed the token is unrecoverable and a new key must be minted.
  const [freshToken, setFreshToken] = React.useState<string | null>(null);

  const rows = keys.data?.keys ?? [];
  const live = rows.filter((k: ApiKey) => !k.revoked_at);
  const revoked = rows.filter((k: ApiKey) => k.revoked_at);

  return (
    <AppShell>
      <div className="container max-w-3xl space-y-6 py-8">
        <header>
          <Link href="/" className="text-xs text-muted-foreground hover:text-foreground">
            ← All projects
          </Link>
          <h1 className="mt-1 flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <KeyRound className="h-5 w-5 text-primary" />
            Agent access
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Let an assistant like Muse cut your footage. It connects to ReelForge with a
            key you create here. It can never publish anything or delete your work.
          </p>
        </header>

        <Card>
          <CardHeader>
            <CardTitle>Connect an agent</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label>MCP URL — paste this as a custom connector</Label>
              <CopyField value={connection.data?.mcp_url ?? '…'} label="MCP URL" />
            </div>
            {connection.data && !connection.data.reachable_publicly ? (
              <Alert>
                <AlertTriangle className="h-4 w-4" />
                <AlertTitle>Only reachable from this Mac</AlertTitle>
                <AlertDescription>
                  That address is local, so a phone can&apos;t reach it. Start the named
                  tunnel to get a public hostname:{' '}
                  <code className="font-mono text-xs">
                    docker compose -f compose.yml -f compose.named-tunnel.yml up -d
                  </code>
                </AlertDescription>
              </Alert>
            ) : null}

            <div className="space-y-1.5">
              <Label htmlFor="key-name">Key name</Label>
              <div className="flex gap-2">
                <Input
                  id="key-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Muse on my phone"
                />
                <Button
                  disabled={!name.trim() || createKey.isPending}
                  onClick={async () => {
                    const created = await createKey.mutateAsync(name.trim());
                    setFreshToken(created.token);
                  }}
                >
                  <Plus className="h-4 w-4" />
                  {createKey.isPending ? 'Creating…' : 'Create key'}
                </Button>
              </div>
            </div>

            {createKey.isError ? (
              <Alert variant="destructive">
                <AlertTitle>Couldn&apos;t create the key</AlertTitle>
                <AlertDescription>{humanMessage(createKey.error)}</AlertDescription>
              </Alert>
            ) : null}

            {freshToken ? (
              <Alert>
                <AlertTitle>Copy this key now</AlertTitle>
                <AlertDescription className="space-y-2">
                  <p className="text-xs">
                    It is shown once. ReelForge stores only a hash, so if you lose it you
                    will need to create another.
                  </p>
                  <CopyField value={freshToken} label="API key" />
                  <Button variant="ghost" size="sm" onClick={() => setFreshToken(null)}>
                    Done
                  </Button>
                </AlertDescription>
              </Alert>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Keys</CardTitle>
          </CardHeader>
          <CardContent>
            {keys.isLoading ? (
              <p className="text-sm text-muted-foreground">Loading…</p>
            ) : rows.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No keys yet. Create one above, then paste it into the agent along with the
                MCP URL.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {[...live, ...revoked].map((k: ApiKey) => (
                  <li key={k.id} className="flex items-center justify-between gap-3 py-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">{k.name}</span>
                        {k.revoked_at ? (
                          <Badge variant="secondary">revoked</Badge>
                        ) : null}
                      </div>
                      <p className="mt-0.5 font-mono text-xs text-muted-foreground">
                        {k.prefix}… · created {formatWhen(k.created_at)} · last used{' '}
                        {formatWhen(k.last_used_at)}
                      </p>
                    </div>
                    {k.revoked_at ? null : (
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={revokeKey.isPending}
                        onClick={() => revokeKey.mutate(k.id)}
                      >
                        Revoke
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  );
}
