"""The tools an MCP agent holding a ReelForge key can call.

Every tool drives the SAME HTTP route the web app uses, in-process, carrying
the caller's key. That is deliberate: validation, job bookkeeping and the
409s that stop duplicate work live in those routes, and a second
implementation would drift from the first.

WHAT IS ABSENT MATTERS AS MUCH AS WHAT IS HERE. No tool publishes to
YouTube, Instagram or TikTok — posting stays in the dashboard, where the
video is watched before it goes out. No tool deletes a project, a clip or a
key. A guardrail test pins this list, so widening it is a decision rather
than an accident.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx


class ToolError(Exception):
    """Refusal/validation message surfaced to the agent as tool output."""


@dataclass
class ToolContext:
    authorization: str  # the caller's "Bearer rf_…" header, re-presented in-process
    client: httpx.AsyncClient

    async def call(
        self, method: str, path: str, *, params: dict | None = None, body: Any = None
    ) -> Any:
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                path = f"{path}?{urlencode(clean, doseq=True)}"
        resp = await self.client.request(
            method, path, json=body, headers={"authorization": self.authorization}
        )
        data: Any = resp.json() if resp.content else None
        if resp.status_code >= 400:
            raise ToolError(_error_message(data, resp.status_code))
        return data


def _error_message(data: Any, status: int) -> str:
    """ReelForge answers errors as {"error": {"code", "message"}}; FastAPI's
    own validation failures come back as {"detail": [...]}."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        detail = data.get("detail")
        if isinstance(detail, list):
            return "; ".join(
                f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}"
                for d in detail
            )
        if detail:
            return str(detail)
    return f"HTTP {status}"


@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    run: Callable[[ToolContext, dict], Awaitable[Any]]
    mutates: bool = False
    extra: dict = field(default_factory=dict)

    def listing(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": not self.mutates,
                "destructiveHint": False,
                "idempotentHint": not self.mutates,
                "openWorldHint": False,
                **self.extra,
            },
        }


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


API = "/api/v1"


# --- overview -----------------------------------------------------------------


OVERVIEW_PROJECTS = 20


async def overview(ctx: ToolContext, args: dict) -> dict:
    """Fan-in: the projects here and what footage each already holds."""
    listing = await ctx.call("GET", f"{API}/projects", params={"limit": OVERVIEW_PROJECTS})
    rows = listing.get("projects", []) if isinstance(listing, dict) else []
    # One call per project, in parallel — the listing carries no asset counts,
    # and the count is what tells the agent whether a project is worth cutting.
    assets = await asyncio.gather(
        *(ctx.call("GET", f"{API}/projects/{p['id']}/assets") for p in rows),
        return_exceptions=True,
    )
    summaries = []
    for project, got in zip(rows, assets):
        clips = got.get("assets", []) if isinstance(got, dict) else []
        summaries.append(
            {
                "projectId": project.get("id"),
                "name": project.get("name"),
                # Photos and voiceover takes live alongside footage; only
                # video can be cut, so that is what gets counted.
                "videoClips": sum(1 for a in clips if a.get("kind") == "video"),
                "createdAt": project.get("created_at"),
            }
        )
    return {
        "projects": summaries,
        "projectCount": listing.get("total", len(summaries)) if isinstance(listing, dict) else len(summaries),
        "shown": len(summaries),
    }


# --- footage in ----------------------------------------------------------------


async def start_upload(ctx: ToolContext, args: dict) -> dict:
    """Mint a link the user taps to send clips from their phone."""
    name = str(args.get("name") or "").strip()
    result = await ctx.call(
        "POST", f"{API}/agent/upload-link", body={"name": name} if name else {}
    )
    return result


async def list_footage(ctx: ToolContext, args: dict) -> dict:
    """Recent projects with the footage they hold, newest first."""
    limit = int(args.get("limit") or 5)
    if not 1 <= limit <= 20:
        raise ToolError("limit must be between 1 and 20")
    listing = await ctx.call("GET", f"{API}/projects", params={"limit": limit})
    rows = listing.get("projects", []) if isinstance(listing, dict) else []
    assets = await asyncio.gather(
        *(ctx.call("GET", f"{API}/projects/{p['id']}/assets") for p in rows),
        return_exceptions=True,
    )
    batches = []
    for project, got in zip(rows, assets):
        clips = got.get("assets", []) if isinstance(got, dict) else []
        videos = [a for a in clips if a.get("kind") == "video"]
        batches.append(
            {
                "projectId": project.get("id"),
                "name": project.get("name"),
                "createdAt": project.get("created_at"),
                "videoClips": len(videos),
                "photos": sum(1 for a in clips if a.get("kind") == "photo"),
                "totalSeconds": round(sum(a.get("duration_sec") or 0 for a in videos), 1),
                "clips": [
                    {
                        "assetId": a.get("id"),
                        "filename": a.get("original_filename"),
                        "durationSec": a.get("duration_sec"),
                        "width": a.get("width"),
                        "height": a.get("height"),
                    }
                    for a in videos[:12]
                ],
            }
        )
    return {"batches": batches}


# --- cutting -------------------------------------------------------------------


def _project_arg(args: dict) -> str:
    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        raise ToolError("project_id is required — get it from list_footage")
    return project_id


async def _start_cut(ctx: ToolContext, body: dict) -> dict:
    job = await ctx.call("POST", f"{API}/agent/cuts", body=body)
    return {
        "jobId": job.get("id"),
        "status": job.get("status"),
        "note": (
            "Started. This takes minutes — poll check_job with this jobId, and "
            "tell the user roughly how long it will be rather than waiting silently."
        ),
    }


def _delivery_arg(args: dict) -> list[str] | None:
    raw = args.get("delivery")
    if raw is None:
        return None
    wanted = [raw] if isinstance(raw, str) else list(raw)
    unknown = [c for c in wanted if c not in ("links", "folder", "email")]
    if unknown:
        raise ToolError(
            f"delivery can be links, folder or email — not {', '.join(map(str, unknown))}"
        )
    return wanted


async def cut_reels(ctx: ToolContext, args: dict) -> dict:
    body: dict = {"project_id": _project_arg(args), "mode": "reels"}
    delivery = _delivery_arg(args)
    if delivery:
        body["delivery"] = delivery
    for key, arg in (("count", "count"), ("min_sec", "min_sec"), ("max_sec", "max_sec")):
        if args.get(arg) is not None:
            body[key] = args[arg]
    if args.get("direction"):
        body["prompt"] = str(args["direction"])
    return await _start_cut(ctx, body)


async def make_long_video(ctx: ToolContext, args: dict) -> dict:
    body: dict = {"project_id": _project_arg(args), "mode": "long"}
    delivery = _delivery_arg(args)
    if delivery:
        body["delivery"] = delivery
    if args.get("target_duration_sec") is not None:
        body["target_duration_sec"] = args["target_duration_sec"]
    if args.get("direction"):
        body["prompt"] = str(args["direction"])
    return await _start_cut(ctx, body)


# Stages the pipeline reports, in words an agent can relay to a person.
_STAGE_WORDS = {
    "analyze": "watching the footage",
    "probe": "reading the footage",
    "scenes": "finding the shots",
    "transcribe": "listening to what's said",
    "loudness": "measuring the audio",
    "energy": "measuring the action",
    "semantics": "understanding each shot",
    "select": "picking the best moments",
    "prepare": "getting ready to render",
    "clips": "cutting the clips",
    "captions": "adding captions",
    "music": "adding music",
    "render": "rendering",
    "finalize": "finishing up",
    "transcode": "exporting",
    "done": "done",
}


async def check_job(ctx: ToolContext, args: dict) -> dict:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ToolError("job_id is required — cut_reels and make_long_video return one")
    job = await ctx.call("GET", f"{API}/jobs/{job_id}")
    stage = job.get("stage") or ""
    out: dict = {
        "jobId": job.get("id"),
        "status": job.get("status"),
        "percent": round(float(job.get("progress") or 0.0) * 100),
        "doing": _STAGE_WORDS.get(stage, stage or "starting"),
    }
    if job.get("status") == "done":
        result = job.get("result") or {}
        out["clips"] = result.get("clips", [])
        for key in ("note", "failures", "projectId", "elapsedSec", "delivery"):
            if result.get(key) is not None:
                out[key] = result[key]
        # A long video reports itself as one rendered reel rather than a list.
        if not out["clips"] and result.get("mezzanine_path"):
            out["longVideo"] = {
                "reelId": result.get("reel_id"),
                "durationSec": result.get("duration_sec"),
                "title": result.get("title"),
            }
    elif job.get("status") == "failed":
        out["error"] = job.get("error")
    return out


TOOLS: list[Tool] = [
    Tool(
        "overview",
        "What's in ReelForge",
        "Start here. Lists this ReelForge's projects (a project holds the raw "
        "footage for one shoot or topic) with how many clips each holds, plus "
        "disk usage. Read-only. Use it to find the projectId for any other "
        "tool, and to tell the user what footage you can already work with.",
        _obj({}),
        overview,
    ),
    Tool(
        "start_upload",
        "Get a link for sending footage",
        "Returns a link the user opens on their phone to send raw footage into "
        "ReelForge. You cannot upload video yourself — give the user this link "
        "and tell them to tap it and pick their clips. It expires in a few "
        "hours. Pass a name to say what the footage is (\"skimboard session\"); "
        "the clips land in a project by that name, and cut_reels takes its "
        "projectId. Poll list_footage to see the clips arrive.",
        _obj(
            {
                "name": {
                    "type": "string",
                    "description": "What this footage is, e.g. 'skimboard session'. "
                    "Defaults to a dated name.",
                }
            }
        ),
        start_upload,
        mutates=True,
    ),
    Tool(
        "list_footage",
        "What footage is here",
        "The most recent batches of footage and the clips in each, newest "
        "first. Use it to confirm the user's upload landed, to see how much "
        "footage there is before cutting, and to get the projectId. Files "
        "dropped into the watch folder show up here too, without an upload "
        "link.",
        _obj(
            {
                "limit": {
                    "type": "integer",
                    "description": "How many batches to return (1-20, default 5).",
                }
            }
        ),
        list_footage,
    ),
    Tool(
        "cut_reels",
        "Cut short clips from footage",
        "Turn a batch of footage into short vertical clips: ReelForge watches "
        "the footage, reads what is said, picks the strongest moments and "
        "renders them with captions and music. Takes minutes to tens of "
        "minutes — it returns a jobId to poll with check_job, not the clips. "
        "One run per project at a time. Pass a direction to steer what it "
        "looks for (\"the part about pricing\", \"the funniest bits\").",
        _obj(
            {
                "project_id": {
                    "type": "string",
                    "description": "Which batch to cut, from list_footage.",
                },
                "count": {
                    "type": "integer",
                    "description": "How many clips to make (1-10, default 3).",
                },
                "min_sec": {"type": "number", "description": "Shortest clip, seconds."},
                "max_sec": {"type": "number", "description": "Longest clip, seconds."},
                "direction": {
                    "type": "string",
                    "description": "What to look for, in the user's own words. "
                    "Clips that don't match are dropped, so a narrow direction "
                    "can return nothing.",
                },
                "delivery": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["links", "folder", "email"]},
                    "description": "How the finished clips come back: links (a "
                    "URL per clip), folder (copied into the user's synced "
                    "folder), email. Any combination; omit to use their "
                    "default. Ask if they haven't said.",
                },
            },
            ["project_id"],
        ),
        cut_reels,
        mutates=True,
    ),
    Tool(
        "make_long_video",
        "Build one long video from several clips",
        "Mix every clip in a batch into ONE longer video: the best sections "
        "from each, ordered so it plays as one piece. Needs at least two "
        "clips. Slower than cut_reels — returns a jobId to poll with "
        "check_job. Use this when the user wants a single video rather than "
        "several short ones.",
        _obj(
            {
                "project_id": {
                    "type": "string",
                    "description": "Which batch to build from, from list_footage.",
                },
                "target_duration_sec": {
                    "type": "number",
                    "description": "Roughly how long, in seconds (60-1800, default 300).",
                },
                "direction": {
                    "type": "string",
                    "description": "What the video should be about, in the user's words.",
                },
                "delivery": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["links", "folder", "email"]},
                    "description": "How the finished clips come back: links (a "
                    "URL per clip), folder (copied into the user's synced "
                    "folder), email. Any combination; omit to use their "
                    "default. Ask if they haven't said.",
                },
            },
            ["project_id"],
        ),
        make_long_video,
        mutates=True,
    ),
    Tool(
        "check_job",
        "How is the cutting going",
        "Progress for a cut_reels or make_long_video job: what it is doing "
        "right now and how far along. When it finishes, this is where the "
        "clips appear. Poll it every minute or so rather than continuously, "
        "and tell the user what stage it is at.",
        _obj(
            {"job_id": {"type": "string", "description": "The jobId you were given."}},
            ["job_id"],
        ),
        check_job,
    ),
]

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}
