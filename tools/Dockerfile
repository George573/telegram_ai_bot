# Multi-arch index digest (amd64/arm64) of python:3.12-slim; update deliberately.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp XDG_CONFIG_HOME=/tmp/.config XDG_CACHE_HOME=/tmp/.cache
WORKDIR /app
COPY tools/requirements.lock .
RUN apt-get update && apt-get install -y --no-install-recommends chromium && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --uid 10002 --no-create-home tools
COPY src/yandex_reviews.py src/yandex_afisha.py tools/browser.py tools/review_browser.py tools/afisha_browser.py tools/server.py ./
COPY src/licenses ./licenses
USER 10002
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
CMD ["python", "server.py"]
