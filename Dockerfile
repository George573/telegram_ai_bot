FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home bot
COPY src ./src
COPY scripts ./scripts
USER 10001
CMD ["python", "src/bot.py"]
