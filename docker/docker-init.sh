#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0.

set -e

/app/docker/docker-bootstrap.sh

STEP_CNT=4

echo_step() {
cat <<EOF

######################################################################

Init Step ${1}/${STEP_CNT} [${2}] -- ${3}

######################################################################

EOF
}

ADMIN_PASSWORD="${SUPERSET_ADMIN_PASSWORD:-admin}"

echo_step "1" "Starting" "Applying DB migrations"
superset db upgrade
echo_step "1" "Complete" "Applying DB migrations"

echo_step "2" "Starting" "Setting up admin user (admin / $ADMIN_PASSWORD)"
superset fab create-admin \
  --username admin \
  --firstname Superset \
  --lastname Admin \
  --email admin@superset.com \
  --password "$ADMIN_PASSWORD"
echo_step "2" "Complete" "Setting up admin user"

echo_step "3" "Starting" "Setting up roles and permissions"
superset init
echo_step "3" "Complete" "Setting up roles and permissions"

if [ "$SUPERSET_LOAD_EXAMPLES" = "yes" ]; then
  echo_step "4" "Starting" "Loading examples"
  superset load_examples --force
  echo_step "4" "Complete" "Loading examples"
fi
