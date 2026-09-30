"""The phone upload page an agent hands you.

MCP tools carry JSON, so an agent can never take the video itself. Instead
`start_upload` mints a signed link; you open it on your phone, pick clips,
and they land in the project the link names. The link is the credential, so
it is short-lived and scoped to exactly one project.

Root-level (not /api/v1) because the tunnel forwards whole path prefixes and
this one has to be tappable from a phone.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db
from apps.api.streaming import stream_file_with_range
from reelforge_core import links
from reelforge_core import links as signing
from apps.api.services.ingest_file import UnreadableMedia, ingest_media_file
from apps.api.settings import settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["upload-link"])

UPLOAD_LINK_TTL_S = 6 * 60 * 60  # long enough to find the clips, short enough to expire
PURPOSE = "upload"


def upload_url(project_id: str, *, ttl_seconds: int = UPLOAD_LINK_TTL_S) -> str:
    token = signing.sign({"p": PURPOSE, "project": project_id}, ttl_seconds)
    base = (settings.public_media_base or settings.public_api_base).rstrip("/")
    return f"{base}/upload/{token}"


async def _project_for(token: str, db: AsyncSession) -> dbmod.Project | None:
    payload = signing.verify(token)
    if payload is None or payload.get("p") != PURPOSE:
        return None
    return await db.get(dbmod.Project, str(payload.get("project", "")))


_EXPIRED_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link expired</title><style>%(css)s</style></head>
<body><main><h1>This upload link has expired</h1>
<p>Ask for a new one and it will work straight away.</p></main></body></html>"""

_CSS = """
:root { color-scheme: dark; }
body { margin:0; font: 16px/1.5 -apple-system, system-ui, sans-serif;
  background:#0b0c0e; color:#e8e8ea; }
main { max-width: 34rem; margin: 0 auto; padding: 2rem 1rem 4rem; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
p.sub { color:#9a9aa2; margin:0 0 1.5rem; }
label.pick { display:block; text-align:center; padding:2rem 1rem; cursor:pointer;
  border:1px dashed #3a3a42; border-radius:14px; background:#141519; }
label.pick:active { background:#1b1c22; }
input[type=file] { display:none; }
.big { font-size:1.05rem; font-weight:600; }
ul { list-style:none; padding:0; margin:1.5rem 0 0; }
li { display:flex; gap:.75rem; align-items:center; padding:.6rem 0;
  border-bottom:1px solid #212229; font-size:.92rem; }
li .name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
li .state { color:#9a9aa2; font-variant-numeric:tabular-nums; }
li.done .state { color:#4ade80; }
li.failed .state { color:#f87171; }
.bar { height:3px; background:#212229; border-radius:2px; overflow:hidden; margin-top:.75rem; }
.bar i { display:block; height:100%; width:0; background:#6366f1; transition:width .2s; }
footer { margin-top:2rem; color:#6f6f78; font-size:.8rem; }
"""

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Send footage to ReelForge</title><style>%(css)s</style></head>
<body><main>
<h1>Send footage</h1>
<p class="sub">Goes to <strong>%(project)s</strong>. Keep this page open until
every clip finishes.</p>
<label class="pick"><span class="big">Choose videos</span><br>
<span class="sub">or photos to cut in</span>
<input id="picker" type="file" accept="video/*,image/*" multiple></label>
<div class="bar"><i id="bar"></i></div>
<ul id="list"></ul>
<footer>This link expires. Anyone with it can add footage to this project.</footer>
</main>
<script>
const token = %(token)s;
const list = document.getElementById('list');
const bar = document.getElementById('bar');
document.getElementById('picker').addEventListener('change', async (e) => {
  const files = [...e.target.files];
  e.target.value = '';
  for (const file of files) {
    const li = document.createElement('li');
    li.innerHTML = '<span class="name"></span><span class="state">waiting</span>';
    li.querySelector('.name').textContent = file.name;
    list.appendChild(li);
    const state = li.querySelector('.state');
    try {
      const body = await send(file, (pct) => {
        state.textContent = pct + '%%'; bar.style.width = pct + '%%';
      });
      li.className = 'done';
      // Same bytes already here: say so rather than implying it was added.
      state.textContent = body.added === false ? (body.note || 'already here') : 'sent';
    } catch (err) {
      li.className = 'failed';
      state.textContent = String(err.message || err).slice(0, 60);
    }
    bar.style.width = '0';
  }
});
// XHR, not fetch: it reports upload progress, which matters when a 4K clip
// takes a minute over cellular and the page would otherwise look frozen.
function send(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload/' + token + '/file');
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) onProgress(Math.round((ev.loaded / ev.total) * 100));
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body.error || ('HTTP ' + xhr.status)));
    };
    xhr.onerror = () => reject(new Error('network error'));
    xhr.send(form);
  });
}
</script></body></html>"""


@router.get("/media/{token}")
async def signed_media(token: str, request: Request) -> Response:
    """Serve one exported clip to whoever holds an unexpired link.

    Deliberately outside every auth gate, like growth-agent's /media: the
    link IS the capability, and it has to work when tapped in a chat, by a
    phone that has no key. The token names a file relative to the outputs
    directory and nothing else, so it can't be pointed at the database or a
    source video.
    """
    path = links.resolve_media_token(token)
    if path is None:
        return JSONResponse(
            status_code=404, content={"error": "this link has expired or is invalid"}
        )
    return await stream_file_with_range(
        path,
        request,
        media_type="video/mp4",
        filename_for_download=path.name,
        cache_control="private, no-cache",
    )


@router.get("/upload/{token}", response_class=HTMLResponse)
async def upload_page(token: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    project = await _project_for(token, db)
    if project is None:
        return HTMLResponse(_EXPIRED_PAGE % {"css": _CSS}, status_code=404)
    import json as _json

    return HTMLResponse(
        _PAGE % {"css": _CSS, "project": project.name, "token": _json.dumps(token)}
    )


@router.post("/upload/{token}/file")
async def upload_one_file(
    token: str, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
) -> JSONResponse:
    project = await _project_for(token, db)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "this link has expired"})

    limit = int(settings.max_upload_gb * (1024**3))
    tmp_dir = Path(settings.data_dir) / "uploads" / ".incoming"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, suffix=Path(file.filename or "").suffix)
    tmp = Path(tmp_name)
    written = 0
    try:
        with open(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"file exceeds the {settings.max_upload_gb:.1f} GB limit")
                out.write(chunk)
        asset, created = await ingest_media_file(
            db, project.id, tmp, file.filename or tmp.name, move=True
        )
    except ValueError as exc:
        tmp.unlink(missing_ok=True)
        return JSONResponse(status_code=413, content={"error": str(exc)})
    except UnreadableMedia as exc:
        tmp.unlink(missing_ok=True)
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception:
        tmp.unlink(missing_ok=True)
        log.exception("upload via link failed for project %s", project.id)
        return JSONResponse(status_code=500, content={"error": "could not store that file"})

    body: dict = {
        "assetId": asset.id,
        "filename": asset.original_filename,
        "kind": asset.kind,
        "durationSec": asset.duration_sec,
        "added": created,
    }
    if not created:
        # Same bytes as footage already here. Say where it actually lives —
        # silently "adding" it to this batch would be a lie the agent repeats.
        owner = await db.get(dbmod.Project, asset.project_id)
        body["note"] = (
            f"already in ReelForge, in {owner.name!r}"
            if owner is not None and owner.id != project.id
            else "already in this batch"
        )
        body["projectId"] = asset.project_id
    log.info(
        "link upload: %s -> project %s (%s)",
        asset.id[:12], project.id, "new" if created else "duplicate",
    )
    return JSONResponse(body)
