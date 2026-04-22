export class APIError extends Error {
  constructor(
    public code: string,
    public status: number,
    message: string,
    public details?: Record<string, unknown>,
    public requestId?: string,
  ) {
    super(message);
    this.name = 'APIError';
  }
}

export function isClientError(err: unknown): boolean {
  return err instanceof APIError && err.status >= 400 && err.status < 500;
}

export const ERROR_MESSAGES: Record<string, string> = {
  PROJECT_NOT_FOUND: 'This project no longer exists.',
  ASSET_NOT_FOUND: "The source video couldn't be found.",
  REEL_NOT_FOUND: 'This reel is no longer available.',
  JOB_NOT_FOUND: "That job isn't around anymore.",
  EXPORT_NOT_FOUND: 'That export is no longer available.',
  UPLOAD_TOO_LARGE: 'That file is larger than the configured limit.',
  UPLOAD_UNSUPPORTED_TYPE: 'Only video files can be uploaded.',
  UPLOAD_SESSION_NOT_FOUND: 'That upload session no longer exists.',
  UPLOAD_CHUNK_OUT_OF_ORDER: 'A chunk was sent out of order.',
  UPLOAD_ALREADY_COMPLETED: 'This upload has already been finalized.',
  ANALYSIS_NOT_READY: 'Analyze the footage before continuing.',
  SELECTION_NOT_READY: 'Select reels before composing.',
  MEZZANINE_NOT_READY: 'Compose the reel before exporting.',
  JOB_ALREADY_RUNNING: 'That action is already in progress.',
  INVALID_PRESET: 'Unknown export preset.',
  INVALID_CONFIG: 'Some of the configuration is invalid.',
  NETWORK_ERROR: "Couldn't reach the server. Check your connection.",
  INVALID_RESPONSE: 'The server returned an unexpected response. Try reloading.',
  INTERNAL_ERROR: 'The server had a problem. Try again in a moment.',
};

export function humanMessage(err: unknown): string {
  if (err instanceof APIError) {
    return ERROR_MESSAGES[err.code] ?? err.message ?? 'Something went wrong.';
  }
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}
