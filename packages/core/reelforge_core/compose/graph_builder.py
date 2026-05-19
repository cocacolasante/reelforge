"""Assemble the final-render FFmpeg command: clips + xfade + acrossfade + effects
+ captions + music sidechain mix.

Returns an argv list so the caller can choose how to invoke it. The returned
graph is verified by the DSL layer — duplicate labels or unbalanced inputs
raise GraphError at construction time, not at FFmpeg runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.graph import FilterGraph, FilterNode
from reelforge_core.models import AnalysisReport, ComposeConfig

# Map our transition.kind to xfade's `transition=` value. "cut" is implemented as
# a 40ms fade so the graph shape stays uniform.
_TRANSITION_MAP = {
    # "auto" should be resolved before we reach the graph builder (see
    # compose/auto.py::resolve_smart_config). Keep a defensive fall-through
    # to plain fade just in case.
    "auto": "fade",
    "fade": "fade",
    "fadeblack": "fadeblack",
    "slideleft": "slideleft",
    "wipeleft": "wipeleft",
    "dissolve": "dissolve",
    "cut": "fade",
}


@dataclass
class RenderPlan:
    args: list[str]
    mezzanine_duration_sec: float
    music_input_index: int | None
    filter_complex: str


def _transition_duration(config: ComposeConfig) -> float:
    return 0.04 if config.transition.kind == "cut" else config.transition.duration_sec


def _xfade_offsets(
    clip_durations: list[float], xfade_dur: float
) -> list[float]:
    """Offsets[i] is the xfade offset for the transition between clip i and i+1.
    Length = len(clips) - 1. See spec §7 for the math."""
    if len(clip_durations) <= 1:
        return []
    offsets: list[float] = []
    cumulative = 0.0
    for i, dur in enumerate(clip_durations[:-1]):
        # Offset for transition i is cumulative duration up through clip i, minus xfade_dur,
        # minus (i transitions that already overlapped by xfade_dur each).
        cumulative += dur
        offsets.append(cumulative - (i + 1) * xfade_dur)
    return offsets


def build_final_command(
    *,
    clips: list[ClipInfo],
    analysis: AnalysisReport,
    music_path: Path | None,
    captions_path: Path | None,
    config: ComposeConfig,
    output_path: Path,
) -> RenderPlan:
    if not clips:
        raise ValueError("build_final_command requires at least one clip")

    width, height = config.resolution
    fps = config.target_fps
    xfade_dur = _transition_duration(config)
    transition_kind = _TRANSITION_MAP[config.transition.kind]

    # ----- input args -----
    # -stats forces ffmpeg to emit `time=...` lines on stderr even when
    # -loglevel is warning — required for the render progress watcher.
    args: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y"]
    for clip in clips:
        args += ["-i", str(clip.path)]
    music_input_index: int | None = None
    if music_path is not None:
        music_input_index = len(clips)
        args += ["-i", str(music_path)]

    graph = FilterGraph()

    # ----- normalise each clip's video/audio timebase -----
    v_labels: list[str] = []
    a_labels: list[str] = []
    durations = [c.duration for c in clips]

    # Determine low-energy scenes for Ken Burns
    low_energy_by_idx: set[int] = set()
    if config.effects.ken_burns_on_low_energy:
        for sem in analysis.semantics:
            if sem.visual_energy == "low":
                low_energy_by_idx.add(sem.scene_index)

    for i, clip in enumerate(clips):
        raw_v = f"[{i}:v]"
        raw_a = f"[{i}:a]"
        v_out = f"[v{i}]"
        a_out = f"[a{i}]"

        # Video: format → settb → setpts → optional Ken Burns → final out label
        v_prep_out = f"[vp{i}]"
        graph.add(
            FilterNode(
                filter_name="format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS",
                inputs=[raw_v],
                outputs=[v_prep_out],
            )
        )
        if clip.scene_index in low_energy_by_idx and config.effects.ken_burns_on_low_energy:
            # z ramps from 1.0 to ken_burns_zoom over d frames
            d_frames = max(1, int(round(clip.duration * fps)))
            target_zoom = config.effects.ken_burns_zoom
            # Step per frame to land near target_zoom at the last frame
            step = (target_zoom - 1.0) / max(1, d_frames - 1)
            # Raw string — zoompan expression syntax uses commas and colons that
            # must not be escaped by our DSL (the whole z value is one arg).
            zoompan_args = {
                "z": f"min(zoom+{step:.6f}\\,{target_zoom:.3f})",
                "d": str(d_frames),
                "s": f"{width}x{height}",
            }
            graph.add(
                FilterNode(
                    filter_name="zoompan",
                    inputs=[v_prep_out],
                    outputs=[v_out],
                    args=zoompan_args,
                )
            )
        else:
            # Rename label so downstream math is uniform.
            graph.add(
                FilterNode(
                    filter_name="null",
                    inputs=[v_prep_out],
                    outputs=[v_out],
                )
            )

        v_labels.append(v_out)

        if clip.has_audio:
            graph.add(
                FilterNode(
                    filter_name="aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS",
                    inputs=[raw_a],
                    outputs=[a_out],
                )
            )
        else:
            # Synthesize silence of matching duration from lavfi. We pre-built
            # clips to always have audio in extract_clips (silent → -an). Here
            # we handle audio-less clips by using anullsrc as a per-clip input;
            # but the simpler guarantee from §4 is: audio is present if source
            # had audio. For now, generate silent track inline via aevalsrc.
            graph.add(
                FilterNode(
                    filter_name=f"aevalsrc=exprs=0:duration={clip.duration:.3f}:sample_rate=48000:channel_layout=stereo,asetpts=PTS-STARTPTS",
                    inputs=[],
                    outputs=[a_out],
                )
            )
        a_labels.append(a_out)

    # ----- xfade chain -----
    offsets = _xfade_offsets(durations, xfade_dur)
    v_chain = v_labels[0]
    a_chain = a_labels[0]
    for i, offset in enumerate(offsets):
        next_v = f"[xv{i}]"
        next_a = f"[xa{i}]"
        graph.add(
            FilterNode(
                filter_name="xfade",
                inputs=[v_chain, v_labels[i + 1]],
                outputs=[next_v],
                args={
                    "transition": transition_kind,
                    "duration": f"{xfade_dur:.3f}",
                    "offset": f"{offset:.3f}",
                },
            )
        )
        graph.add(
            FilterNode(
                filter_name="acrossfade",
                inputs=[a_chain, a_labels[i + 1]],
                outputs=[next_a],
                args={"d": f"{xfade_dur:.3f}"},
            )
        )
        v_chain = next_v
        a_chain = next_a

    # ----- effects on final video stream -----
    if config.effects.unsharp:
        next_v = "[v_sharp]"
        # unsharp args contain colons/equals that DSL escape handles.
        graph.add(
            FilterNode(
                filter_name="unsharp",
                inputs=[v_chain],
                outputs=[next_v],
                args={
                    "luma_msize_x": 5,
                    "luma_msize_y": 5,
                    "luma_amount": f"{config.effects.unsharp_amount:.2f}",
                },
            )
        )
        v_chain = next_v

    if config.effects.lut:
        # Resolve LUT id to a file path under /app/assets/luts or /data/luts.
        from reelforge_core.compose.pipeline import resolve_lut  # local import to avoid cycle

        lut_path = resolve_lut(config.effects.lut)
        if lut_path is not None:
            next_v = "[v_lut]"
            graph.add(
                FilterNode(
                    filter_name="lut3d",
                    inputs=[v_chain],
                    outputs=[next_v],
                    args={"file": str(lut_path)},
                )
            )
            v_chain = next_v

    # ----- captions -----
    if captions_path is not None and config.captions.mode != "off":
        next_v = "[vfinal]"
        graph.add(
            FilterNode(
                filter_name="subtitles",
                inputs=[v_chain],
                outputs=[next_v],
                args={
                    "filename": str(captions_path),
                    "fontsdir": "/usr/share/fonts/truetype/inter",
                },
            )
        )
        v_chain = next_v
    else:
        # Rename to vfinal for the -map target
        graph.add(
            FilterNode(
                filter_name="null",
                inputs=[v_chain],
                outputs=["[vfinal]"],
            )
        )
        v_chain = "[vfinal]"

    # ----- audio: loudnorm voice, then mix with optional music bed -----
    graph.add(
        FilterNode(
            filter_name="loudnorm",
            inputs=[a_chain],
            outputs=["[voice]"],
            args={
                "I": f"{config.voice_volume_db:.1f}",
                "LRA": 7,
                "TP": -1.5,
            },
        )
    )

    if music_path is not None:
        assert music_input_index is not None
        # Split voice so it can drive both the sidechain key AND the amix primary.
        graph.add(
            FilterNode(
                filter_name="asplit",
                inputs=["[voice]"],
                outputs=["[voice_mix]", "[voice_key]"],
            )
        )
        graph.add(
            FilterNode(
                filter_name="aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
                inputs=[f"[{music_input_index}:a]"],
                outputs=["[music_raw]"],
            )
        )
        graph.add(
            FilterNode(
                filter_name="loudnorm",
                inputs=["[music_raw]"],
                outputs=["[music]"],
                args={
                    "I": f"{config.music_volume_db:.1f}",
                    "LRA": 7,
                    "TP": -1.5,
                },
            )
        )
        threshold_linear = 10 ** (config.ducking_threshold_db / 20.0)
        graph.add(
            FilterNode(
                filter_name="sidechaincompress",
                inputs=["[music]", "[voice_key]"],
                outputs=["[music_ducked]"],
                args={
                    "threshold": f"{threshold_linear:.4f}",
                    "ratio": f"{config.ducking_ratio:.1f}",
                    "attack": f"{config.ducking_attack_ms:.1f}",
                    "release": f"{config.ducking_release_ms:.1f}",
                },
            )
        )
        graph.add(
            FilterNode(
                filter_name="amix",
                inputs=["[voice_mix]", "[music_ducked]"],
                outputs=["[afinal]"],
                args={
                    "inputs": 2,
                    "duration": "first",
                    "dropout_transition": 2,
                },
            )
        )
    else:
        graph.add(
            FilterNode(
                filter_name="anull",
                inputs=["[voice]"],
                outputs=["[afinal]"],
            )
        )

    filter_complex = graph.serialize()

    total_duration = sum(durations) - max(0, len(durations) - 1) * xfade_dur

    args += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vfinal]",
        "-map",
        "[afinal]",
        "-c:v",
        "libx264",
        "-preset",
        config.video_preset,
        "-crf",
        str(config.video_crf),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-c:a",
        "aac",
        "-b:a",
        f"{config.audio_bitrate_kbps}k",
        "-movflags",
        "+faststart",
        "-metadata",
        "creation_time=1970-01-01T00:00:00Z",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-shortest",
        str(output_path),
    ]

    return RenderPlan(
        args=args,
        mezzanine_duration_sec=total_duration,
        music_input_index=music_input_index,
        filter_complex=filter_complex,
    )
