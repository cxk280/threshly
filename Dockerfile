# Threshly coordinator/worker image (mock engine + Postgres + S3 support).
# The vLLM GPU path is intentionally NOT installed here — build a CUDA image with
# `pip install "threshly[vllm]"` for GPU workers; this image runs the coordinator and
# CPU/mock workers used for the stack, CI, and demos.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install ".[postgres,s3]"

# Default to the coordinator; the worker service overrides the command in compose.
EXPOSE 8080
CMD ["threshly", "coordinator"]
