# Multi-stage build for Git Worktree MCP Server
# pygit2 requires libgit2 native library

# Builder stage
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git libgit2-dev pkg-config build-essential \
    && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
COPY src/ src/

RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# Runtime stage
FROM python:3.13-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    git libgit2-1.7 curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r -g 1000 app && \
    useradd -r -g app -u 1000 -m -d /app app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Create repo and workspaces directories
RUN mkdir -p /repo /workspaces && chown app:app /repo /workspaces

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_REPO_PATH=/repo \
    GIT_WORKSPACES_PATH=/workspaces

USER app

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "mcp_git_worktree.server:app", "--host", "0.0.0.0", "--port", "8000"]
