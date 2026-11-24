#!/bin/sh
set -e

# 多线程 worker：WAL 模式下重算写事务不会阻塞下载读请求。
exec gunicorn \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --access-logfile - \
  --error-logfile - \
  "wsgi:app"
