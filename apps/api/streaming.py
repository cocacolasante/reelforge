"""HTTP Range helper for streaming large media files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import AsyncIterator

import aiofiles
from fastapi import Request
from fastapi.responses import FileResponse, Response, StreamingResponse

_RANGE_RE = re.compile(r"^bytes=(?P<start>\d*)-(?P<end>\d*)$")


def parse_range(header: str, file_size: int) -> tuple[int | None, int | None]:
    """Return (start, end) inclusive byte offsets, or (None, None) if unsatisfiable.

    Supports:
      bytes=START-END   → explicit
      bytes=START-      → from START to end
      bytes=-SUFFIX     → last SUFFIX bytes
    """
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None, None
    raw_start, raw_end = m.group("start"), m.group("end")
    if raw_start == "" and raw_end == "":
        return None, None
    if raw_start == "":
        # Suffix range: last N bytes
        suffix = int(raw_end)
        if suffix <= 0:
            return None, None
        start = max(0, file_size - suffix)
        end = file_size - 1
        return start, end
    start = int(raw_start)
    end = int(raw_end) if raw_end else file_size - 1
    if start > end or start >= file_size:
        return None, None
    end = min(end, file_size - 1)
    return start, end


async def stream_file_with_range(
    path: Path,
    request: Request,
    *,
    media_type: str,
    filename_for_download: str | None = None,
    cache_control: str = "private, max-age=3600",
) -> Response:
    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
    }
    if filename_for_download:
        headers["Content-Disposition"] = (
            f'attachment; filename="{filename_for_download}"'
        )

    if range_header is None:
        return FileResponse(path, media_type=media_type, headers=headers)

    start, end = parse_range(range_header, file_size)
    if start is None:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    length = end - start + 1
    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        }
    )

    async def _iter() -> AsyncIterator[bytes]:
        async with aiofiles.open(path, "rb") as f:
            await f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = await f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _iter(), status_code=206, media_type=media_type, headers=headers
    )
