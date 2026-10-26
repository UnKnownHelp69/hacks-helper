FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/bot.db \
    LOG_FILE=/app/logs/bot.log

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY main.py .
COPY scripts ./scripts

# Run as an unprivileged user; data and logs live in volumes.
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /app/data /app/logs \
    && chown -R botuser:botuser /app/data /app/logs
USER botuser

VOLUME ["/app/data", "/app/logs"]

CMD ["python", "main.py"]
