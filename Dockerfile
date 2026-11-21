FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.2,<27" \
    && python -m pip install --no-cache-dir .
# Shipped so the same image can run `alembic upgrade head` (the lucromei-migrate Cloud Run job).
COPY alembic.ini ./
COPY migrations ./migrations

RUN chown 10001:10001 /app
USER 10001:10001

# Cloud Run sets PORT (8080 by default) and checks the container listens on it.
EXPOSE 8080
# The app logs each request itself (JSON, without query strings, which can carry phone numbers).
CMD ["sh", "-c", "exec uvicorn lucromei.main:app --host 0.0.0.0 --port \"${PORT:-8080}\" --no-access-log"]
