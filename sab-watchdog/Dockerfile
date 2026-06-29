# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG=/config/config.yml \
    STATE_PATH=/state/state.json \
    LOG_LEVEL=INFO

RUN apt-get update && apt-get install -y --no-install-recommends \
        tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sab_watchdog.py .

VOLUME ["/config", "/state"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/sab_watchdog.py"]
