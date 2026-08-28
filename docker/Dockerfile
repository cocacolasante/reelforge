# syntax=docker/dockerfile:1.7

# ---- Stage 1: system deps ----------------------------------------------------
FROM python:3.12.7-slim-bookworm AS system
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libass9 fontconfig fonts-inter \
        curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 --shell /bin/bash reelforge
ENTRYPOINT ["/usr/bin/tini", "--"]

# ---- Stage 2: python deps (cached separately from app code) ------------------
FROM system AS python-deps
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.30
COPY pyproject.toml uv.lock ./
# Skip installing the root project itself — code lands in the next stage.
# --frozen honors the committed uv.lock so rebuilds can't drift (an unpinned
# anthropic once jumped a major version on rebuild and broke ranking).
RUN uv sync --frozen --no-install-project

# ---- Stage 3: app code -------------------------------------------------------
FROM python-deps AS core
COPY packages/ packages/
# Only copy Python apps — apps/web is for the Node image and must not bloat this one.
COPY apps/__init__.py apps/__init__.py
COPY apps/api/ apps/api/
COPY apps/worker/ apps/worker/
COPY apps/cli/ apps/cli/
COPY assets/ /app/assets/
COPY docker/entrypoints/ /usr/local/bin/
RUN chmod +x /usr/local/bin/reelforge /usr/local/bin/api-entrypoint.sh /usr/local/bin/worker-entrypoint.sh \
    && mkdir -p /data /models/whisper \
    # Synthesize placeholder music tracks (one per mood). Deterministic + CC0.
    && bash /app/assets/music/synthesize_placeholders.sh /app/assets/music \
    # Synthesize the bundled LUTs (warm/cool/cinematic/vivid). ~460 KB total.
    && bash /app/assets/luts/synthesize_luts.sh /app/assets/luts \
    && chown -R reelforge:reelforge /data /models /app
USER reelforge
ENV PATH="/app/.venv/bin:/usr/local/bin:$PATH" \
    PYTHONPATH="/app:/app/packages/core" \
    REELFORGE_DATA_DIR=/data \
    WHISPER_MODEL_CACHE=/models/whisper

# ---- Stage 4a: API -----------------------------------------------------------
FROM core AS api
EXPOSE 8001
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8001"]

# ---- Stage 4b: Worker --------------------------------------------------------
FROM core AS worker
CMD ["arq", "apps.worker.main.WorkerSettings"]

# ---- Stage 4c: CLI -----------------------------------------------------------
FROM core AS cli
ENTRYPOINT ["/usr/bin/tini", "--", "reelforge"]
CMD ["--help"]
