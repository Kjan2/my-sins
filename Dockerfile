FROM docker:29-cli AS dockercli
FROM python:3.12-slim-bookworm AS web
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 10001 contest && useradd -u 10001 -g contest -M contest && mkdir /data /app/staticfiles && chown contest:contest /data /app/staticfiles
COPY --chown=contest:contest . .
USER contest
CMD ["gunicorn", "config.wsgi:application", "--bind=0.0.0.0:8000", "--workers=2", "--threads=4", "--timeout=60", "--access-logfile=-", "--error-logfile=-"]

FROM web AS worker
USER root
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
USER contest
CMD ["python", "manage.py", "judge_worker"]
