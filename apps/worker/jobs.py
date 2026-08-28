"""arq job functions.

- `analyze_asset` (Phase 1): scenes + transcript + loudness + semantics → analysis.json
- `select_reels_job` (Phase 2): candidates + ranking + dedup → reels.json

Both jobs stream progress to `job:{job_id}:progress` via a shared throttled writer,
and mirror terminal state (success/failure) into the SQLite `jobs` table.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from pathlib import Path

from apps.worker.progress import (
    make_throttled_progress_writer,
    write_terminal,
)
from reelforge_core import db
from reelforge_core.analysis import analyze
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.compose import compose
from reelforge_core.export import export
from reelforge_core.ingest import MediaAsset, probe
from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    ComposeConfig,
    ProgressEvent,
    ReelSelection,
    SelectionConfig,
)
from reelforge_core.reels import select_reels
from reelforge_core.usage import record_anthropic_usage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# analyze_asset (Phase 1)
# ---------------------------------------------------------------------------


async def _load_asset(asset_id: str) -> MediaAsset:
    wd = working_dir_for(asset_id)
    probe_path = wd / "probe.json"
    if not probe_path.exists():
        raise FileNotFoundError(
            f"no probe.json at {probe_path}; run MediaAsset.from_path first"
        )
    raw = json.loads(probe_path.read_text())
    source = Path(raw["path"])
    if not source.exists():
        raise FileNotFoundError(f"probe.json references missing source: {source}")
    asset = probe(source)
    if asset.id != asset_id:
        log.warning(
            "content drift: asset_id on queue (%s) != recomputed (%s)",
            asset_id,
            asset.id,
        )
    return asset


async def analyze_asset(
    ctx: dict, asset_id: str, config_dict: dict | None = None
) -> dict:
    job_id = ctx["job_id"]
    redis = ctx["redis"]
    config = AnalysisConfig(**(config_dict or {}))
    extra = {"job_id": job_id, "asset_id": asset_id}
    log.info("analyze_asset start", extra=extra)

    await db.record_job_start(job_id, kind="analyze_asset", asset_id=asset_id)
    asset = await _load_asset(asset_id)
    on_progress = make_throttled_progress_writer(redis, job_id)

    try:
        report = await analyze(asset, config, progress=on_progress)
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("analyze_asset failed: %s", exc, extra=extra, exc_info=True)
        await db.record_job_failure(job_id, str(exc), tb)
        await write_terminal(redis, job_id, "error", str(exc))
        raise

    analysis_path = str(working_dir_for(asset.id) / "analysis.json")
    # Record Anthropic usage (semantics model).
    try:
        usage = report.anthropic_usage or {}
        await record_anthropic_usage(
            job_id=job_id,
            model=report.config.semantics_model,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_hits=int(usage.get("cache_hits", 0) or 0),
            asset_id=asset.id,
        )
    except Exception:  # pragma: no cover
        log.exception("failed to record anthropic usage", extra=extra)

    result = {
        "analysis_path": analysis_path,
        "asset_id": asset.id,
        "duration": report.duration,
        "scenes": len(report.scenes),
    }
    await db.record_job_success(job_id, result)
    await write_terminal(redis, job_id, "done", "done")
    log.info("analyze_asset done", extra=extra)
    return result


# ---------------------------------------------------------------------------
# select_reels_job (Phase 2)
# ---------------------------------------------------------------------------


async def select_reels_job(
    ctx: dict, asset_id: str, config_dict: dict | None = None
) -> dict:
    job_id = ctx["job_id"]
    redis = ctx["redis"]
    config = SelectionConfig(**(config_dict or {}))
    extra = {"job_id": job_id, "asset_id": asset_id}
    log.info("select_reels_job start", extra=extra)

    await db.record_job_start(job_id, kind="select_reels", asset_id=asset_id)

    wd = working_dir_for(asset_id)
    analysis_path = wd / "analysis.json"
    if not analysis_path.exists():
        tb = f"analysis.json not found for asset {asset_id}. Run analyze first."
        await db.record_job_failure(job_id, tb, tb)
        await write_terminal(redis, job_id, "error", tb)
        raise FileNotFoundError(tb)

    try:
        analysis = AnalysisReport.model_validate_json(analysis_path.read_text())
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("failed to parse analysis.json: %s", exc, extra=extra, exc_info=True)
        await db.record_job_failure(job_id, str(exc), tb)
        await write_terminal(redis, job_id, "error", str(exc))
        raise

    on_progress = make_throttled_progress_writer(redis, job_id)

    try:
        selection = await select_reels(analysis, config, progress=on_progress)
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("select_reels_job failed: %s", exc, extra=extra, exc_info=True)
        await db.record_job_failure(job_id, str(exc), tb)
        await write_terminal(redis, job_id, "error", str(exc))
        raise

    reels_path = wd / "reels.json"
    try:
        usage = selection.anthropic_usage or {}
        await record_anthropic_usage(
            job_id=job_id,
            model=selection.config.ranking_model,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_hits=int(usage.get("cache_hits", 0) or 0),
            asset_id=asset_id,
        )
    except Exception:  # pragma: no cover
        log.exception("failed to record anthropic usage", extra=extra)

    result = {
        "reels_path": str(reels_path),
        "asset_id": asset_id,
        "reel_count": len(selection.reels),
    }
    await db.record_job_success(job_id, result)
    await write_terminal(redis, job_id, "done", "done")
    log.info("select_reels_job done", extra=extra)
    return result


# ---------------------------------------------------------------------------
# compose_reel_job (Phase 3)
# ---------------------------------------------------------------------------


async def compose_reel_job(
    ctx: dict,
    asset_id: str,
    reel_id: str,
    config_dict: dict | None = None,
) -> dict:
    job_id = ctx["job_id"]
    redis = ctx["redis"]
    config = ComposeConfig(**(config_dict or {}))
    extra = {"job_id": job_id, "asset_id": asset_id, "reel_id": reel_id}
    log.info("compose_reel_job start", extra=extra)

    await db.record_job_start(job_id, kind="compose_reel", asset_id=asset_id)

    wd = working_dir_for(asset_id)
    analysis_path = wd / "analysis.json"
    reels_path = wd / "reels.json"
    if not analysis_path.exists() or not reels_path.exists():
        msg = (
            f"missing analysis/reels for asset {asset_id}. Run analyze and "
            f"select before compose."
        )
        await db.record_job_failure(job_id, msg, msg)
        await write_terminal(redis, job_id, "error", msg)
        raise FileNotFoundError(msg)

    try:
        analysis = AnalysisReport.model_validate_json(analysis_path.read_text())
        selection = ReelSelection.model_validate_json(reels_path.read_text())
    except Exception as exc:
        tb = traceback.format_exc()
        await db.record_job_failure(job_id, str(exc), tb)
        await write_terminal(redis, job_id, "error", str(exc))
        raise

    reel = next((r for r in selection.reels if r.candidate_id == reel_id), None)
    if reel is None:
        msg = f"reel {reel_id} not found in selection for asset {asset_id}"
        await db.record_job_failure(job_id, msg, msg)
        await write_terminal(redis, job_id, "error", msg)
        raise LookupError(msg)

    asset = await _load_asset(asset_id)
    on_progress = make_throttled_progress_writer(redis, job_id)

    try:
        manifest = await compose(asset, reel, analysis, config, progress=on_progress)
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("compose_reel_job failed: %s", exc, extra=extra, exc_info=True)
        await db.record_job_failure(job_id, str(exc), tb)
        await write_terminal(redis, job_id, "error", str(exc))
        raise

    result = {
        "mezzanine_path": manifest.mezzanine_path,
        "compose_json_path": str(
            Path(manifest.mezzanine_path).parent / "compose.json"
        ),
        "asset_id": asset_id,
        "reel_id": reel_id,
        "duration_sec": manifest.duration_sec,
    }
    await db.record_job_success(job_id, result)
    await write_terminal(redis, job_id, "done", "done")
    log.info("compose_reel_job done", extra=extra)
    return result


# ---------------------------------------------------------------------------
# export_reel_job (Phase 4)
# ---------------------------------------------------------------------------


async def compile_montage_job(
    ctx: dict,
    project_id: str,
    montage_id: str,
    child_mezzanine_paths: list[str],
    transition_duration: float = 0.6,
    target_resolution_w: int = 1080,
    target_resolution_h: int = 1920,
    target_fps: int = 30,
    transition_kind: str = "fade",
) -> dict:
    """Long-form montage: concatenate already-rendered mezzanines with xfade.

    Inputs are validated to exist on disk; the worker just runs FFmpeg.
    """
    job_id = ctx["job_id"]
    redis = ctx["redis"]
    extra = {"job_id": job_id, "project_id": project_id, "montage_id": montage_id}
    log.info("compile_montage_job start", extra=extra)

    await db.record_job_start(job_id, kind="compile_montage", asset_id=None)
    on_progress = make_throttled_progress_writer(redis, job_id)

    inputs = [Path(p) for p in child_mezzanine_paths]
    missing = [str(p) for p in inputs if not p.exists()]
    if missing:
        msg = f"montage inputs missing on disk: {missing}"
        await db.record_job_failure(job_id, msg, msg)
        await write_terminal(redis, job_id, "error", msg)
        raise FileNotFoundError(msg)

    from reelforge_core.compose.montage import compile_montage

    data_dir = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
    out_dir = data_dir / "working" / "_montage" / project_id / montage_id

    try:
        manifest = await compile_montage(
            inputs=inputs,
            output_dir=out_dir,
            montage_id=montage_id,
            transition_duration=transition_duration,
            transition_kind=transition_kind,
            target_resolution=(target_resolution_w, target_resolution_h),
            target_fps=target_fps,
            progress=on_progress,
        )
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("compile_montage_job failed: %s", exc, extra=extra, exc_info=True)
        await db.record_job_failure(job_id, str(exc), tb)
        await write_terminal(redis, job_id, "error", str(exc))
        raise

    result = {
        "mezzanine_path": manifest.mezzanine_path,
        "compose_json_path": str(out_dir / "compose.json"),
        "project_id": project_id,
        "montage_id": montage_id,
        "duration_sec": manifest.duration_sec,
    }
    await db.record_job_success(job_id, result)
    await write_terminal(redis, job_id, "done", "done")
    log.info("compile_montage_job done", extra=extra)
    return result


async def export_reel_job(
    ctx: dict,
    asset_id: str,
    reel_id: str,
    preset_id: str,
    force: bool = False,
) -> dict:
    job_id = ctx["job_id"]
    redis = ctx["redis"]
    extra = {
        "job_id": job_id,
        "asset_id": asset_id,
        "reel_id": reel_id,
        "preset_id": preset_id,
    }
    log.info("export_reel_job start", extra=extra)

    await db.record_job_start(job_id, kind="export_reel", asset_id=asset_id)
    on_progress = make_throttled_progress_writer(redis, job_id)

    try:
        manifest = await export(
            asset_id, reel_id, preset_id, force=force, progress=on_progress
        )
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("export_reel_job failed: %s", exc, extra=extra, exc_info=True)
        await db.record_job_failure(job_id, str(exc), tb)
        await write_terminal(redis, job_id, "error", str(exc))
        raise

    result = {
        "output_path": manifest.output_path,
        "sidecar_path": str(
            Path(manifest.output_path).with_name(f"{preset_id}.export.json")
        ),
        "file_size_bytes": manifest.file_size_bytes,
        "preset_id": preset_id,
        "asset_id": asset_id,
        "reel_id": reel_id,
    }
    await db.record_job_success(job_id, result)
    await write_terminal(redis, job_id, "done", "done")
    log.info("export_reel_job done", extra=extra)
    return result


# ---------------------------------------------------------------------------
# publish_reel_job (Phase 9+) — YouTube / Instagram / TikTok
# ---------------------------------------------------------------------------


async def publish_reel_job(ctx: dict, publication_id: str) -> dict:
    """Upload a finished export to the publication's connected account.

    Dispatches on the publication's platform:
    - youtube:   resumable upload -> public watch URL
    - instagram: Meta fetches our tokened public media URL -> permalink
    - tiktok:    direct file upload to the user's TikTok inbox (they finish
                 captioning/posting in the TikTok app)
    """
    import asyncio

    from reelforge_core.publish import store as pub_store
    from reelforge_core.publish import youtube

    job_id = ctx["job_id"]
    redis = ctx["redis"]
    extra = {"job_id": job_id, "publication_id": publication_id}
    log.info("publish_reel_job start", extra=extra)

    await db.record_job_start(job_id, kind="publish_reel", asset_id=None)
    on_progress = make_throttled_progress_writer(redis, job_id)
    loop = asyncio.get_running_loop()

    def _cb(frac: float) -> None:
        fut = asyncio.run_coroutine_threadsafe(
            on_progress(ProgressEvent("upload", frac, frac)), loop
        )
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    try:
        pub = await asyncio.to_thread(pub_store.get_publication, publication_id)
        if pub is None:
            raise RuntimeError(f"publication {publication_id} not found")
        platform = pub["platform"]
        if pub.get("account_id"):
            account = await asyncio.to_thread(
                pub_store.get_account_by_id, pub["account_id"]
            )
        else:  # pre-multi-channel rows
            account = await asyncio.to_thread(pub_store.get_account, platform)
        if account is None:
            raise RuntimeError(
                f"the {platform} account for this publication is no longer connected"
            )

        from reelforge_core.publish.store import _conn  # reuse the sync conn

        def _asset_id() -> str | None:
            r = _conn().execute(
                "SELECT asset_id FROM reels WHERE id = ?", (pub["reel_id"],)
            ).fetchone()
            return r["asset_id"] if r else None

        asset_id = await asyncio.to_thread(_asset_id)
        if asset_id is None:
            raise RuntimeError(f"reel {pub['reel_id']} not found")
        data_dir = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))

        # CC-BY music legally requires a credit line — append it to whatever
        # description/caption the user wrote (idempotent; None for CC0/no music).
        from reelforge_core.publish.credits import append_credit, music_credit_for_reel

        credit = music_credit_for_reel(asset_id, pub["reel_id"], data_dir)
        if credit:
            pub["description"] = append_credit(pub.get("description") or "", credit)
            log.info("music credit appended to publication", extra=extra)
        video_path = (
            data_dir / "outputs" / asset_id / pub["reel_id"] / f"{pub['preset_id']}.mp4"
        )
        if not video_path.exists():
            raise RuntimeError(f"export missing on disk: {video_path}")

        await asyncio.to_thread(pub_store.mark_publication_running, publication_id)
        await on_progress(ProgressEvent("upload", 0.02, 0.02))

        if platform == "youtube":
            client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
            client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
            if not client_id or not client_secret:
                raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not configured")
            access_token = await asyncio.to_thread(
                youtube.refresh_access_token,
                client_id,
                client_secret,
                account["refresh_token"],
            )
            await asyncio.to_thread(
                pub_store.update_account_access_token, account["id"], access_token
            )
            video_id = await asyncio.to_thread(
                youtube.upload_video,
                access_token,
                video_path,
                title=pub["title"],
                description=pub["description"] or "",
                privacy=pub["privacy"] or "private",
                progress_cb=_cb,
            )
            url = youtube.video_url(video_id)

        elif platform == "instagram":
            from reelforge_core.publish import instagram

            public_base = os.environ.get("REELFORGE_PUBLIC_MEDIA_BASE", "").rstrip("/")
            if not public_base:
                raise RuntimeError(
                    "REELFORGE_PUBLIC_MEDIA_BASE not configured — Instagram needs "
                    "a public tunnel to fetch the video (docs/publishing.md)"
                )
            if not pub.get("public_token"):
                raise RuntimeError("publication has no public media token")
            access_token = account["access_token"]
            # Long-lived tokens self-refresh (>=24h old). Best effort: on
            # success persist; on failure keep the stored token (it may still
            # be valid for weeks).
            try:
                refreshed = await asyncio.to_thread(instagram.refresh_token, access_token)
                access_token = refreshed["access_token"]
                await asyncio.to_thread(
                    pub_store.update_account_tokens, account["id"], access_token
                )
            except Exception:
                log.info("instagram token refresh skipped (token too new or transient)")
            extra_data = json.loads(account.get("extra_json") or "{}")
            ig_user_id = extra_data.get("user_id") or account["external_id"]
            video_url = f"{public_base}/api/v1/public/media/{pub['public_token']}"
            caption = pub["title"]
            if pub.get("description"):
                caption = f"{pub['title']}\n\n{pub['description']}"
            video_id, permalink = await asyncio.to_thread(
                instagram.publish_reel,
                access_token,
                ig_user_id,
                video_url,
                caption,
                _cb,
            )
            url = permalink

        elif platform == "tiktok":
            from reelforge_core.publish import tiktok

            client_key = os.environ.get("TIKTOK_CLIENT_KEY", "")
            client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "")
            if not client_key or not client_secret:
                raise RuntimeError("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET not configured")
            tokens = await asyncio.to_thread(
                tiktok.refresh_tokens,
                client_key,
                client_secret,
                account["refresh_token"],
            )
            # TikTok rotates refresh tokens — persist whatever came back.
            await asyncio.to_thread(
                pub_store.update_account_tokens,
                account["id"],
                tokens["access_token"],
                tokens.get("refresh_token"),
            )
            video_id = await asyncio.to_thread(
                tiktok.upload_to_inbox,
                tokens["access_token"],
                video_path,
                _cb,
            )
            url = None  # user finishes the post inside the TikTok app

        else:
            raise RuntimeError(f"unsupported platform {platform!r}")

        await asyncio.to_thread(
            pub_store.mark_publication_done, publication_id, video_id, url
        )
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("publish_reel_job failed: %s", exc, extra=extra, exc_info=True)
        try:
            await asyncio.to_thread(
                pub_store.mark_publication_failed, publication_id, str(exc)
            )
        except Exception:
            log.exception("could not mark publication failed")
        await db.record_job_failure(job_id, str(exc), tb)
        await write_terminal(redis, job_id, "error", str(exc))
        raise

    result = {
        "publication_id": publication_id,
        "platform": platform,
        "video_id": video_id,
        "video_url": url,
    }
    await db.record_job_success(job_id, result)
    await write_terminal(redis, job_id, "done", "done")
    log.info("publish_reel_job done", extra=extra)
    return result
