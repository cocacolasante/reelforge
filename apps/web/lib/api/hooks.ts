'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from './client';
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
  ReelListSchema,
  ReelSchema,
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
    onSuccess: (_job, vars) => {
      qc.invalidateQueries({ queryKey: ['asset', vars.assetId] });
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
    queryFn: () => api('/health', { schema: HealthSchema }),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}
