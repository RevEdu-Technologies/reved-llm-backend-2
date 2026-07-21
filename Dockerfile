# syntax=docker/dockerfile:1.7
# RevEd LLM Backend — production image.
#
# Build from the repository root so the build context contains main.py,
# app/, requirements.txt, etc:
#
#   docker build -f docker/Dockerfile -t reved-backend:local .
#
# Run with an .env file (DATABASE_URL, GROQ_API_KEY, PINECONE_API_KEY,
# SUPABASE_JWT_SECRET, REDIS_URL, ...):
#
#   docker run --rm -p 8000:8000 --env-file .env reved-backend:local

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   - libpq for psycopg sync driver (Alembic uses it for migrations).
#   - poppler-utils + tesseract-ocr for pdf2image / pytesseract used by the
#     ingestion pipeline. They're listed in requirements.txt, so the image
#     needs the binaries to import cleanly.
#   - build-essential is only kept around long enough to compile any wheels
#     that lack a manylinux build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq5 \
        libpq-dev \
        poppler-utils \
        tesseract-ocr \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so the layer is cached when only source changes.
# Notes
#   - sentence-transformers pulls torch transitively (~900MB), so the cold
#     download is large. We bump pip's per-read timeout and retry count so
#     a brief TLS blip doesn't abort the whole image build.
#   - The BuildKit cache mount keeps downloaded wheels around between
#     builds, which means a failed build can resume from where it stopped
#     instead of re-pulling hundreds of MB. Requires the
#     `# syntax=docker/dockerfile:1.7` directive at the top of the file.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        --timeout 300 \
        --retries 10 \
        -r requirements.txt

# Drop compiler toolchain to keep the runtime image lean.
RUN apt-get purge -y --auto-remove build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy application source. .dockerignore keeps tests, notebooks, .env, etc. out.
COPY main.py alembic.ini ./
COPY app ./app
COPY scripts ./scripts

# Run as a non-root user.
RUN groupadd --system reved \
    && useradd --system --gid reved --home-dir /app --shell /usr/sbin/nologin reved \
    && chown -R reved:reved /app
USER reved

EXPOSE 8000

# Liveness probe target — matches /api/v1/health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/api/v1/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
