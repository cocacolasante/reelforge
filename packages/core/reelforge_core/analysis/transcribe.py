"""faster-whisper transcription with VAD + word timestamps."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from reelforge_core.analysis.audio_extract import extract_audio
from reelforge_core.errors import TranscriptionError
from reelforge_core.ingest import MediaAsset
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    AnalysisConfig,
    ProgressCallback,
    ProgressEvent,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    compute_overall,
)

log = logging.getLogger(__name__)

logging.getLogger("faster_whisper").setLevel(logging.WARNING)


def _select_device(config: AnalysisConfig) -> tuple[str, str]:
    """Resolve device + compute_type, honoring explicit overrides and env fallbacks."""
    # Env overrides win when config is "auto" — the GPU compose profile sets these.
    env_device = os.environ.get("WHISPER_DEVICE", "").strip().lower() or None
    env_compute = os.environ.get("WHISPER_COMPUTE_TYPE", "").strip().lower() or None

    device = config.whisper_device
    compute = config.whisper_compute_type

    if device == "auto":
        if env_device in {"cpu", "cuda"}:
            device = env_device  # type: ignore[assignment]
        else:
            try:
                import ctranslate2  # type: ignore

                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:  # pragma: no cover - import-time CT2 issues
                device = "cpu"

    if compute == "auto":
        if env_compute in {"int8", "float16", "float32"}:
            compute = env_compute  # type: ignore[assignment]
        else:
            compute = "float16" if device == "cuda" else "int8"

    return device, compute


def _run_transcribe(
    wav_path: Path, config: AnalysisConfig
) -> tuple[list[TranscriptSegment], str, float, float]:
    """Sync wrapper around faster-whisper. Returns (segments, lang, lang_prob, duration)."""
    from faster_whisper import WhisperModel

    device, compute_type = _select_device(config)
    cache_dir = os.environ.get("WHISPER_MODEL_CACHE", "/models/whisper")

    log.info(
        "loading Whisper model %s (device=%s compute=%s)",
        config.whisper_model,
        device,
        compute_type,
    )
    # If not yet cached, this downloads. ~150 MB for base.en, ~3 GB for large-v3.
    log.info(
        "Downloading Whisper model '%s' to %s if not yet cached "
        "(may take several minutes on first run)",
        config.whisper_model,
        cache_dir,
    )

    model = WhisperModel(
        config.whisper_model,
        download_root=cache_dir,
        device=device,
        compute_type=compute_type,
    )

    segments_iter, info = model.transcribe(
        str(wav_path),
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
        condition_on_previous_text=False,
    )

    segments: list[TranscriptSegment] = []
    for seg in segments_iter:
        words_out: list[TranscriptWord] = []
        if seg.words:
            for w in seg.words:
                prob = w.probability if w.probability is not None else 0.0
                words_out.append(
                    TranscriptWord(
                        start=float(w.start),
                        end=float(w.end),
                        word=w.word,
                        probability=float(prob),
                    )
                )
        segments.append(
            TranscriptSegment(
                start=float(seg.start),
                end=float(seg.end),
                text=seg.text,
                words=words_out,
            )
        )
    return segments, info.language, float(info.language_probability), float(info.duration)


async def transcribe(
    asset: MediaAsset,
    working_dir: Path,
    config: AnalysisConfig,
    progress: ProgressCallback,
) -> Transcript | None:
    if not asset.has_audio:
        write_json_atomic(working_dir / "transcript.json", {"transcript": None})
        await progress(ProgressEvent("transcribe", 1.0, compute_overall("transcribe", 1.0)))
        return None

    await progress(ProgressEvent("transcribe", 0.02, compute_overall("transcribe", 0.02)))
    wav_path = working_dir / "audio.wav"
    await asyncio.to_thread(extract_audio, asset.path, wav_path)
    await progress(ProgressEvent("transcribe", 0.10, compute_overall("transcribe", 0.10)))

    # Drive real progress from segment boundaries. We can't interleave Python
    # progress callbacks into the CT2 inference loop easily, so we do two jumps:
    # 0.10 before model load, then report 1.0 after the iterator is consumed.
    # This is honest because wall-time is dominated by the iterator.
    try:
        segments, language, language_prob, duration = await asyncio.to_thread(
            _run_transcribe, wav_path, config
        )
    except Exception as exc:
        raise TranscriptionError(f"Whisper transcription failed: {exc}") from exc

    transcript = Transcript(
        language=language,
        language_probability=language_prob,
        duration=duration,
        segments=segments,
    )
    write_json_atomic(working_dir / "transcript.json", transcript.model_dump())
    await progress(ProgressEvent("transcribe", 1.0, compute_overall("transcribe", 1.0)))
    return transcript


# Small helper so tests can inject a clock for throttle assertions.
_monotonic = time.monotonic
