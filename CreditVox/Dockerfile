FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=prod

WORKDIR /app

# Install deps first for better layer caching. Include the Postgres driver,
# since production runs against a managed Postgres (Neon), not SQLite.
COPY requirements.txt requirements-postgres.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-postgres.txt

# App code.
COPY creditvox/ ./creditvox/
COPY web/ ./web/

# Non-root user.
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

# Bind to the platform-provided $PORT (Render/Railway inject it); default 8000
# for plain `docker run`. Single worker is correct until you outgrow it — with
# Postgres you can then raise --workers.
CMD ["sh", "-c", "uvicorn creditvox.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
