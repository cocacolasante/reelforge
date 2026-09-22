"""Object storage for the queue consumer.

ReelForge has never had an object-storage client — everything lives under
`/data`. This adds one, used only by the queue interface: pull the source video
down from a presigned URL, push rendered variants back up.

Credentials come from this service's own environment, never from a job payload.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Raised for anything the caller should surface as a job failure."""


def _client():
    endpoint = os.environ.get("S3_ENDPOINT")
    return boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
        # MinIO needs path-style addressing; real S3 tolerates it.
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )


def bucket() -> str:
    name = os.environ.get("S3_BUCKET")
    if not name:
        raise StorageError("S3_BUCKET is not set on the ReelForge consumer")
    return name


def download(url: str, dest: Path, *, timeout: int = 900) -> Path:
    """Fetch a presigned URL to `dest`, streaming so a large file never lands in memory."""
    import urllib.error
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, tmp.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        # A 403 here almost always means the presigned URL outlived the queue
        # wait. Say so plainly instead of retrying into the same wall.
        hint = (
            " (the presigned URL has most likely expired; growth-agent should re-issue it)"
            if exc.code in (400, 403)
            else ""
        )
        raise StorageError(f"could not download source: HTTP {exc.code}{hint}") from exc
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim as a job failure
        raise StorageError(f"could not download source: {exc}") from exc

    tmp.replace(dest)
    log.info("downloaded source to %s (%d bytes)", dest, dest.stat().st_size)
    return dest


def upload(path: Path, key: str, *, content_type: str = "video/mp4") -> int:
    """Upload `path` to `key`. Returns the byte count written."""
    try:
        _client().upload_file(
            str(path), bucket(), key, ExtraArgs={"ContentType": content_type}
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError(f"could not upload {key}: {exc}") from exc
    size = path.stat().st_size
    log.info("uploaded %s (%d bytes)", key, size)
    return size
