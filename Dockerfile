FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5055 \
    DATA_DIR=/data \
    CACHE_DIR=/data/cache \
    AUTOMATION=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py mail.py threads.py store.py sender.py automation.py ./
COPY static/ static/

# Run as an unprivileged user; cache and database live on a volume.
RUN useradd --create-home --uid 1000 follower \
    && mkdir -p /data/cache \
    && chown -R follower:follower /data
USER follower
VOLUME ["/data"]

EXPOSE 5055

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/status', timeout=4)"

# One worker only: it owns the single IMAP connection and the scheduler thread.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:${PORT} app:app"]
