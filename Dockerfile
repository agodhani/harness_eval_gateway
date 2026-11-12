# Control-plane API image (FastAPI + SQLAlchemy + Alembic).
# Sandboxes use their own per-harness images (F4), not this one.
FROM python:3.12-slim

# uv for fast, lockfile-reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# docker CLI — the orchestrator (F2) drives SandboxRuntime, which shells out
# to `docker` against the mounted /var/run/docker.sock (compose).
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

# Install deps first for layer caching (dev deps excluded).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
