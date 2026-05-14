#!/usr/bin/env bash
# deploy.sh — push local commits to GitHub, then pull and rebuild on the remote.
#
# Usage:
#   ./deploy.sh                  # rebuild only core-engine (most common)
#   ./deploy.sh core-engine      # same as above, explicit
#   ./deploy.sh data-service     # rebuild a different service
#   ./deploy.sh all              # rebuild every service

set -euo pipefail

REMOTE_USER="overlord"
REMOTE_HOST="100.78.91.15"
REMOTE_PROJECT="/root/Project/src/github.com/rickydjohn/fyers-autotrader"
SERVICE="${1:-core-engine}"

# Deploy the currently-checked-out branch. Stay on master for normal deploys
# (rollback path); switch to a feature branch when shipping WIP work that should
# not land on master yet.
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# ── 1. Push local commits to GitHub ──────────────────────────────────────────
echo "==> Pushing branch ${BRANCH} to origin..."
git push -u origin "${BRANCH}"

# ── 2. Pull on remote and rebuild ────────────────────────────────────────────
echo "==> Deploying to ${REMOTE_HOST} (service: ${SERVICE}, branch: ${BRANCH})..."

ssh "${REMOTE_USER}@${REMOTE_HOST}" "sudo bash -s" <<EOF
set -euo pipefail
cd "${REMOTE_PROJECT}"

echo "--- git fetch + reset to origin/${BRANCH} ---"
git fetch origin
git reset --hard "origin/${BRANCH}"

echo "--- docker compose rebuild ---"
if [ "${SERVICE}" = "all" ]; then
  docker compose up -d --build
else
  docker compose up -d --no-deps --build "${SERVICE}"
fi

echo "--- container status ---"
docker ps --filter name=trading --format "table {{.Names}}\t{{.Status}}"
EOF

echo "==> Done."
