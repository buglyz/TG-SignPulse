# 使用你提供的基础镜像（如果已经推送到容器仓库）或直接基于源码构建
FROM node:20-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS app
# 关键点：修改端口为 HF 要求的 7860
ENV PORT=7860 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    TG_SESSION_MODE=string

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential tzdata gosu && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir "pydantic<2" "fastapi==0.109.2" "bcrypt==4.0.1"

COPY . /app
RUN pip install --no-cache-dir . uvicorn[standard] sqlalchemy "passlib[bcrypt]==1.7.4" "python-jose[cryptography]" pyotp qrcode[pil] apscheduler python-multipart psycopg2-binary

# Frontend 静态文件
RUN mkdir -p /web
COPY --from=frontend-builder /frontend/out /web

# 修改权限以适应 HF 运行环境
RUN mkdir -p /data && chmod 777 /data

# 暴露 7860
EXPOSE 7860

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
