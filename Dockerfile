FROM python:3.13.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBUG=0 \
    DATA_DIR=/data \
    BACKUP_DIR=/backups

WORKDIR /app

# Runs as uid 1000 so the backups folder on the host stays owned by the normal user.
RUN useradd --uid 1000 --create-home app \
    && mkdir /data /backups \
    && chown app:app /data /backups

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# collectstatic loads settings, which need a non-default key when DEBUG is off. This key is never used to serve.
RUN SECRET_KEY=collectstatic-build-step-only DATA_DIR=/tmp/build-data python manage.py collectstatic --noinput

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/login/', timeout=4)"

# Apply any new migrations, then serve. SQLite handles a home's traffic easily with a couple of workers.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn homeapp.wsgi --bind 0.0.0.0:8000 --workers 2 --threads 4 --access-logfile - --forwarded-allow-ips='*'"]
