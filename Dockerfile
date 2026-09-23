FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY ct_agent ./ct_agent
RUN pip install --no-cache-dir . && mkdir -p /app/data

# Persistent data lives in /app/data. On Railway, attach a volume there (Railway
# rejects the Dockerfile VOLUME keyword); with docker compose it's a named volume.
# Runs as root because Railway mounts volumes as root.
EXPOSE 8000
# Every worker plus the API, each in its own supervised process.
CMD ["ctagent", "run-all"]
