"""Helpers for building synthetic AnalysisReports in tests. No Docker, no FFmpeg."""

from __future__ import annotations

from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    LoudnessPoint,
    REELFORGE_VERSION,
    Scene,
    SceneSemantics,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)


def make_scenes(durations_sec: list[float], fps: float = 30.0) -> list[Scene]:
    scenes: list[Scene] = []
    start = 0.0
    for i, d in enumerate(durations_sec):
        end = start + d
        scenes.append(
            Scene(
                index=i,
                start_sec=start,
                end_sec=end,
                start_frame=int(round(start * fps)),
                end_frame=int(round(end * fps)),
                thumbnail_path=f"thumbs/scene_{i:04d}.jpg",
            )
        )
        start = end
    return scenes


def make_semantics(n: int) -> list[SceneSemantics]:
    moods = [
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
    energies = ["low", "medium", "high"]
    return [
        SceneSemantics(
            scene_index=i,
            summary=f"Scene {i} does a thing.",
            tags=[f"t{i}a", f"t{i}b", f"t{i}c"],
            mood=moods[i % len(moods)],  # type: ignore[arg-type]
            has_speech=i % 2 == 0,
            visual_energy=energies[i % len(energies)],  # type: ignore[arg-type]
        )
        for i in range(n)
    ]


def make_transcript(scenes: list[Scene]) -> Transcript:
    segs: list[TranscriptSegment] = []
    for s in scenes:
        segs.append(
            TranscriptSegment(
                start=s.start_sec,
                end=s.end_sec,
                text=f" words for scene {s.index}",
                words=[
                    TranscriptWord(
                        start=s.start_sec,
                        end=s.start_sec + 0.5,
                        word=f" scene{s.index}",
                        probability=0.99,
                    )
                ],
            )
        )
    if not segs:
        return Transcript(language="en", language_probability=1.0, duration=0.0, segments=[])
    return Transcript(
        language="en",
        language_probability=0.99,
        duration=segs[-1].end,
        segments=segs,
    )


def make_loudness(duration_sec: float) -> list[LoudnessPoint]:
    n = max(0, int(duration_sec))
    return [
        LoudnessPoint(time_sec=i + 0.5, lufs=-18.0 - (i % 5))
        for i in range(n)
    ]


def make_analysis(
    asset_id: str,
    scene_durations: list[float],
    *,
    with_audio: bool = True,
) -> AnalysisReport:
    scenes = make_scenes(scene_durations)
    semantics = make_semantics(len(scenes))
    transcript = make_transcript(scenes) if with_audio else None
    total_duration = sum(scene_durations)
    loudness = make_loudness(total_duration) if with_audio else []
    return AnalysisReport(
        asset_id=asset_id,
        source_path=f"/data/inbox/{asset_id}.mp4",
        duration=total_duration,
        width=1920,
        height=1080,
        fps=30.0,
        has_audio=with_audio,
        config=AnalysisConfig(),
        scenes=scenes,
        transcript=transcript,
        loudness=loudness,
        semantics=semantics,
        created_at="2026-04-22T00:00:00+00:00",
        elapsed_sec=1.0,
        reelforge_version=REELFORGE_VERSION,
        anthropic_usage={"input_tokens": 0, "output_tokens": 0, "cache_hits": 0},
    )
