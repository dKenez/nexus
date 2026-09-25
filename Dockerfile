# syntax=docker/dockerfile:1
FROM docker.io/library/python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so source changes don't invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM docker.io/library/python:3.13-slim

# Fixed uid: in edda the backups PV is a hostPath directory pre-owned by this uid.
ARG UID=2017
RUN groupadd --gid "${UID}" nexus \
    && useradd --uid "${UID}" --gid "${UID}" --no-create-home --home-dir /app \
       --shell /usr/sbin/nologin nexus

WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root recipes ./recipes

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    RECIPES_DIR=/app/recipes \
    BACKUP_DIR=/backups

USER ${UID}
EXPOSE 8080
CMD ["python", "-m", "nexus"]
