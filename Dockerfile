# npm run build 会先 vite build 生成 dist/，再由 scripts/sync-to-backend.js
# 同步到 ../app/static/frontend/，供后端 FastAPI 作为静态资源直接托管。
# base 镜像走 Docker daemon registry-mirror（见 ~/.docker/daemon.json），无需在 FROM 写前缀
FROM node:22-alpine AS frontend-builder
WORKDIR /aether

# 国内 npm 镜像（容器内访问 registry.npmjs.org 易超时）
RUN npm config set registry https://registry.npmmirror.com

# 先单独拷依赖文件，利用 Docker 层缓存（源码改动不会重装 node_modules）
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci

# 拷源码并构建
COPY frontend/ ./frontend/
RUN cd frontend && npm run build


# ===== Stage 2: Python 运行时 =====
FROM python:3.11-slim AS runtime

# OpenCV 运行时依赖：opencv-python 需要 libGL / libglib，slim 镜像默认没有
# （本项目用 RTSP 网络流，无需 GUI；若改用 opencv-python-headless 可省去 libgl1）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /aether

# 先装 Python 依赖（利用层缓存）
# 国内 PyPI 镜像 + 超时容忍（容器内访问 files.pythonhosted.org 易超时）
COPY requirements.txt .
RUN pip install --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        --trusted-host pypi.tuna.tsinghua.edu.cn \
        --timeout 120 \
        -r requirements.txt

# 拷贝后端代码
COPY app/ ./app/

# 拷贝文档（RAG 服务读取 docs/ 做知识库索引）
COPY docs/ ./docs/

# 拷贝 Stage 1 构建好的前端产物
COPY --from=frontend-builder /aether/app/static/frontend ./app/static/frontend

# 默认配置：镜像内置 config.example.json 作为 config.json，
# 实际部署时用 volume 挂载自己的 config.json 覆盖。
COPY config.example.json ./config.json

# 容器内加载进度端口需对宿主可见（宿主本机默认 127.0.0.1）
ENV STARTUP_PROGRESS_HOST=0.0.0.0 \
    TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8010 8011

# 与 run_app.bat 一致的启动命令（纯 Docker 模式）
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
