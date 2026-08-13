"""Pydantic data models shared by pipeline stages, the worker, the API, and the CLI.

Nothing downstream should pass around raw dicts — if it's on the wire or on disk,
it goes through one of these models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field

REELFORGE_VERSION = "0.5.0"  # Phase 4: export

# ---------------------------------------------------------------------------
# Scene / transcript / loudness / semantics
# ---------------------------------------------------------------------------


class Scene(BaseModel):
    index: int
    start_sec: float
    end_sec: float
    start_frame: int
    end_frame: int
    thumbnail_path: str  # relative to /data/working/{asset_id}/


class TranscriptWord(BaseModel):
    start: float
    end: float
    word: str
    probability: float


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    words: list[TranscriptWord]


class Transcript(BaseModel):
    language: str
    language_probability: float
    duration: float
    segments: list[TranscriptSegment]


class LoudnessPoint(BaseModel):
    time_sec: float
    lufs: float  # -inf is serialized as -80.0


Mood = Literal[
    "calm",
    "tense",
    "joyful",
    "somber",
    "energetic",
    "mysterious",
    "romantic",
    "triumphant",
    "melancholic",
    "neutral",
]

VisualEnergy = Literal["low", "medium", "high"]

MOOD_VALUES: tuple[str, ...] = (
    "calm",
    "tense",
    "joyful",
    "somber",
    "energetic",
    "mysterious",
    "romantic",
    "triumphant",
    "melancholic",
    "neutral",
)


class SceneSemantics(BaseModel):
    scene_index: int
    summary: str
    tags: list[str] = Field(min_length=3, max_length=7)
    mood: Mood
    has_speech: bool
    visual_energy: VisualEnergy
    cached: bool = False


# ---------------------------------------------------------------------------
# Config + report
# ---------------------------------------------------------------------------


class AnalysisConfig(BaseModel):
    scene_threshold: float = 27.0
    min_scene_duration: float = 2.0
    # Long-take splitting: scenes longer than max_scene_sec are split into
    # ~scene_split_target_sec pieces at speech pauses / loudness dips so raw
    # unedited footage (few hard cuts) still yields reel candidates.
    scene_split_enabled: bool = True
    max_scene_sec: float = 45.0
    scene_split_target_sec: float = 40.0
    whisper_model: str = "base.en"
    whisper_device: Literal["auto", "cpu", "cuda"] = "auto"
    whisper_compute_type: Literal["auto", "int8", "float16", "float32"] = "auto"
    semantics_model: str = "claude-haiku-4-5-20251001"
    semantics_concurrency: int = 5
    semantics_prompt_version: str = "v1"
    thumbnail_width: int = 480
    resume: bool = False


class UsageTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0


class AnalysisReport(BaseModel):
    asset_id: str
    source_path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    config: AnalysisConfig
    scenes: list[Scene]
    transcript: Transcript | None
    loudness: list[LoudnessPoint]
    semantics: list[SceneSemantics]
    created_at: str
    elapsed_sec: float
    reelforge_version: str
    anthropic_usage: dict


# ---------------------------------------------------------------------------
# Phase 2: reel selection
# ---------------------------------------------------------------------------


class ReelScores(BaseModel):
    narrative_coherence: int = Field(ge=0, le=100)
    hook_strength: int = Field(ge=0, le=100)
    emotional_payoff: int = Field(ge=0, le=100)
    standalone_clarity: int = Field(ge=0, le=100)

    @property
    def weighted(self) -> float:
        """Overall score. Weights sum to 1.0. If you change them, update
        docs + the determinism checks — this is the single source of truth."""
        return (
            0.35 * self.hook_strength
            + 0.30 * self.narrative_coherence
            + 0.20 * self.emotional_payoff
            + 0.15 * self.standalone_clarity
        )


class ReelCandidate(BaseModel):
    """Pre-ranking: a scene-aligned span. No title or scores yet."""

    candidate_id: str
    scene_indices: list[int]
    start_sec: float
    end_sec: float
    duration_sec: float
    scene_count: int


class RankedReel(BaseModel):
    """Post-ranking: candidate + LLM outputs + final rank."""

    candidate_id: str
    scene_indices: list[int]
    start_sec: float
    end_sec: float
    duration_sec: float
    title: str
    hook: str
    justification: str
    scores: ReelScores
    overall: float
    rank: int
    suggested_mood: Mood


OutputForm = Literal["short", "long_single", "long_montage"]


class SelectionConfig(BaseModel):
    # Output form controls the candidate-enumeration window:
    #   short          → reels in [target_min_sec, target_max_sec], default 30–60s
    #   long_single    → one big span centered on long_target_duration_sec
    #   long_montage   → same as short; downstream `compile_montage` stitches
    #                     top_k of them into a single longer mezzanine.
    output_form: OutputForm = "short"
    target_min_sec: float = 30.0
    target_max_sec: float = 60.0
    long_target_duration_sec: float | None = None  # used only when output_form="long_single"
    max_scenes_per_reel: int = 6
    top_k: int = 10
    overlap_threshold: float = 0.5
    ranking_model: str = "claude-sonnet-4-5"
    ranking_prompt_version: str = "v1"
    temperature: float = 0.0
    resume: bool = False

    @property
    def effective_min_sec(self) -> float:
        if self.output_form == "long_single" and self.long_target_duration_sec:
            return max(15.0, self.long_target_duration_sec * 0.85)
        return self.target_min_sec

    @property
    def effective_max_sec(self) -> float:
        if self.output_form == "long_single" and self.long_target_duration_sec:
            return self.long_target_duration_sec * 1.15
        return self.target_max_sec

    @property
    def effective_max_scenes(self) -> int:
        # Long spans cover more scenes; raise the ceiling so the enumerator
        # doesn't truncate the candidate set prematurely.
        if self.output_form == "long_single":
            return max(self.max_scenes_per_reel, 60)
        if self.output_form == "long_montage":
            return self.max_scenes_per_reel
        return self.max_scenes_per_reel


class ReelSelection(BaseModel):
    asset_id: str
    analysis_source: str
    config: SelectionConfig
    candidates_generated: int
    candidates_dropped_by_dedup: int
    reels: list[RankedReel]
    anthropic_usage: dict
    created_at: str
    elapsed_sec: float
    reelforge_version: str


# ---------------------------------------------------------------------------
# Phase 3: composition
# ---------------------------------------------------------------------------


class MusicTrack(BaseModel):
    id: str
    path: str
    source: Literal["bundled", "user"]
    bpm: int | None
    mood: Mood
    duration_sec: float
    license: str
    attribution: str | None = None


class CaptionStyle(BaseModel):
    mode: Literal["off", "static", "karaoke"] = "static"
    font_family: str = "Inter"
    font_size_px: int = 64
    primary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    outline_width_px: int = 4
    highlight_color: str = "&H0000FFFF"
    max_chars_per_line: int = 28
    max_lines: int = 2
    # Karaoke mode groups words into short single lines of at most this many
    # characters; the spoken word is highlighted within the visible line.
    karaoke_max_chars: int = 18
    position: Literal["lower_third", "centered", "top"] = "lower_third"
    safe_margin_pct: float = 0.15


class TransitionStyle(BaseModel):
    # "auto" is resolved at compose time by compose.auto.pick_transition_kind.
    kind: Literal[
        "auto", "fade", "fadeblack", "slideleft", "wipeleft", "dissolve", "cut"
    ] = "auto"
    duration_sec: float = 0.4


class EffectsConfig(BaseModel):
    ken_burns_on_low_energy: bool = True
    ken_burns_zoom: float = 1.10
    unsharp: bool = True
    unsharp_amount: float = 0.5
    # "auto" lets compose.auto.pick_lut_id choose from bundled LUTs by mood.
    # Any other string is treated as a literal LUT id.
    lut: str | None = "auto"
    # Reframing wider footage into portrait/square targets:
    #   auto      — subject-tracked crop for portrait/square, letterbox else
    #   crop      — always crop-track when the source is wider than the target
    #   letterbox — legacy scale+pad behavior
    reframe: Literal["auto", "crop", "letterbox"] = "auto"


Aspect = Literal["9:16", "16:9", "1:1"]


def _resolution_for(aspect: Aspect) -> tuple[int, int]:
    return {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
    }[aspect]


class ComposeConfig(BaseModel):
    aspect: Aspect = "9:16"
    # target_resolution is derived from aspect if not overridden.
    target_resolution: tuple[int, int] | None = None
    target_fps: int = 30
    # Overall encode quality. Adjusts BOTH encode stages (intermediate clips
    # and the mezzanine render) unless video_crf / video_preset were set to
    # non-default values explicitly (explicit always wins):
    #   draft    — fast iteration: clips ultrafast/20, mezzanine veryfast/20
    #   standard — clips ultrafast/18, mezzanine medium/18 (legacy behavior)
    #   high     — final delivery: clips fast/16, mezzanine slow/16
    quality: Literal["draft", "standard", "high"] = "standard"
    video_crf: int = 18
    video_preset: str = "medium"
    audio_bitrate_kbps: int = 256
    captions: CaptionStyle = Field(default_factory=CaptionStyle)
    transition: TransitionStyle = Field(default_factory=TransitionStyle)
    effects: EffectsConfig = Field(default_factory=EffectsConfig)
    music_track_id: str | None = None
    no_music: bool = False
    # Mid-scene trim offsets (Phase 7). Clamped to ±2s; the API enforces the
    # minimum-duration guard.
    trim_start_offset_sec: float = 0.0
    trim_end_offset_sec: float = 0.0
    # Speech-safe outer cuts: nudge the reel's first/last cut point off the
    # middle of a spoken word — extend up to the max nudge to include the
    # word, else drop the partial word entirely.
    speech_safe_cuts: bool = True
    speech_safe_max_nudge_sec: float = 0.6
    # Beat-synced transitions: shorten interior clips by up to the cap so
    # each crossfade midpoint lands on a beat of the chosen music track.
    beat_sync: bool = True
    beat_sync_max_adjust_sec: float = 0.45
    music_volume_db: float = -18.0
    voice_volume_db: float = -14.0
    # Final-mix loudness normalization: one loudnorm pass on the mixed bus so
    # every output lands at a consistent level. -14 LUFS integrated is the
    # normalization target used by YouTube / TikTok / Spotify.
    normalize_loudness: bool = True
    loudness_target_lufs: float = -14.0
    loudness_true_peak_db: float = -1.5
    ducking_threshold_db: float = -20.0
    ducking_ratio: float = 8.0
    ducking_attack_ms: float = 5.0
    ducking_release_ms: float = 250.0
    burn_title_card: bool = False
    seed: int = 1
    # When True (default), `transition.kind == "auto"` + `effects.lut == "auto"`
    # are resolved to mood-driven picks at compose time. Disabling smart_mode
    # treats those sentinels as literals (cut/null) — useful when reproducing
    # an exact render. See `compose/auto.py::resolve_smart_config`.
    smart_mode: bool = True

    @property
    def resolution(self) -> tuple[int, int]:
        return self.target_resolution or _resolution_for(self.aspect)

    @property
    def effective_mezz_preset(self) -> str:
        if self.video_preset != "medium":  # explicit override wins
            return self.video_preset
        return {"draft": "veryfast", "standard": "medium", "high": "slow"}[self.quality]

    @property
    def effective_mezz_crf(self) -> int:
        if self.video_crf != 18:  # explicit override wins
            return self.video_crf
        return {"draft": 20, "standard": 18, "high": 16}[self.quality]

    @property
    def clip_preset(self) -> str:
        return {"draft": "ultrafast", "standard": "ultrafast", "high": "fast"}[
            self.quality
        ]

    @property
    def clip_crf(self) -> int:
        return {"draft": 20, "standard": 18, "high": 16}[self.quality]


class ComposeManifest(BaseModel):
    asset_id: str
    reel_id: str
    reel_title: str
    reel_hook: str
    config: ComposeConfig
    chosen_music: MusicTrack | None
    mezzanine_path: str
    duration_sec: float
    width: int
    height: int
    fps: float
    scene_clip_map: list[dict]
    ffmpeg_version: str
    reelforge_version: str
    created_at: str
    elapsed_sec: float


# ---------------------------------------------------------------------------
# Phase 4: export presets
# ---------------------------------------------------------------------------


PresetId = Literal[
    "mp4_h264_social", "mp4_h265_hq", "mov_prores_422", "mov_prores_hq"
]


class PresetSpec(BaseModel):
    """Immutable declaration of a transcode preset. Defined in code, not user-editable."""

    id: PresetId
    container: Literal["mp4", "mov"]
    video_codec: Literal["libx264", "libx265", "prores_ks"]
    video_pixel_format: Literal["yuv420p", "yuv422p10le"]
    video_params: dict[str, str | int] = Field(default_factory=dict)
    audio_codec: Literal["aac", "pcm_s16le"]
    audio_bitrate_kbps: int | None = None
    container_flags: dict[str, str] = Field(default_factory=dict)
    target_use: str
    typical_size_ratio_vs_mezzanine: float


class ExportConfig(BaseModel):
    preset_id: PresetId
    force: bool = False


class ExportManifest(BaseModel):
    asset_id: str
    reel_id: str
    preset_id: PresetId
    preset_spec_version: str
    output_path: str
    input_mezzanine_path: str
    input_mezzanine_sha256: str
    container: str
    video_codec: str
    video_pixel_format: str
    audio_codec: str
    duration_sec: float
    width: int
    height: int
    fps: float
    file_size_bytes: int
    ffmpeg_version: str
    ffmpeg_command: list[str]
    reelforge_version: str
    created_at: str
    elapsed_sec: float


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

Stage = Literal["probe", "scenes", "transcribe", "loudness", "semantics"]

STAGE_WEIGHTS: dict[Stage, float] = {
    "probe": 0.02,
    "scenes": 0.08,
    "transcribe": 0.55,
    "loudness": 0.10,
    "semantics": 0.25,
}


@dataclass
class ProgressEvent:
    stage: Stage
    stage_progress: float
    overall_progress: float
    message: str | None = None


ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


async def noop_progress(_evt: ProgressEvent) -> None:
    return None


def compute_overall(stage: Stage, stage_progress: float) -> float:
    """Overall fraction done given the current stage and its local progress."""
    completed = 0.0
    for s, w in STAGE_WEIGHTS.items():
        if s == stage:
            completed += w * max(0.0, min(1.0, stage_progress))
            break
        completed += w
    return min(1.0, completed)
