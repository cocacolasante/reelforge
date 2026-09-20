"""BullMQ consumer entrypoint.

A new process alongside the existing arq worker, the FastAPI app and the CLI —
all of which keep working unchanged. It speaks BullMQ because the producer
(growth-agent) is a Node service whose whole job system is BullMQ; the official
`bullmq` PyPI package is a port of the same Lua scripts, so the two interoperate
without a bridge.

    python -m apps.queue_consumer.main
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from bullmq import Queue, Worker

from apps.queue_consumer import labels
from apps.queue_consumer.contract import ClipJob, LabelIngest, Manifest
from apps.queue_consumer.handler import run_job

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [queue_consumer] %(message)s",
)
log = logging.getLogger(__name__)

# Hyphens, not colons: BullMQ's Node client (the producer) rejects a queue name
# containing `:` because it is the Redis key separator. The Python port accepts
# one, so a colon here would look fine on this side and break growth-agent.
JOBS_QUEUE = os.environ.get("REELFORGE_JOBS_QUEUE", "reelforge-jobs")
LABELS_QUEUE = os.environ.get("REELFORGE_LABELS_QUEUE", "reelforge-labels")


def _redis_opts() -> dict:
    return {"connection": os.environ.get("REDIS_URL", "redis://redis:6379")}


async def _handle_clip_job(job, _token: str | None = None) -> dict:
    """
    Parse, run, and publish the manifest to the queue the job named.

    The manifest is published for failures too — a job that dies quietly leaves
    growth-agent's source_videos row stuck in `ingesting` forever.
    """
    payload = ClipJob.model_validate(job.data)
    log.info("picked up job %s (tenant %s)", payload.job_id, payload.tenant_id)

    try:
        manifest = await run_job(payload)
    except Exception as exc:  # noqa: BLE001 - last resort; still report it
        log.exception("unhandled error in job %s", payload.job_id)
        manifest = Manifest(
            job_id=payload.job_id,
            tenant_id=payload.tenant_id,
            source_video_id=payload.source_video_id,
            status="failed",
            error=f"unhandled error: {exc}"[:1000],
        )

    callback = Queue(payload.callback_queue, _redis_opts())
    try:
        await callback.add("manifest", manifest.model_dump(by_alias=True, mode="json"))
        log.info(
            "published manifest for job %s: status=%s clips=%d",
            payload.job_id,
            manifest.status,
            len(manifest.clips),
        )
    finally:
        await callback.close()

    return {"status": manifest.status, "clips": len(manifest.clips)}


async def _handle_label(job, _token: str | None = None) -> dict:
    payload = LabelIngest.model_validate(job.data)
    labels.store(payload)
    return {"stored": payload.reelforge_clip_id}


async def main() -> None:
    opts = _redis_opts()
    # Clip jobs are ffmpeg-bound; one at a time per process keeps them honest.
    jobs_worker = Worker(JOBS_QUEUE, _handle_clip_job, {**opts, "concurrency": 1})
    labels_worker = Worker(LABELS_QUEUE, _handle_label, {**opts, "concurrency": 4})

    log.info("consuming %s and %s", JOBS_QUEUE, LABELS_QUEUE)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    log.info("shutting down; letting in-flight jobs finish")
    await jobs_worker.close()
    await labels_worker.close()


if __name__ == "__main__":
    asyncio.run(main())
