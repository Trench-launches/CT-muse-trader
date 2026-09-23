FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY ct_agent ./ct_agent
RUN pip install --no-cache-dir . && useradd -m -u 10001 agent && mkdir -p /app/data && chown agent /app/data

USER agent
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["ctagent", "api", "--host", "0.0.0.0", "--port", "8000"]
