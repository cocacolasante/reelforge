import { z } from 'zod';
import { APIError } from './errors';

export const API_BASE =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_API_URL) ||
  'http://localhost:8000';

function randomId(): string {
  if (
    typeof crypto !== 'undefined' &&
    'randomUUID' in crypto &&
    typeof crypto.randomUUID === 'function'
  ) {
    return crypto.randomUUID().replace(/-/g, '');
  }
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export interface ApiOpts<T> extends Omit<RequestInit, 'body'> {
  body?: unknown;           // serialized to JSON unless it's FormData / Blob / BufferSource
  schema?: z.ZodType<T>;    // validate the response
  query?: Record<string, string | number | boolean | undefined | null>;
  expectStatus?: number[];  // default [200, 201, 204]
  rawBody?: boolean;        // skip JSON serialization (for chunked PUT)
}

export async function api<T = unknown>(path: string, opts: ApiOpts<T> = {}): Promise<T> {
  const {
    body,
    schema,
    query,
    expectStatus = [200, 201, 204],
    rawBody = false,
    headers: headersInit,
    ...rest
  } = opts;

  const url = new URL(
    path.startsWith('http') ? path : API_BASE + (path.startsWith('/api') ? path : `/api/v1${path}`),
  );
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null) continue;
      url.searchParams.set(k, String(v));
    }
  }

  const requestId = randomId();
  const headers = new Headers(headersInit);
  headers.set('X-Request-Id', requestId);

  let fetchBody: BodyInit | undefined;
  if (body !== undefined && body !== null) {
    if (rawBody) {
      fetchBody = body as BodyInit;
    } else if (
      body instanceof FormData ||
      body instanceof Blob ||
      body instanceof ArrayBuffer ||
      ArrayBuffer.isView(body as ArrayBufferView)
    ) {
      fetchBody = body as BodyInit;
    } else {
      fetchBody = JSON.stringify(body);
      if (!headers.has('content-type')) headers.set('content-type', 'application/json');
    }
  }

  let resp: Response;
  try {
    resp = await fetch(url.toString(), { ...rest, headers, body: fetchBody });
  } catch (err) {
    throw new APIError(
      'NETWORK_ERROR',
      0,
      err instanceof Error ? err.message : 'network failure',
      undefined,
      requestId,
    );
  }

  const contentType = resp.headers.get('content-type') ?? '';
  let parsed: unknown = null;
  if (resp.status !== 204 && contentType.includes('application/json')) {
    try {
      parsed = await resp.json();
    } catch {
      parsed = null;
    }
  } else if (resp.status !== 204) {
    try {
      parsed = await resp.text();
    } catch {
      parsed = null;
    }
  }

  if (!expectStatus.includes(resp.status)) {
    if (parsed && typeof parsed === 'object' && 'error' in (parsed as Record<string, unknown>)) {
      const envelope = parsed as { error: { code: string; message: string; details?: Record<string, unknown> }; request_id?: string };
      throw new APIError(
        envelope.error?.code ?? 'INTERNAL_ERROR',
        resp.status,
        envelope.error?.message ?? resp.statusText,
        envelope.error?.details,
        envelope.request_id ?? requestId,
      );
    }
    throw new APIError(
      resp.status >= 500 ? 'INTERNAL_ERROR' : 'INVALID_RESPONSE',
      resp.status,
      `${resp.status} ${resp.statusText}`,
      undefined,
      requestId,
    );
  }

  if (resp.status === 204) return undefined as T;

  if (schema) {
    const result = schema.safeParse(parsed);
    if (!result.success) {
      throw new APIError(
        'INVALID_RESPONSE',
        resp.status,
        `response schema mismatch: ${result.error.issues[0]?.message ?? 'unknown'}`,
        { issues: result.error.issues },
        requestId,
      );
    }
    return result.data;
  }

  return parsed as T;
}
