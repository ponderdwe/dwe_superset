#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0.

set -eo pipefail

REQUIREMENTS_LOCAL="/app/docker/requirements-local.txt"

if [ -f "${REQUIREMENTS_LOCAL}" ]; then
  echo "Installing local overrides at ${REQUIREMENTS_LOCAL}"
  apt-get update -qq && apt-get install -y -qq --no-install-recommends git && rm -rf /var/lib/apt/lists/*
  uv pip install --python /app/.venv --override /app/docker/requirements-overrides.txt -r "${REQUIREMENTS_LOCAL}"
else
  echo "Skipping local overrides"
fi

case "${1}" in
  worker)
    echo "Starting Celery worker..."
    celery --app=superset.tasks.celery_app:app worker -O fair -l INFO
    ;;
  beat)
    echo "Starting Celery beat..."
    rm -f /tmp/celerybeat.pid
    celery --app=superset.tasks.celery_app:app beat --pidfile /tmp/celerybeat.pid -l INFO -s "${SUPERSET_HOME}"/celerybeat-schedule
    ;;
  app)
    echo "Starting web app (development server)..."
    flask run -p 8088 --with-threads --reload --debugger --host=0.0.0.0
    ;;
  app-gunicorn)
    echo "Starting web app (gunicorn)..."
    /app/docker/run-server.sh
    ;;
  "")
    # Called without a command (e.g. from docker-init.sh to run requirements install only)
    ;;
  *)
    echo "Unknown command: ${1}"
    exit 1
    ;;
esac
