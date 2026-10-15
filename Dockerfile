# Signet: one-command demo image.
#   docker compose up            -> http://localhost:8077 (offline demo mode)
# See README "Run with Docker" for the live mode (local Ollama + FISCO BCOS).

FROM node:22-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN mkdir -p ../backend/src/pca && npm run build

FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.23
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
# Dev group included: eth-tester provides the in-process EVM used when no chain is reachable.
RUN uv sync --frozen --no-install-project
COPY contracts/ /app/contracts/
COPY backend/src/ ./src/
COPY backend/tests/ ./tests/
COPY --from=web /src/backend/src/pca/static/ ./src/pca/static/
RUN uv sync --frozen \
 && uv run python -c "from pca.anchor import compiled; compiled()"  # fetch solc + compile the contract at build time

ENV PCA_DATA=/data PCA_LLM=scripted
VOLUME /data
EXPOSE 8077
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8077/api/status')"
CMD ["uv", "run", "--frozen", "--no-sync", "uvicorn", "pca.api:app", "--host", "0.0.0.0", "--port", "8077"]
