# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Optional internal PyPI mirror (Nexus/Artifactory/devpi) for builds without internet access.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --index-url "${PIP_INDEX_URL}" ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} -r requirements.txt

COPY timelog ./timelog

# Build information shown in the footer. BUILD_TIMESTAMP defaults to the time of this layer's build.
ARG BUILD_TIMESTAMP=""
ARG GIT_COMMIT=""
RUN echo "${BUILD_TIMESTAMP:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" > timelog/BUILD_TIMESTAMP \
 && echo "${GIT_COMMIT}" > timelog/BUILD_COMMIT

RUN useradd --no-create-home --uid 10001 --home-dir /app timelog \
 && mkdir /backups && chown timelog /backups && chmod 700 /backups
USER timelog

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# --proxy-headers: trust X-Forwarded-* from the nginx reverse proxy (FORWARDED_ALLOW_IPS).
CMD ["sh", "-c", "exec uvicorn --factory timelog.main:create_app --host 0.0.0.0 --port 8000 --workers ${WEB_WORKERS:-1} --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\""]
