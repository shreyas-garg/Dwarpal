FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --all-extras --no-install-project

COPY dwarpal ./dwarpal
COPY policies ./policies
COPY config ./config
COPY demo ./demo
COPY scripts/fetch_models.py ./scripts/fetch_models.py
RUN uv sync --frozen --no-dev --all-extras

# PR-05: download every model the policies use into the image, so a cold start never fetches
# one. Spaces run the container as uid 1000, so the cache must be readable by everyone.
ENV HF_HOME=/app/.cache/huggingface
RUN /app/.venv/bin/python scripts/fetch_models.py && chmod -R a+rX /app/.cache

ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA} \
    PORT=7860 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HUB_OFFLINE=1 \
    RATE_LIMIT_PER_MINUTE=20 \
    DAILY_REQUEST_CAP=200 \
    TRUST_FORWARDED_FOR=true

# Same uid Spaces uses, so a local `docker run` behaves like the Space.
# PR-07: the request log (data/requests.db) needs a directory that user can write.
RUN useradd -m -u 1000 user && mkdir -p /app/data && chown user /app/data
USER user
ENV HOME=/home/user

# Hugging Face Spaces expects port 7860.
EXPOSE 7860
CMD ["sh", "-c", "uvicorn dwarpal.app:app --host 0.0.0.0 --port ${PORT}"]
