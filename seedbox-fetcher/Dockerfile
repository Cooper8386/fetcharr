# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS base

ARG RCLONE_VERSION=v1.66.0
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=99 \
    PGID=100 \
    CONFIG=/config/config.yml \
    LOG_LEVEL=INFO

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip tini gosu \
    && rm -rf /var/lib/apt/lists/*

# Install rclone for the actual copy operations.
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
      amd64) rcarch="amd64" ;; \
      arm64) rcarch="arm64" ;; \
      *) echo "unsupported arch: $arch"; exit 1 ;; \
    esac; \
    curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-${rcarch}.zip" -o /tmp/rclone.zip; \
    unzip /tmp/rclone.zip -d /tmp; \
    install -m 0755 "/tmp/rclone-${RCLONE_VERSION}-linux-${rcarch}/rclone" /usr/local/bin/rclone; \
    rm -rf /tmp/rclone*

WORKDIR /opt/fetcharr
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/state", "/data"]

EXPOSE 8765

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
