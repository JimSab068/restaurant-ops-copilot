# syntax=docker/dockerfile:1

##############################################################################
# Restaurant Ops Copilot -- shared image for both the API service
# (api/main.py, served via uvicorn) and the worker (worker/main.py's SQS
# poll loop, which calls worker/handler.py's process_message()). Select
# which process runs via CMD at `docker run` / ECS task-definition time --
# see the two CMD examples at the bottom of this file.
#
# ASSUMPTIONS -- please confirm/correct:
#   1. Dependencies are declared in requirements.txt at the repo root.
#      If you're on poetry/uv/Pipenv instead, tell me and I'll swap the
#      install step for the right one.
#   2. Python 3.11 (Component_1/Component_4's `str | None` syntax needs
#      Python >=3.10).
#   3. Postgres driver is psycopg2-binary (ships prebuilt wheels, so the
#      builder stage doesn't need libpq-dev headers -- just build-essential
#      as a safety net for anything else in requirements.txt that isn't
#      wheel-only). If you're on psycopg (v3) or asyncpg instead, say so
#      and I'll adjust the driver + system packages in both stages.
##############################################################################

FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.11-slim AS runtime

# libpq5 is the runtime shared library psycopg2-binary's wheel links
# against -- distinct from libpq-dev (headers), which is only needed to
# compile against libpq, not to run against it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=appuser:appuser . .

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runtime configuration (see shared/settings.py, api/generate_api_key.py,
# shared/queue.py) is expected via environment, not baked into the image:
#   DATABASE_URL, API_KEY_PEPPER, SQS_QUEUE_URL, AWS_REGION, and optionally
#   SQS_ENDPOINT_URL for an SQS-compatible local service such as LocalStack,
#   and whatever
#   SYNTHETIC_EXECUTION_ONLY expects (worker/handler.py). Pass these at
#   `docker run -e ...` / via the ECS task definition's environment block,
#   not with an ENV or ARG line here -- especially API_KEY_PEPPER, which
#   should come from a secret (ECS Secrets Manager / Parameter Store
#   integration), never a plain env var baked into the image or task def.
#
# One-off database bootstrap (same image locally and in the cloud):
#   SEED_DATABASE_URL=<seed-capable URL>
#   RLS_ADMIN_DATABASE_URL=<non-superuser table-owner URL>
#   ./bootstrap_database.sh
# Keep both URLs in runtime secrets; do not bake them into this image.

EXPOSE 8000

# Default: run the API.
#   docker run -p 8000:8000 --env-file .env <image>
#
# Override to run the worker instead (adjust the module path if
# worker/main.py's entrypoint differs):
#   docker run --env-file .env <image> python -m worker.main
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]