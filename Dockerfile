FROM node:20-slim AS frontend-builder

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS app

ENV PORT=7860 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    TG_SESSION_MODE=string

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential tzdata gosu && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY tg_signer/__init__.py ./tg_signer/__init__.py
RUN pip install --no-cache-dir "pydantic<2" "fastapi==0.109.2" "bcrypt==4.0.1"

COPY . /app
RUN pip install --no-cache-dir . uvicorn[standard] sqlalchemy "passlib[bcrypt]==1.7.4" "python-jose[cryptography]" pyotp qrcode[pil] apscheduler python-multipart psycopg2-binary

ARG TARGETPLATFORM
RUN if [ "${TARGETPLATFORM:-}" = "linux/amd64" ] || [ "$(uname -m)" = "x86_64" ]; then \
      pip install --no-cache-dir tgcrypto; \
    else \
      echo "Skipping tgcrypto on ${TARGETPLATFORM:-unknown}"; \
    fi

RUN mkdir -p /web
COPY --from=frontend-builder /frontend/out /web

RUN mkdir -p /data

ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd -r -g ${APP_GID} app && \
    useradd -r -u ${APP_UID} -g app -d /app -s /usr/sbin/nologin app && \
    chown -R app:app /data

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.getenv(\"PORT\", \"7860\")}/healthz').read()"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
