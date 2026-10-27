FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --include=dev
COPY frontend ./frontend
COPY scripts/build_editor.mjs ./scripts/build_editor.mjs
RUN npm run build:editor

FROM docker:29-cli AS dockercli
FROM python:3.12-slim-bookworm AS web
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 10001 contest && useradd -u 10001 -g contest -M contest && mkdir /data /app/staticfiles && chown contest:contest /data /app/staticfiles
COPY --chown=contest:contest . .
COPY --from=frontend --chown=contest:contest /build/static/editor ./static/editor
USER contest
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind=0.0.0.0:8000", "--workers=2", "--threads=4", "--timeout=60", "--control-socket=/tmp/gunicorn.ctl", "--access-logfile=-", "--error-logfile=-"]

FROM web AS worker
USER root
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
USER contest
CMD ["python", "manage.py", "judge_worker"]
