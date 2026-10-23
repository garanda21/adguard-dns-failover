FROM python:3.12-slim

ARG APP_VERSION=dev
ARG GIT_COMMIT=unknown

LABEL org.opencontainers.image.title="dns-blockswitch" \
      org.opencontainers.image.description="Switch AdGuard Home DNS during IP blocks" \
      org.opencontainers.image.source="https://github.com/garanda21/dns-blockswitch" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Madrid \
    DATA_DIR=/data \
    APP_VERSION=${APP_VERSION}

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home app \
    && mkdir /data && chown app:app /data

WORKDIR /opt/dns-blockswitch
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

USER app
VOLUME /data
ENTRYPOINT ["python", "-m", "app"]
CMD ["daemon"]
