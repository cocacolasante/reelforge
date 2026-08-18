'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { API_BASE, api } from './client';
import {
  AnalysisReportSchema,
  AssetListSchema,
  AssetSchema,
  ExportListSchema,
  ExportSchema,
  HealthSchema,
  JobSchema,
  MusicListSchema,
  ProjectListSchema,
  ProjectSchema,
  PublicationListSchema,
  ReelListSchema,
  ReelSchema,
  SocialAccountListSchema,
  UploadSessionSchema,
  type Asset,
  type Job,
  type Project,
} from './schemas';

// ---------- projects ----------

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: () => api('/projects', { schema: ProjectListSchema }),
  });
}

export function useProject(projectId: string | undefined) {
  return useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api(`/projects/${projectId}`, { schema: ProjectSchema }),
    enabled: !!projectId,
  });
}

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { name: string }) =>
      api<Project>('/projects', { method: 'POST', body: input, schema: ProjectSchema }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['projects'] }),
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) =>
      api<void>(`/projects/${projectId}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['projects'] }),
  });
}

// ---------- assets ----------

export function useProjectAssets(projectId: string | undefined) {
  return useQuery({
    queryKey: ['assets', projectId],
    queryFn: () =>
      api(`/projects/${projectId}/assets`, { schema: AssetListSchema }),
    enabled: !!projectId,
  });
}

export function useAsset(assetId: string | undefined) {
  return useQuery({
    queryKey: ['asset', assetId],
    queryFn: () => api(`/assets/${assetId}`, { schema: AssetSchema }),
    enabled: !!assetId,
  });
}

// ---------- analysis ----------

export function useAnalysis(assetId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ['analysis', assetId],
    queryFn: () =>
      api(`/assets/${assetId}/analysis`, { schema: AnalysisReportSchema }),
    enabled: !!assetId && enabled,
    retry: false,
  });
}

export function useEnqueueAnalyze() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ assetId, config }: { assetId: string; config?: Record<string, unknown> }) =>
      api<Job>(`/assets/${assetId}/analyze`, {
        method: 'POST',
        body: config ?? {},
        schema: JobSchema,
      }),
    onSuccess: () => {
      // Readiness lives on the asset list (analysis_ready); nothing
      // subscribes to a per-asset key.
      qc.invalidateQueries({ queryKey: ['assets'] });
    },
  });
}

export function useEnqueueSelect() {
  return useMutation({
    mutationFn: ({ assetId, config }: { assetId: string; config?: Record<string, unknown> }) =>
      api<Job>(`/assets/${assetId}/select`, {
        method: 'POST',
        body: config ?? {},
        schema: JobSchema,
      }),
  });
}

// ---------- reels ----------

export function useReelsForAsset(assetId: string | undefined) {
  return useQuery({
    queryKey: ['reels', assetId],
    queryFn: () => api(`/assets/${assetId}/reels`, { schema: ReelListSchema }),
    enabled: !!assetId,
    retry: false,
  });
}

export function useReel(reelId: string | undefined) {
  return useQuery({
    queryKey: ['reel', reelId],
    queryFn: () => api(`/reels/${reelId}`, { schema: ReelSchema }),
    enabled: !!reelId,
  });
}

export function useEnqueueCompose() {
  return useMutation({
    mutationFn: ({ reelId, config }: { reelId: string; config: Record<string, unknown> }) =>
      api<Job>(`/reels/${reelId}/compose`, {
        method: 'POST',
        body: config,
        schema: JobSchema,
      }),
  });
}

// ---------- jobs ----------

export function useJob(jobId: string | undefined, refetchMs: number | false = 1500) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api(`/jobs/${jobId}`, { schema: JobSchema }),
    enabled: !!jobId,
    refetchInterval: (q) => {
      const data = q.state.data as Job | undefined;
      if (!data) return refetchMs;
      if (data.status === 'done' || data.status === 'failed') return false;
      return refetchMs;
    },
  });
}

// ---------- music ----------

export function useMusicLibrary() {
  return useQuery({
    queryKey: ['music'],
    queryFn: () => api('/music', { schema: MusicListSchema }),
  });
}

// ---------- exports ----------

export function useExports(reelId: string | undefined) {
  return useQuery({
    queryKey: ['exports', reelId],
    queryFn: () =>
      api(`/reels/${reelId}/exports`, { schema: ExportListSchema }),
    enabled: !!reelId,
  });
}

export function useEnqueueExport() {
  return useMutation({
    mutationFn: ({ reelId, presetId, force = false }: { reelId: string; presetId: string; force?: boolean }) =>
      api<Job>(`/reels/${reelId}/exports`, {
        method: 'POST',
        body: { preset_id: presetId, force },
        schema: JobSchema,
      }),
  });
}

// ---------- health ----------

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    // /health is served at the API root, not under /api/v1 — call it absolutely
    // so the client's prefixing logic doesn't turn it into /api/v1/health (404).
    queryFn: () => api(`${API_BASE}/health`, { schema: HealthSchema }),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}

// ---------- social publishing ----------

export function useSocialAccounts() {
  return useQuery({
    queryKey: ['social-accounts'],
    queryFn: () => api('/social/accounts', { schema: SocialAccountListSchema }),
  });
}

export function usePublications(reelId: string | undefined) {
  return useQuery({
    queryKey: ['publications', reelId],
    queryFn: () =>
      api(`/reels/${reelId}/publications`, { schema: PublicationListSchema }),
    enabled: !!reelId,
  });
}

export function usePublishReel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      reelId,
      body,
    }: {
      reelId: string;
      body: {
        platform: string;
        account_id: string;
        preset_id: string;
        title: string;
        description: string;
        privacy: string;
      };
    }) =>
      api<Job>(`/reels/${reelId}/publish`, {
        method: 'POST',
        body,
        schema: JobSchema,
      }),
    onSuccess: (_job, vars) => {
      qc.invalidateQueries({ queryKey: ['publications', vars.reelId] });
    },
  });
}

export function useDeleteAsset(projectId: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (assetId: string) =>
      api<void>(`/assets/${assetId}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['assets', projectId] });
      qc.invalidateQueries({ queryKey: ['project-reels', projectId] });
      qc.invalidateQueries({ queryKey: ['project', projectId] });
    },
  });
}

/** Photos belonging to a project — candidates for insertion into a reel. */
export function useProjectPhotos(projectId: string | undefined) {
  const q = useProjectAssets(projectId);
  return {
    ...q,
    photos: (q.data?.assets ?? []).filter((a) => a.kind === 'photo'),
  };
}
