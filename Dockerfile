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
COPY demo ./demo
RUN uv sync --frozen --no-dev --all-extras

ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA} \
    PORT=7860 \
    PATH="/app/.venv/bin:$PATH"

# Hugging Face Spaces expects port 7860.
EXPOSE 7860
CMD ["sh", "-c", "uvicorn dwarpal.app:app --host 0.0.0.0 --port ${PORT}"]
