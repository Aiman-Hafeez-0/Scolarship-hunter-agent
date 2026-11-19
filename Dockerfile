# The dashboard as a container (used by Render; works anywhere Docker runs).
#
#   docker build -t scholarship-dashboard .
#   docker run --env-file .env -e PORT=8000 -p 8000:8000 scholarship-dashboard
#
# Secrets are never copied into the image: they come from environment variables at run time.

FROM python:3.12-slim

# uv, pinned to the version used to make uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first: this slow step is reused until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Then the code. The dashboard (app/) imports parts of the pipeline (pipeline/).
COPY app ./app
COPY pipeline ./pipeline

# Run as a normal user, not root.
RUN useradd --create-home --uid 10001 dashboard
USER dashboard

# Render sets PORT (usually 10000); 8000 is the fallback for running it yourself.
ENV PORT=8000
EXPOSE 8000

# Docker's own check (Render uses healthCheckPath in render.yaml instead). The slim image has
# no curl, so Python asks /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['PORT'] + '/health', timeout=4)"

# "exec" makes uvicorn the main process, so it stops cleanly when Render stops the container.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"$PORT\""]
