"""The growth-agent <-> ReelForge queue contract, as validated models.

The authoritative prose lives in growth-agent's docs/reelforge-contract.md.
Keep the two in step; this file is the executable half.

Field names are camelCase on the wire because the producer is TypeScript. The
aliases keep Python code snake_case without a translation layer at every call
site.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Platform = Literal["instagram", "youtube", "tiktok"]
Aspect = Literal["9:16", "16:9", "1:1"]


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


class Wire(BaseModel):
    """Serialises camelCase, accepts either spelling on the way in."""

    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True)


class PlatformTarget(Wire):
    platform: Platform
    max_duration: float
    aspect: Aspect = "9:16"


class SelectionOverrides(Wire):
    top_k: int | None = None
    min_sec: float | None = None
    max_sec: float | None = None
    prompt: str | None = None


class ClipJob(Wire):
    """`reelforge-jobs` — the request.

    No credentials: `source_url` is a short-lived presigned GET, and variants are
    uploaded with this service's own S3 configuration under `output_prefix`.
    """

    job_id: str
    tenant_id: str
    source_video_id: str
    source_url: str
    output_prefix: str
    callback_queue: str = "reelforge-manifests"
    platform_targets: list[PlatformTarget] = Field(default_factory=list)
    selection: SelectionOverrides | None = None


class PrescoreStage(Wire):
    value: float
    version: str
    shortlisted: bool
    features: dict[str, Any]


class RankStage(Wire):
    narrative_coherence: int
    hook_strength: int
    emotional_payoff: int
    standalone_clarity: int
    weighted: float
    overall: float
    rank_position: int | None = None
    prompt_relevance: int | None = None
    source: str | None = None
    edit_style: str | None = None


class BoundaryStage(Wire):
    """Present for every clip; `adjusted` is False when refinement left it alone."""

    adjusted: bool
    pre_refine_start_sec: float | None = None
    pre_refine_end_sec: float | None = None
    start_delta_sec: float | None = None
    end_delta_sec: float | None = None


class StageScores(Wire):
    prescore: PrescoreStage | None = None
    rank: RankStage
    boundary: BoundaryStage


class Variant(Wire):
    platform: Platform
    aspect: Aspect
    duration_sec: float
    key: str
    bytes: int
    content_type: str = "video/mp4"


class VariantError(Wire):
    platform: Platform
    error: str


class ManifestClip(Wire):
    reelforge_clip_id: str
    start_sec: float
    end_sec: float
    duration_sec: float
    rank: int
    title: str
    hook: str
    stage_scores: StageScores
    variants: list[Variant] = Field(default_factory=list)
    variant_errors: list[VariantError] = Field(default_factory=list)


class SelectionProvenance(Wire):
    candidates_generated: int
    dropped_by_dedup: int
    dropped_by_diversity: int
    prescore_version: str | None = None


class Manifest(Wire):
    """`reelforge-manifests` — emitted exactly once per job, success or failure."""

    job_id: str
    tenant_id: str
    source_video_id: str
    status: Literal["completed", "failed"]
    error: str | None = None
    asset_id: str | None = None
    reelforge_version: str | None = None
    elapsed_sec: float = 0.0
    selection: SelectionProvenance | None = None
    clips: list[ManifestClip] = Field(default_factory=list)


class PerformanceLabels(Wire):
    completion_rate: float | None = None
    shares_per_view: float | None = None
    watch_time_pct: float | None = None
    verdict: Literal["strong", "average", "weak"] | None = None


class LabelIngest(Wire):
    """`reelforge-labels` — real-world performance, keyed by the stable clip id."""

    reelforge_clip_id: str
    labels: PerformanceLabels
    asset_id: str | None = None
    tenant_id: str | None = None
    observed_at: str | None = None
