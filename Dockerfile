# Axon MCP Server — Code Intelligence Knowledge Graph
# Serves MCP tools + Web UI for AI agents and developers
# Indexes Go/Python/TS services into a structural knowledge graph
#
# Usage:
#   docker build -t cert-ix/axon-mcp .
#   docker run -d --name axon-mcp \
#     -v /apps/mvp_v1/go-services:/repo \
#     -p 127.0.0.1:8420:8420 \
#     --restart unless-stopped \
#     cert-ix/axon-mcp

# Stage 1: Build frontend assets
FROM node:20-slim AS frontend-builder
WORKDIR /build/frontend
COPY src/axon/web/frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY src/axon/web/frontend/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12-slim

# Install system dependencies for tree-sitter native extensions, kuzu, and health check
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files and install Python dependencies first (better layer caching)
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Copy pre-built frontend from stage 1
COPY --from=frontend-builder /build/frontend/dist ./src/axon/web/frontend/dist

# Install axon and all dependencies
RUN pip install --no-cache-dir -e .

# Copy entrypoint script
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Health check — verify MCP endpoint is responding
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -sf http://localhost:8420/api/host || exit 1

# Expose MCP + UI port
EXPOSE 8420

# Mount point: /repo — the source code repository to index
# The .axon index directory is written inside /repo/.axon
VOLUME ["/repo"]

ENTRYPOINT ["/docker-entrypoint.sh"]
