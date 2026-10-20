FROM node:20-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY backend/app/runtime/ui_catalog.json /src/backend/app/runtime/ui_catalog.json
# The diagram-worker test asserts Node's self descriptor, which differs in this image.
RUN npm run ui:check && ./node_modules/.bin/tsc && ./node_modules/.bin/vite build && node scripts/check-bundle.mjs

FROM python:3.12-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock /app/backend/
WORKDIR /app/backend
RUN uv sync --frozen --no-dev

COPY backend/ /app/backend
COPY --from=frontend /src/frontend/dist /app/frontend/dist
COPY deploy/start.sh /start.sh
RUN chmod +x /start.sh

ENV PATH="/app/backend/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
WORKDIR /app/backend
CMD ["/start.sh"]
