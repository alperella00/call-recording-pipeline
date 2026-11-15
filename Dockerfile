FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/venv \
    PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY config ./config
RUN uv sync --frozen --no-dev

# Test image: adds dev dependencies and the tests. `docker compose run --rm tests`
FROM base AS test
RUN uv sync --frozen
COPY tests ./tests
# The CRM mock, so the CRM tests run against the real server code in-process.
COPY crm_mock/server.py crm_mock/calendar.json ./crm_mock/
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# Runtime image (default target). Recordings and manifests (./data) are mounted read-only at runtime.
FROM base AS app
RUN useradd --system --no-create-home appuser
USER appuser
