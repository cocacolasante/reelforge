"""Assemble the final-render FFmpeg command: clips + xfade + acrossfade + effects
+ captions + music sidechain mix.

Returns an argv list so the caller can choose how to invoke it. The returned
graph is verified by the DSL layer — duplicate labels or unbalanced inputs
raise GraphError at construction time, not at FFmpeg runtime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.graph import FilterGraph, FilterNode
from reelforge_core.models import AnalysisReport, ComposeConfig

log = logging.getLogger(__name__)

# Map our transition.kind to xfade's `transition=` value. "cut" is implemented as
# a 40ms fade so the graph shape stays uniform.
_TRANSITION_MAP = {
    # "auto" should be resolved before we reach the graph builder (see
    # compose/auto.py::resolve_smart_config). Keep a defensive fall-through
    # to plain fade just in case. Every non-auto/cut TransitionStyle.kind
    # (models.py) must have an entry here.
    "auto": "fade",
    "fade": "fade",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "dissolve": "dissolve",
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    "smoothleft": "smoothleft",
    "smoothright": "smoothright",
    "circleopen": "circleopen",
    "circleclose": "circleclose",
    "cut": "fade",
}


@dataclass
class RenderPlan:
    args: list[str]
    mezzanine_duration_sec: float
    music_input_index: int | None
    filter_complex: str


def _drift_crop(width: int, height: int, dur: float, position: int) -> str:
    """Eased diagonal crop drift for Ken Burns / animated punch-in.

    Direction rotates with `position` (TL→BR, BR→TL, TR→BL, BL→TR) so
    consecutive moving shots don't all drift the same way — mirrors the
    photo pan rotation in compose/photos.py. Ease-out: the window
    decelerates into its final position.
    """
    # eased progress d = 1 - (1 - min(t/dur, 1))^2 ; inv = 1 - d
    inv = f"pow(1-min(t/{dur:.3f}\\,1)\\,2)"
    d = f"(1-{inv})"
    corners = [
        (d, d),  # TL → BR
        (inv, inv),  # BR → TL
        (inv, d),  # TR → BL
        (d, inv),  # BL → TR
    ]
    x_frac, y_frac = corners[position % 4]
    return (
        f"crop={width}:{height}:"
        f"x='(iw-ow)*{x_frac}':y='(ih-oh)*{y_frac}'"
    )


def _transition_duration(config: ComposeConfig) -> float:
    return 0.04 if config.transition.kind == "cut" else config.transition.duration_sec


def _xfade_offsets(
    clip_durations: list[float], xfade_dur: float | list[float]
) -> list[float]:
    """Offsets[i] is the xfade offset for the transition between clip i and i+1.
    Length = len(clips) - 1. `xfade_dur` is one duration for every cut, or a
    per-cut list (len n-1) when transitions differ per cut."""
    if len(clip_durations) <= 1:
        return []
    n_cuts = len(clip_durations) - 1
    xfades = (
        list(xfade_dur) if isinstance(xfade_dur, list) else [xfade_dur] * n_cuts
    )
    offsets: list[float] = []
    cumulative = 0.0
    overlapped = 0.0
    for i, dur in enumerate(clip_durations[:-1]):
        # Offset for transition i is cumulative duration up through clip i,
        # minus the overlap already reclaimed by earlier transitions, minus
        # this transition's own duration.
        cumulative += dur
        overlapped += xfades[i]
        offsets.append(cumulative - overlapped)
    return offsets


def resolve_transitions(
    config: ComposeConfig, n_clips: int, per_cut: list[tuple[str, float] | None] | None = None
) -> list[tuple[str, float]]:
    """(xfade transition name, duration) for each of the n-1 cuts.

    `per_cut[i]` overrides the reel-wide transition for cut i (None keeps the
    default). "cut" renders as a 40ms fade so the graph shape stays uniform.
    """
    default_kind = config.transition.kind
    default_dur = _transition_duration(config)
    out: list[tuple[str, float]] = []
    for i in range(max(0, n_clips - 1)):
        override = per_cut[i] if per_cut and i < len(per_cut) else None
        if override is not None:
            kind, dur = override
            dur = 0.04 if kind == "cut" else max(0.04, dur)
        else:
            kind, dur = default_kind, default_dur
        out.append((_TRANSITION_MAP.get(kind, "fade"), dur))
    return out


def expand_transitions_for_photos(
    clips: list[ClipInfo],
    video_transitions: list[tuple[str, float]],
    default: tuple[str, float],
) -> list[tuple[str, float]]:
    """Re-map per-cut transitions after photos were interleaved into the shot
    list. Cuts between still-adjacent video shots keep their transition; any
    cut touching a photo gets the reel-wide default. Pure."""
    vidx: list[int | None] = []
    k = 0
    for c in clips:
        vidx.append(None if c.is_photo else k)
        if not c.is_photo:
            k += 1
    out: list[tuple[str, float]] = []
    for j in range(len(clips) - 1):
        a, b = vidx[j], vidx[j + 1]
        if a is not None and b is not None and a < len(video_transitions):
            out.append(video_transitions[a])
        else:
            out.append(default)
    return out


def clamp_transitions(
    durations: list[float], transitions: list[tuple[str, float]]
) -> list[tuple[str, float]]:
    """Clamp each crossfade so it never outlasts its shorter adjoining shot
    (an xfade longer than a clip renders broken with no ffmpeg error). Rule:
    duration ≤ half the shorter neighbour, floored at 0.04s. Pure — callers
    MUST apply this to the list that captions and beat-sync consume too, so
    the triplicated offset math stays in agreement."""
    out: list[tuple[str, float]] = []
    for i, (kind, dur) in enumerate(transitions):
        if i + 1 < len(durations):
            limit = max(0.04, 0.5 * min(durations[i], durations[i + 1]))
            if dur > limit:
                log.info(
                    "transition %d (%s %.2fs) clamped to %.2fs — adjoining "
                    "shot too short",
                    i,
                    kind,
                    dur,
                    limit,
                )
                dur = round(limit, 3)
        out.append((kind, dur))
    return out


def build_final_command(
    *,
    clips: list[ClipInfo],
    analysis: AnalysisReport,
    music_path: Path | None,
    captions_path: Path | None,
    config: ComposeConfig,
    output_path: Path,
    transitions: list[tuple[str, float]] | None = None,
    voiceovers: list[tuple[Path, float, float]] | None = None,
) -> RenderPlan:
    """`voiceovers`: (path, start_sec on the mezzanine, linear gain) per take.
    Muted takes should be omitted by the caller."""
    if not clips:
        raise ValueError("build_final_command requires at least one clip")

    width, height = config.resolution
    fps = config.target_fps
    # Per-cut (xfade name, duration). Defaults to the reel-wide transition.
    cuts = transitions if transitions is not None else resolve_transitions(config, len(clips))
    if len(cuts) != max(0, len(clips) - 1):
        raise ValueError(
            f"transitions has {len(cuts)} entries for {len(clips)} clips (need {len(clips) - 1})"
        )
    xfade_durs = [d for _, d in cuts]

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
    vo_inputs: list[tuple[int, float, float]] = []  # (input index, start, gain)
    for vo_path, vo_start, vo_gain in (voiceovers or []):
        if vo_gain <= 0:
            continue
        vo_inputs.append((len(clips) + (1 if music_path is not None else 0) + len(vo_inputs), vo_start, vo_gain))
        args += ["-i", str(vo_path)]

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
        wants_ken_burns = not clip.is_photo and (
            clip.force_ken_burns
            or (
                config.effects.ken_burns_on_low_energy
                and clip.scene_index in low_energy_by_idx
            )
        )
        if not clip.is_photo and clip.punch_in is not None:
            # Punch-in: digital zoom in the graph (clip stays cache-shareable).
            zoom = min(1.6, max(1.01, clip.punch_in))
            sw = int(width * zoom / 2) * 2
            sh = int(height * zoom / 2) * 2
            if clip.punch_in_animated:
                crop = _drift_crop(width, height, max(0.1, clip.duration), i)
            else:
                crop = f"crop={width}:{height}:(iw-ow)/2:(ih-oh)/2"
            graph.add(
                FilterNode(
                    filter_name=f"scale={sw}:{sh},{crop}",
                    inputs=[v_prep_out],
                    outputs=[v_out],
                )
            )
        elif wants_ken_burns:
            # Ken Burns-style motion as constant-zoom + animated crop: scale
            # the clip up ONCE, then drift a target-sized window diagonally
            # across the margin. Orders of magnitude cheaper than zoompan
            # (which re-resamples every frame and made renders ~10x slower);
            # visually it reads the same "slow deliberate camera move".
            # Direction rotates with clip position; the drift is eased
            # (decelerating) so the move settles instead of stopping dead.
            zoom = max(1.01, config.effects.ken_burns_zoom)
            sw = int(width * zoom / 2) * 2
            sh = int(height * zoom / 2) * 2
            graph.add(
                FilterNode(
                    filter_name=(
                        f"scale={sw}:{sh},"
                        + _drift_crop(width, height, max(0.1, clip.duration), i)
                    ),
                    inputs=[v_prep_out],
                    outputs=[v_out],
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
            # Per-shot gain from the editor (mute = 0). Applied before the
            # crossfade so a muted shot fades in/out like any other.
            gain_part = (
                f"volume={max(0.0, clip.volume):.4f},"
                if abs(clip.volume - 1.0) > 1e-6
                else ""
            )
            graph.add(
                FilterNode(
                    filter_name=(
                        "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                        f"{gain_part}asetpts=PTS-STARTPTS"
                    ),
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
    offsets = _xfade_offsets(durations, xfade_durs)
    v_chain = v_labels[0]
    a_chain = a_labels[0]
    for i, offset in enumerate(offsets):
        next_v = f"[xv{i}]"
        next_a = f"[xa{i}]"
        kind_i, dur_i = cuts[i]
        graph.add(
            FilterNode(
                filter_name="xfade",
                inputs=[v_chain, v_labels[i + 1]],
                outputs=[next_v],
                args={
                    "transition": kind_i,
                    "duration": f"{dur_i:.3f}",
                    "offset": f"{offset:.3f}",
                },
            )
        )
        graph.add(
            FilterNode(
                filter_name="acrossfade",
                inputs=[a_chain, a_labels[i + 1]],
                outputs=[next_a],
                args={"d": f"{dur_i:.3f}"},
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

    # Program length after crossfade overlap — needed below to pad the
    # voiceover bus to full length, and reported in the plan.
    total_duration = sum(durations) - sum(xfade_durs)

    # ----- voiceover takes: mix beneath-ducked footage audio -----
    # Each take is delayed to its mezzanine start and gain-staged; the takes
    # sum to [vo_mix]; footage audio ducks under it (sidechain), and the two
    # are summed into the voice bus BEFORE voice loudnorm — so music then
    # ducks beneath footage+voiceover together.
    if vo_inputs:
        vo_labels: list[str] = []
        for k, (idx, start, gain) in enumerate(vo_inputs):
            label = f"[vo{k}]"
            delay_ms = max(0, int(round(start * 1000)))
            graph.add(
                FilterNode(
                    filter_name=(
                        "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                        f"adelay=delays={delay_ms}|{delay_ms}:all=1,"
                        f"volume={gain:.4f}"
                    ),
                    inputs=[f"[{idx}:a]"],
                    outputs=[label],
                )
            )
            vo_labels.append(label)
        if len(vo_labels) == 1:
            graph.add(FilterNode(filter_name="anull", inputs=vo_labels, outputs=["[vo_mix]"]))
        else:
            graph.add(
                FilterNode(
                    filter_name="amix",
                    inputs=vo_labels,
                    outputs=["[vo_mix]"],
                    args={"inputs": len(vo_labels), "duration": "longest", "normalize": 0},
                )
            )
        # Level the voiceover bus so takes recorded at different gains match.
        graph.add(
            FilterNode(
                filter_name="loudnorm",
                inputs=["[vo_mix]"],
                outputs=["[vo_norm]"],
                args={"I": f"{config.voiceover_volume_db:.1f}", "LRA": 7, "TP": -1.5},
            )
        )
        # Pad to the full program length. Two-input filters (the ducking
        # sidechain, amix) stop when their SHORTEST input ends — without this
        # a 4s take would cut the whole mix off at 4s. Padded after loudnorm
        # so levelling sees only the real takes, not the silence.
        graph.add(
            FilterNode(
                filter_name=(
                    "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"apad=whole_dur={max(0.1, total_duration):.3f}"
                ),
                inputs=["[vo_norm]"],
                outputs=["[vo_ready]"],
            )
        )
        if config.voiceover_ducking:
            graph.add(
                FilterNode(
                    filter_name="asplit",
                    inputs=["[vo_ready]"],
                    outputs=["[vo_main]", "[vo_key]"],
                )
            )
            threshold_linear = 10 ** (config.ducking_threshold_db / 20.0)
            graph.add(
                FilterNode(
                    filter_name="sidechaincompress",
                    inputs=[a_chain, "[vo_key]"],
                    outputs=["[aclips_ducked]"],
                    args={
                        "threshold": f"{threshold_linear:.4f}",
                        "ratio": f"{config.ducking_ratio:.1f}",
                        "attack": f"{config.ducking_attack_ms:.1f}",
                        "release": f"{config.ducking_release_ms:.1f}",
                    },
                )
            )
            footage_label = "[aclips_ducked]"
            vo_main = "[vo_main]"
        else:
            footage_label = a_chain
            vo_main = "[vo_ready]"
        graph.add(
            FilterNode(
                filter_name="amix",
                inputs=[footage_label, vo_main],
                outputs=["[voice_pre]"],
                # duration=first: the footage defines the length; takes that
                # run past the end are truncated, not padded.
                args={"inputs": 2, "duration": "first", "dropout_transition": 2, "normalize": 0},
            )
        )
        a_chain = "[voice_pre]"

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
                outputs=["[amixed]"],
                args={
                    "inputs": 2,
                    "duration": "first",
                    "dropout_transition": 2,
                },
            )
        )
        a_pre_final = "[amixed]"
    else:
        a_pre_final = "[voice]"

    # Final-bus loudness normalization. The per-stem loudnorms above set the
    # *balance* (voice vs music); this pass pins the finished mix to the
    # social-platform target so amix's input scaling and stem summing can't
    # push the output quiet or hot.
    if config.normalize_loudness:
        graph.add(
            FilterNode(
                filter_name="loudnorm",
                inputs=[a_pre_final],
                outputs=["[anorm]"],
                args={
                    "I": f"{config.loudness_target_lufs:.1f}",
                    "LRA": 11,
                    "TP": f"{config.loudness_true_peak_db:.1f}",
                },
            )
        )
        # loudnorm upsamples to 192 kHz internally and emits at that rate —
        # bring the bus back to the mezzanine's 48 kHz before AAC encode.
        # The trailing alimiter is load-bearing: loudnorm's dynamic mode can
        # overshoot its TP ceiling by several dB while its gain smoothing
        # settles during the first seconds; the brick-wall keeps sample peaks
        # at the configured true-peak ceiling. latency=1 compensates the
        # limiter's lookahead delay so audio stays in sync with video.
        limit_linear = 10 ** (config.loudness_true_peak_db / 20.0)
        graph.add(
            FilterNode(
                filter_name=(
                    "aresample=48000,"
                    "aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"alimiter=limit={limit_linear:.4f}:level=false:latency=true"
                ),
                inputs=["[anorm]"],
                outputs=["[afinal]"],
            )
        )
    else:
        graph.add(
            FilterNode(
                filter_name="anull",
                inputs=[a_pre_final],
                outputs=["[afinal]"],
            )
        )

    filter_complex = graph.serialize()

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
        config.effective_mezz_preset,
        "-crf",
        str(config.effective_mezz_crf),
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
