# Todo v2. Build context is v2/ (docker build -f v2/Dockerfile v2). Publishes as todo:v2.
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
# v2's own names and data file, so a v2 container can't open v1's database by accident.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TODO_V2_DB=/data/todo-v2.db \
    TODO_V2_PORT=7680 \
    TODO_V2_FRONTEND_DIST=/app/frontend/dist
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
COPY todo_v2/ todo_v2/
COPY --from=web /web/dist frontend/dist
VOLUME ["/data"]
EXPOSE 7680

# Commit the image was built from; reported by /health.
ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA}

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"TODO_V2_PORT\"]}/health', timeout=3)"
CMD ["python", "main.py"]
