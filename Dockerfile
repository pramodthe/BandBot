# Single image for the API and the worker; deploy/start.sh runs both.

FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# The build checks the UI catalogue against the backend's committed copy.
COPY backend/app/runtime/ui_catalog.json /src/backend/app/runtime/ui_catalog.json
RUN npm run build

# The full (not slim) Python image and the root user are kept on purpose: the
# process writes its database, checkpoints, and lock files next to a mounted
# persistent disk (/var/data on Render), whose ownership the platform controls.
FROM python:3.12-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /uvx /bin/

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev

COPY backend/ /app/backend
COPY --from=frontend /src/frontend/dist /app/frontend/dist
COPY deploy/start.sh /start.sh
RUN chmod +x /start.sh

ENV PATH="/app/backend/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '10000'), timeout=4)"]
CMD ["/start.sh"]
