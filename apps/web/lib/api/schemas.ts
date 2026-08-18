import { z } from 'zod';

// --- Projects ---------------------------------------------------------------

export const ProjectSchema = z.object({
  id: z.string(),
  name: z.string(),
  created_at: z.string(),
  source_asset_id: z.string().nullable(),
});
export const ProjectListSchema = z.object({
  projects: z.array(ProjectSchema),
  total: z.number(),
});
export type Project = z.infer<typeof ProjectSchema>;

// --- Assets -----------------------------------------------------------------

export const AssetSchema = z.object({
  id: z.string(),
  project_id: z.string(),
  kind: z.string(),
  path: z.string(),
  original_filename: z.string(),
  duration_sec: z.number(),
  width: z.number(),
  height: z.number(),
  fps: z.number(),
  has_audio: z.boolean(),
  size_bytes: z.number(),
  created_at: z.string(),
  analysis_ready: z.boolean(),
});
export const AssetListSchema = z.object({ assets: z.array(AssetSchema) });
export type Asset = z.infer<typeof AssetSchema>;

// --- Uploads ----------------------------------------------------------------

export const UploadSessionSchema = z.object({
  id: z.string(),
  project_id: z.string(),
  filename: z.string(),
  content_type: z.string(),
  total_bytes: z.number(),
  received_bytes: z.number(),
  chunk_size: z.number(),
  status: z.enum(['active', 'completed', 'aborted']),
  asset_id: z.string().nullable(),
  created_at: z.string(),
  received_chunk_indices: z.array(z.number()),
});
export type UploadSession = z.infer<typeof UploadSessionSchema>;

// --- Analysis (pass-through — large shape) ---------------------------------

export const SceneSchema = z.object({
  index: z.number(),
  start_sec: z.number(),
  end_sec: z.number(),
  start_frame: z.number(),
  end_frame: z.number(),
  thumbnail_path: z.string(),
});
export const SceneSemanticsSchema = z.object({
  scene_index: z.number(),
  summary: z.string(),
  tags: z.array(z.string()),
  mood: z.string(),
  has_speech: z.boolean(),
  visual_energy: z.string(),
  cached: z.boolean().default(false),
});
export const LoudnessPointSchema = z.object({
  time_sec: z.number(),
  lufs: z.number(),
});
export const AnalysisReportSchema = z.object({
  asset_id: z.string(),
  source_path: z.string(),
  duration: z.number(),
  width: z.number(),
  height: z.number(),
  fps: z.number(),
  has_audio: z.boolean(),
  scenes: z.array(SceneSchema),
  semantics: z.array(SceneSemanticsSchema),
  loudness: z.array(LoudnessPointSchema),
  transcript: z.any().nullable(),
  anthropic_usage: z.record(z.any()),
  created_at: z.string(),
  elapsed_sec: z.number(),
  reelforge_version: z.string(),
  config: z.record(z.any()),
});
export type AnalysisReport = z.infer<typeof AnalysisReportSchema>;

// --- Reels ------------------------------------------------------------------

export const ReelScoresSchema = z.object({
  narrative_coherence: z.number(),
  hook_strength: z.number(),
  emotional_payoff: z.number(),
  standalone_clarity: z.number(),
});
export const ReelSchema = z.object({
  id: z.string(),
  project_id: z.string(),
  asset_id: z.string(),
  rank: z.number(),
  title: z.string(),
  hook: z.string(),
  justification: z.string(),
  start_sec: z.number(),
  end_sec: z.number(),
  duration_sec: z.number(),
  overall_score: z.number(),
  suggested_mood: z.string(),
  scene_indices: z.array(z.number()),
  scores: ReelScoresSchema,
  mezzanine_ready: z.boolean(),
});
export const ReelListSchema = z.object({ reels: z.array(ReelSchema) });
export type Reel = z.infer<typeof ReelSchema>;

// --- Jobs -------------------------------------------------------------------

export const JobSchema = z.object({
  id: z.string(),
  kind: z.enum(['analyze', 'select', 'compose', 'export', 'publish']),
  status: z.enum(['queued', 'running', 'done', 'failed']),
  progress: z.number(),
  stage: z.string().nullable(),
  message: z.string().nullable(),
  logs: z.array(z.record(z.any())),
  result: z.record(z.any()).nullable(),
  error: z.string().nullable(),
  started_at: z.string().nullable().optional(),
  finished_at: z.string().nullable().optional(),
  created_at: z.string(),
});
export type Job = z.infer<typeof JobSchema>;

// --- Music ------------------------------------------------------------------

export const MusicTrackSchema = z.object({
  id: z.string(),
  path: z.string(),
  source: z.string(),
  bpm: z.number().nullable(),
  mood: z.string(),
  duration_sec: z.number(),
  license: z.string(),
  attribution: z.string().nullable(),
});
export const MusicListSchema = z.object({ tracks: z.array(MusicTrackSchema) });
export type MusicTrack = z.infer<typeof MusicTrackSchema>;

// --- Exports ---------------------------------------------------------------

export const ExportSchema = z.object({
  id: z.string(),
  reel_id: z.string(),
  preset_id: z.string(),
  output_path: z.string().nullable(),
  file_size_bytes: z.number().nullable(),
  created_at: z.string(),
});
export const ExportListSchema = z.object({ exports: z.array(ExportSchema) });
export type ExportItem = z.infer<typeof ExportSchema>;

// --- Health -----------------------------------------------------------------

export const HealthSchema = z.object({
  status: z.string(),
  ffmpeg: z.string(),
  redis: z.string(),
  anthropic_configured: z.boolean(),
  anthropic_reachable: z.boolean(),
  whisper_models_cache_gb: z.number().optional(),
  data_dir: z.string().optional(),
  environment: z.string().optional(),
});

// --- Social publishing ------------------------------------------------------

export const SocialAccountSchema = z.object({
  id: z.string(),
  platform: z.string(),
  external_id: z.string(),
  display_name: z.string().nullable(),
  connected_at: z.string(),
});
export const SocialAccountListSchema = z.object({
  accounts: z.array(SocialAccountSchema),
});
export type SocialAccount = z.infer<typeof SocialAccountSchema>;

export const PublicationSchema = z.object({
  id: z.string(),
  reel_id: z.string(),
  platform: z.string(),
  channel_title: z.string().nullable(),
  preset_id: z.string(),
  title: z.string(),
  privacy: z.string(),
  status: z.string(),
  publish_job_id: z.string().nullable(),
  video_id: z.string().nullable(),
  video_url: z.string().nullable(),
  error_message: z.string().nullable(),
  created_at: z.string(),
  completed_at: z.string().nullable(),
});
export const PublicationListSchema = z.object({
  publications: z.array(PublicationSchema),
});
export type Publication = z.infer<typeof PublicationSchema>;
