"""Pydantic data models shared by pipeline stages, the worker, the API, and the CLI.

Nothing downstream should pass around raw dicts — if it's on the wire or on disk,
it goes through one of these models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from pydantic import field_validator, BaseModel, Field

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


class EnergyPoint(BaseModel):
    """Per-second combined energy sample (bin center i + 0.5, matching the
    loudness bin convention)."""

    time_sec: float
    motion: float  # mean abs downscaled-grey frame diff, 0..255 scale
    loudness_delta: float  # LUFS(t) - LUFS(t-1); 0.0 when either bin is silence


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
    # Per-second energy track: frames sampled at this rate for the motion
    # metric (part of the energy.json resume stamp).
    energy_sample_fps: float = 2.0
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
    # Per-second energy track (v2, additive — old analysis.json files load
    # with an empty list; the moment generator just won't run for them).
    energy: list[EnergyPoint] = Field(default_factory=list)
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
        docs + the determinism checks — this is the single source of truth.
        When SelectionConfig.prompt is set, rank.py blends this with
        prompt_relevance: overall = 0.45*relevance + 0.55*weighted."""
        return (
            0.35 * self.hook_strength
            + 0.30 * self.narrative_coherence
            + 0.20 * self.emotional_payoff
            + 0.15 * self.standalone_clarity
        )


CandidateSource = Literal["scene", "sentence", "moment"]


class ReelCandidate(BaseModel):
    """Pre-ranking: a time-bounded span. No title or scores yet.

    `start_sec`/`end_sec` are the authoritative bounds (and the identity —
    candidate_id hashes them); `scene_indices` lists the scenes that COVER
    the span, for compose's per-scene clip extraction. `source` names the
    generator that proposed it."""

    candidate_id: str
    scene_indices: list[int]
    start_sec: float
    end_sec: float
    duration_sec: float
    scene_count: int
    source: CandidateSource = "scene"


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
    # 0-100 match against SelectionConfig.prompt; None when no prompt was used.
    prompt_relevance: int | None = Field(default=None, ge=0, le=100)
    # Which generator proposed the winning candidate (default keeps old
    # reels.json files parseable).
    source: CandidateSource = "scene"
    # v2 listwise ranking: the model's explicit order (1 = best) and its
    # literal description of the first 2 seconds. Additive; None on old files.
    rank_position: int | None = Field(default=None, ge=1)
    opening_description: str | None = None
    # CP7 boundary refinement: the pre-refinement bounds, set only when
    # refinement actually moved an edge. candidate_id never changes.
    pre_refine_start_sec: float | None = None
    pre_refine_end_sec: float | None = None


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
    # High cap: the enumerator already breaks on duration > effective_max_sec,
    # so a low scene-count cap only starves fast-cut footage (3-4s scenes can
    # never reach 30s at 6 scenes).
    max_scenes_per_reel: int = 40
    # Union cap across all candidate generators (sentence kept first, then
    # scene, then moment; even time-stride within a truncated generator).
    max_candidates: int = Field(default=400, ge=1)
    # How many prescore-ranked candidates the (single) ranking call sees.
    shortlist_size: int = Field(default=40, ge=1)
    # Best-effort boundary refinement of the top-K (one extra small API call);
    # failures keep the unrefined bounds.
    refine: bool = True
    # MMR diversity strength in overall-score points (0 disables). Halved when
    # a prompt is set — the user asked for a theme, don't fight it.
    diversity_lambda: float = Field(default=8.0, ge=0)
    top_k: int = 10
    overlap_threshold: float = 0.5
    ranking_model: str = "claude-sonnet-4-5"
    ranking_prompt_version: str = "v2"
    temperature: float = 0.0
    resume: bool = False
    # Natural-language direction, e.g. "clips of falls", "make it feel intense".
    # Steers ranking (prompt_relevance gate + blend) and style (suggested_mood).
    prompt: str | None = Field(default=None, max_length=500)

    @field_validator("prompt", mode="before")
    @classmethod
    def _clean_prompt(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

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
    # Reels displaced from the top-k by the MMR diversity re-rank (additive;
    # 0 on old files and when diversity_lambda=0).
    candidates_dropped_by_diversity: int = 0
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
    # Transcribe voiceover takes and caption them too. While a take plays,
    # its words replace any footage captions (the footage is ducked anyway).
    caption_voiceover: bool = True
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


class PhotoInsert(BaseModel):
    """A still photo woven into a reel as its own shot.

    `position` is an index into the reel's shot sequence: 0 places the photo
    before the first video clip, N after the Nth clip (so N == clip count
    puts it at the end). The API fills `path` from the asset id so the
    compose pipeline never needs database access.
    """

    asset_id: str
    path: str
    position: int = 0
    duration_sec: float = 3.0
    ken_burns: bool = True


# ---------------------------------------------------------------------------
# Editable timeline (post-generation editing)
# ---------------------------------------------------------------------------


class TimelineShot(BaseModel):
    """One shot in an edited reel.

    video: an arbitrary [in_ts, out_ts] range of any project video — not
           limited to detected scenes.
    photo: a still held for duration_sec with a baked-in drift.

    `path` is filled by the API at enqueue (asset id -> on-disk path) so the
    compose pipeline never needs database access. `transition_after`
    overrides the reel-wide transition for the cut that FOLLOWS this shot.
    """

    kind: Literal["video", "photo"]
    asset_id: str
    path: str = ""
    in_ts: float = 0.0
    out_ts: float = 0.0
    duration_sec: float = 3.0
    ken_burns: bool = True
    transition_after: "TransitionStyle | None" = None
    # Source-audio gain for this shot (1.0 = as recorded, 0..3). Muted shots
    # keep their audio stream (the crossfade chain needs one) at zero gain.
    volume: float = 1.0
    muted: bool = False

    @property
    def effective_gain(self) -> float:
        if self.muted:
            return 0.0
        return max(0.0, min(3.0, self.volume))

    @property
    def duration(self) -> float:
        if self.kind == "photo":
            return max(0.2, self.duration_sec)
        return max(0.1, self.out_ts - self.in_ts)


class TextOverlay(BaseModel):
    """Burned-in text on the mezzanine timeline (seconds from reel start).

    Colors use ASS &HAABBGGRR notation like CaptionStyle; the web UI converts
    from hex. Rendered through the same subtitle pass as captions, so it
    costs nothing extra at render time.
    """

    id: str = ""
    text: str
    start_sec: float
    end_sec: float
    position: Literal["top", "center", "bottom"] = "center"
    font_size_px: int = 84
    color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    bold: bool = True
    fade_ms: int = 250


class VoiceoverTake(BaseModel):
    """One recorded voiceover take, placed on the mezzanine timeline.

    Takes are audio-only assets (kind="audio") uploaded from the browser's
    recorder. `path` is resolved by the API at enqueue. Several takes can
    cover different parts of a reel ("record in cuts"); they're mixed under
    the footage audio, which ducks beneath them.
    """

    id: str = ""
    asset_id: str
    path: str = ""
    start_sec: float = 0.0
    duration_sec: float = 0.0
    volume: float = 1.0
    muted: bool = False
    label: str = ""

    @property
    def effective_gain(self) -> float:
        if self.muted:
            return 0.0
        return max(0.0, min(3.0, self.volume))


class ReelTimeline(BaseModel):
    shots: list[TimelineShot] = Field(default_factory=list)
    overlays: list[TextOverlay] = Field(default_factory=list)
    voiceovers: list[VoiceoverTake] = Field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.shots)


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
    # Still photos inserted into the shot sequence (see PhotoInsert).
    photo_inserts: list[PhotoInsert] = Field(default_factory=list)
    # Edited timeline. When set it is the complete shot list (video ranges,
    # photos, per-cut transitions, text overlays) and replaces the
    # scene-derived shots, trim offsets and photo_inserts entirely.
    timeline: ReelTimeline | None = None
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
    # Footage audio ducks beneath voiceover takes (sidechain keyed on the
    # voiceover mix); music already ducks beneath the whole voice bus.
    voiceover_ducking: bool = True
    voiceover_volume_db: float = -12.0
    voiceover_whisper_model: str = "base.en"
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

Stage = Literal["probe", "scenes", "transcribe", "loudness", "energy", "semantics"]

# compute_overall walks this dict in insertion order — keys MUST stay in
# pipeline execution order.
STAGE_WEIGHTS: dict[Stage, float] = {
    "probe": 0.02,
    "scenes": 0.08,
    "transcribe": 0.55,
    "loudness": 0.10,
    "energy": 0.03,
    "semantics": 0.22,
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
