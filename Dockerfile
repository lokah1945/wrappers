# syntax=docker/dockerfile:1.6
# ── Enterprise multi-wrapper Docker image ──────────────────────────────
# Builds a single image containing all 4 wrappers + model-registry.
# Run individual wrappers via wrapper-specific entrypoints.
#
# Usage:
#   docker build -t wrappers .
#   docker run -p 9101:9101 wrappers nvidia-python
#   docker run -p 9102:9102 wrappers nous
#   docker run -p 9103:9103 wrappers opencode
#   docker run -p 9104:9104 wrappers blackbox
#   docker run -p 9200:9200 wrappers model-registry

FROM python:3.13-slim AS base

LABEL maintainer="wrappers monorepo"
LABEL description="Production-grade API wrappers for NVIDIA NIM, Nous, OpenCode Zen, and Blackbox AI"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WRAPPER_SKIP_DOTENV=true

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY nvidia-python/requirements.txt /tmp/req-nvidia.txt
COPY nous/requirements.txt /tmp/req-nous.txt
COPY opencode/requirements.txt /tmp/req-opencode.txt
COPY blackbox/requirements.txt /tmp/req-blackbox.txt
COPY model-registry/requirements.txt /tmp/req-registry.txt

# Install all dependencies
RUN pip install --no-cache-dir \
    -r /tmp/req-nvidia.txt \
    -r /tmp/req-nous.txt \
    -r /tmp/req-opencode.txt \
    -r /tmp/req-blackbox.txt \
    -r /tmp/req-registry.txt

# Copy application code
COPY common/ /app/common/
COPY nvidia-python/ /app/nvidia-python/
COPY nous/ /app/nous/
COPY opencode/ /app/opencode/
COPY blackbox/ /app/blackbox/
COPY model-registry/ /app/model-registry/

# Health check endpoint (checks all services)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://127.0.0.1:${LISTEN_PORT:-9101}/health || exit 1

# Default to nvidia-python wrapper
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["--help"]

# Wrapper-specific run commands:
# docker run -p 9101:9101 -e NVIDIA_API_KEY=xxx wrappers-nvidia nvidia-python:src.main:app --host 0.0.0.0 --port 9101
# docker run -p 9102:9102 -e NOUS_API_KEY=xxx wrappers-nous wrapper_nous:app --host 0.0.0.0 --port 9102
# docker run -p 9103:9103 -e OPENCODE_API_KEY=xxx wrappers-opencode src.main:app --host 0.0.0.0 --port 9103
# docker run -p 9104:9104 -e BLACKBOX_API_KEY=xxx wrappers-blackbox src.main:app --host 0.0.0.0 --port 9104
# docker run -p 9200:9200 wrappers-registry service:app --host 0.0.0.0 --port 9200
