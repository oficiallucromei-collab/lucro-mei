FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.2,<27" \
    && python -m pip install --no-cache-dir .

RUN chown 10001:10001 /app
USER 10001:10001
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn lucromei.main:app --host 0.0.0.0 --port \"${PORT:-8080}\" --no-access-log"]

