# Multi-arch index digest (amd64/arm64) of python:3.12-slim; update deliberately.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --uid 10001 --create-home bot
COPY src ./src
COPY scripts ./scripts
USER 10001
# The poll loop touches this file; a hung loop turns the container unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os, sys, time; sys.exit(time.time() - os.path.getmtime('/tmp/bot-heartbeat') > 120)"]
CMD ["python", "src/bot.py"]
