"""Shared response schemas. Never return DB entities directly — go through these."""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Any, Literal

from pydantic import BaseModel


class ProjectOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    source_asset_id: str | None = None


class ProjectCreate(BaseModel):
    name: str


class ProjectList(BaseModel):
    projects: list[ProjectOut]
    total: int


class AssetOut(BaseModel):
    id: str
    project_id: str
    kind: str = "video"  # "video" | "photo"
    path: str
    original_filename: str
    duration_sec: float
    width: int
    height: int
    fps: float
    has_audio: bool
    size_bytes: int
    created_at: datetime
    # True once analysis.json exists on disk — lets the UI show readiness
    # without probing GET /assets/{id}/analysis (which 404s pre-analysis).
    analysis_ready: bool = False


class AssetList(BaseModel):
    assets: list[AssetOut]


# --------- uploads ----------


class UploadSessionCreate(BaseModel):
    filename: str
    content_type: str
    total_bytes: int
    chunk_size: int | None = None


class UploadSessionOut(BaseModel):
    id: str
    project_id: str
    filename: str
    content_type: str
    total_bytes: int
    received_bytes: int
    chunk_size: int
    status: Literal["active", "completed", "aborted"]
    asset_id: str | None = None
    created_at: datetime
    # Exact chunk indices present on disk. Chunks upload in parallel, so
    # received_bytes alone can't tell a resuming client WHICH chunks to skip.
    received_chunk_indices: list[int] = []


# --------- jobs ----------


JobKindLit = Literal["analyze", "select", "compose", "export", "publish"]
JobStatusLit = Literal["queued", "running", "done", "failed"]


class JobOut(BaseModel):
    id: str
    kind: JobKindLit
    status: JobStatusLit
    progress: float
    stage: str | None = None
    message: str | None = None
    logs: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class JobList(BaseModel):
    jobs: list[JobOut]


# --------- reels ----------


class ReelOut(BaseModel):
    id: str
    project_id: str
    asset_id: str
    rank: int
    title: str
    hook: str
    justification: str
    start_sec: float
    end_sec: float
    duration_sec: float
    overall_score: float
    suggested_mood: str
    scene_indices: list[int]
    scores: dict[str, int]
    mezzanine_ready: bool
    # Editor state: True when a saved timeline overrides the AI cut.
    has_edits: bool = False
    edited_duration_sec: Optional[float] = None
    # 0-100 match against the selection prompt; None when no prompt was used.
    prompt_relevance: Optional[int] = None
    # Selection v2: which generator proposed the reel (scene|sentence|moment)
    # and the ranker's literal first-2-seconds description. None on pre-v2 rows.
    source: Optional[str] = None
    opening_description: Optional[str] = None


class ReelList(BaseModel):
    reels: list[ReelOut]


# --------- exports ----------


class ExportOut(BaseModel):
    id: str
    reel_id: str
    preset_id: str
    output_path: str | None
    file_size_bytes: int | None
    created_at: datetime


class ExportList(BaseModel):
    exports: list[ExportOut]


class ExportCreate(BaseModel):
    preset_id: str
    force: bool = False


# --------- music ----------


class MusicTrackOut(BaseModel):
    id: str
    path: str
    source: str
    bpm: int | None
    mood: str
    duration_sec: float
    license: str
    attribution: str | None
