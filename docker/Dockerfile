FROM python:3.15.2-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    PIPX_HOME=/opt/pipx \
    PIPX_BIN_DIR=/opt/bin \
    PATH=/opt/bin:/app/.venv/bin:$PATH
ARG POETRY_VERSION=2.2.1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pipx && pipx install poetry==${POETRY_VERSION}
WORKDIR /app

FROM base AS deps
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main --no-interaction

FROM deps AS dev
RUN apt-get update && apt-get install -y --no-install-recommends git make \
    && rm -rf /var/lib/apt/lists/*
RUN poetry install --no-root --no-interaction
COPY . .
ENV PYTHONPATH=/app/src

FROM node:24.14.1-slim AS miniapp
WORKDIR /miniapp
COPY miniapp/package.json miniapp/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY miniapp/ ./
RUN npm test && npm run build

FROM python:3.15.2-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
RUN groupadd --system envoy && useradd --system --gid envoy --home /app envoy
COPY --from=deps /app/.venv /app/.venv
COPY src ./src
COPY alembic.ini ./
COPY --from=miniapp /miniapp/dist ./miniapp/dist
USER envoy
