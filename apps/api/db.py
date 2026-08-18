"""SQLModel entities + async engine. WAL mode, serialized writes via busy_timeout.

The `jobs` table is the single source of truth for job state, shared between the
API (which enqueues + reads) and the workers (which write progress/result/error).
arq's job id is made equal to our DB id via `_job_id=db_job.id` at enqueue time.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from sqlalchemy import UniqueConstraint, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=_now)
    source_asset_id: Optional[str] = Field(default=None, index=True)


class Asset(SQLModel, table=True):
    __tablename__ = "assets"

    id: str = Field(primary_key=True)  # content-addressed sha256 of head+tail+size
    project_id: str = Field(foreign_key="projects.id", index=True)
    # "video" (footage that gets analyzed into reels) or "photo" (a still,
    # inserted into reels at compose time — never analyzed or selected).
    kind: str = Field(default="video", index=True)
    path: str
    original_filename: str
    duration_sec: float
    width: int
    height: int
    fps: float
    has_audio: bool
    size_bytes: int
    probe_json: str
    created_at: datetime = Field(default_factory=_now)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: str = Field(default_factory=_uuid, primary_key=True)
    arq_job_id: str = Field(default="", index=True)
    project_id: Optional[str] = Field(default=None, index=True)
    kind: str = Field(index=True)          # analyze | select | compose | export
    asset_id: Optional[str] = Field(default=None, index=True)
    reel_id: Optional[str] = Field(default=None, index=True)
    preset_id: Optional[str] = Field(default=None)
    status: str = Field(default="queued", index=True)
    progress: float = 0.0
    stage: Optional[str] = None
    message: Optional[str] = None
    config_json: Optional[str] = None
    result_json: Optional[str] = None
    error_message: Optional[str] = None
    error_traceback: Optional[str] = None
    logs_json: str = "[]"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)


class Reel(SQLModel, table=True):
    __tablename__ = "reels"

    id: str = Field(primary_key=True)       # = candidate_id, OR a synthetic "montage-..."
    project_id: str = Field(foreign_key="projects.id", index=True)
    # For a montage reel the asset_id is the first chapter's asset (used as a
    # fallback for thumbnails). asset_id stays non-null so we don't have to
    # touch every code path that joins through it.
    asset_id: str = Field(foreign_key="assets.id", index=True)
    rank: int
    title: str
    hook: str
    justification: str
    start_sec: float
    end_sec: float
    duration_sec: float
    overall_score: float
    suggested_mood: str
    scene_indices_json: str
    scores_json: str
    compose_job_id: Optional[str] = Field(default=None, index=True)
    mezzanine_path: Optional[str] = None
    trim_start_offset_sec: float = 0.0
    trim_end_offset_sec: float = 0.0
    # Phase 7.x long-form montage: when set, this Reel is the concatenation of
    # the listed child reel_ids. The compose worker takes the montage path
    # instead of the normal extract → render path.
    child_reel_ids_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class Export(SQLModel, table=True):
    __tablename__ = "exports"

    id: str = Field(default_factory=_uuid, primary_key=True)
    reel_id: str = Field(foreign_key="reels.id", index=True)
    preset_id: str
    export_job_id: Optional[str] = Field(default=None, index=True)
    output_path: Optional[str] = None
    file_size_bytes: Optional[int] = None
    created_at: datetime = Field(default_factory=_now)


class SocialAccount(SQLModel, table=True):
    """One row per connected channel/identity. A Google login with several
    YouTube channels (brand accounts) authorizes ONE channel per OAuth grant,
    so users connect each channel they want and pick one at publish time."""

    __tablename__ = "social_accounts"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)

    id: str = Field(default_factory=_uuid, primary_key=True)
    platform: str = Field(index=True)  # "youtube" | "instagram" | "tiktok"
    external_id: str = Field(index=True)  # channel id / ig user id / open_id
    access_token: str
    # Instagram has no refresh token (the long-lived access token refreshes
    # itself); stored as "" there.
    refresh_token: str
    display_name: Optional[str] = None  # channel / account title
    token_expires_at: Optional[datetime] = None
    extra_json: Optional[str] = None  # platform-specific ids (ig user_id, open_id)
    created_at: datetime = Field(default_factory=_now)


class Publication(SQLModel, table=True):
    __tablename__ = "publications"

    id: str = Field(default_factory=_uuid, primary_key=True)
    reel_id: str = Field(foreign_key="reels.id", index=True)
    platform: str = Field(index=True)
    account_id: Optional[str] = Field(default=None, index=True)
    channel_title: Optional[str] = None  # display snapshot at publish time
    # Random token guarding the public media URL Meta fetches during an
    # Instagram publish. Null for platforms that upload directly.
    public_token: Optional[str] = None
    preset_id: str
    title: str
    description: str = ""
    privacy: str = "private"  # private | unlisted | public
    status: str = "queued"  # queued | uploading | done | failed
    publish_job_id: Optional[str] = Field(default=None, index=True)
    video_id: Optional[str] = None
    video_url: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
    completed_at: Optional[datetime] = None


class UploadSession(SQLModel, table=True):
    __tablename__ = "upload_sessions"

    id: str = Field(default_factory=_uuid, primary_key=True)
    project_id: str = Field(foreign_key="projects.id", index=True)
    filename: str
    content_type: str
    total_bytes: int
    received_bytes: int = 0
    chunk_size: int
    parts_dir: str
    status: str = Field(default="active", index=True)     # active | completed | aborted
    asset_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    completed_at: Optional[datetime] = None


class SemanticsCache(SQLModel, table=True):
    """Moved from reelforge_core.db to unify schemas. Same layout."""

    __tablename__ = "semantics_cache"

    cache_key: str = Field(primary_key=True)
    result_json: str
    model: str
    prompt_version: str
    created_at: str


# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------


def make_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,  # one connection per request — SQLite + async concurrency safe
        echo=False,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _connection_record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_all(engine: AsyncEngine) -> None:
    """Create tables if missing. Idempotent. Phase 5 ships without Alembic —
    revisit if schema evolution becomes a real concern.

    One in-place migration: the Phase-1 `jobs` table used `job_id` as PK. The
    Phase-5 schema uses `id`. If the legacy column layout is on disk we drop
    and recreate — no production jobs existed before Phase 5, so losing the
    old rows is acceptable.
    """
    from sqlalchemy import text

    async with engine.begin() as conn:
        cols = (
            await conn.execute(text("PRAGMA table_info(jobs)"))
        ).fetchall()
        col_names = {c[1] for c in cols}
        if cols and "id" not in col_names:
            await conn.execute(text("DROP TABLE jobs"))
        # social_accounts gained external_id (multi-channel support) right
        # after the feature shipped; no accounts could exist under the old
        # single-channel schema, so drop-and-recreate is safe.
        social_cols = {
            c[1]
            for c in (
                await conn.execute(text("PRAGMA table_info(social_accounts)"))
            ).fetchall()
        }
        if social_cols and "external_id" not in social_cols:
            await conn.execute(text("DROP TABLE social_accounts"))
        await conn.run_sync(SQLModel.metadata.create_all)
        # Additive assets column (photo support).
        asset_cols = {
            c[1]
            for c in (await conn.execute(text("PRAGMA table_info(assets)"))).fetchall()
        }
        if asset_cols and "kind" not in asset_cols:
            await conn.execute(
                text("ALTER TABLE assets ADD COLUMN kind TEXT NOT NULL DEFAULT 'video'")
            )
        # Additive publications columns (multi-channel support).
        pub_cols = {
            c[1]
            for c in (
                await conn.execute(text("PRAGMA table_info(publications)"))
            ).fetchall()
        }
        if pub_cols and "account_id" not in pub_cols:
            await conn.execute(text("ALTER TABLE publications ADD COLUMN account_id TEXT"))
        if pub_cols and "channel_title" not in pub_cols:
            await conn.execute(
                text("ALTER TABLE publications ADD COLUMN channel_title TEXT")
            )
        if pub_cols and "public_token" not in pub_cols:
            await conn.execute(
                text("ALTER TABLE publications ADD COLUMN public_token TEXT")
            )
        # Additive social_accounts columns (Instagram/TikTok support).
        social_cols2 = {
            c[1]
            for c in (
                await conn.execute(text("PRAGMA table_info(social_accounts)"))
            ).fetchall()
        }
        if social_cols2 and "token_expires_at" not in social_cols2:
            await conn.execute(
                text("ALTER TABLE social_accounts ADD COLUMN token_expires_at TIMESTAMP")
            )
        if social_cols2 and "extra_json" not in social_cols2:
            await conn.execute(
                text("ALTER TABLE social_accounts ADD COLUMN extra_json TEXT")
            )
        # Idempotent additive migrations: add trim_* columns to reels if absent.
        reel_cols = {
            c[1]
            for c in (await conn.execute(text("PRAGMA table_info(reels)"))).fetchall()
        }
        if reel_cols and "trim_start_offset_sec" not in reel_cols:
            await conn.execute(
                text(
                    "ALTER TABLE reels ADD COLUMN trim_start_offset_sec REAL NOT NULL DEFAULT 0.0"
                )
            )
        if reel_cols and "trim_end_offset_sec" not in reel_cols:
            await conn.execute(
                text(
                    "ALTER TABLE reels ADD COLUMN trim_end_offset_sec REAL NOT NULL DEFAULT 0.0"
                )
            )
        # Phase 7.x: child_reel_ids_json column (nullable, no default needed).
        if reel_cols and "child_reel_ids_json" not in reel_cols:
            await conn.execute(
                text("ALTER TABLE reels ADD COLUMN child_reel_ids_json TEXT")
            )


# ---------------------------------------------------------------------------
# Dependency helper
# ---------------------------------------------------------------------------


class DbState:
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]


db_state = DbState()


async def init_db(database_url: str) -> None:
    db_state.engine = make_engine(database_url)
    db_state.sessionmaker = make_sessionmaker(db_state.engine)
    await create_all(db_state.engine)


async def dispose_db() -> None:
    engine = getattr(db_state, "engine", None)
    if engine is not None:
        await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with db_state.sessionmaker() as session:
        yield session
