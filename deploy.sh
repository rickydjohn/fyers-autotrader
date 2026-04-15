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

# ── 1. Push local commits to GitHub ──────────────────────────────────────────
echo "==> Pushing to origin..."
git push

# ── 2. Pull on remote and rebuild ────────────────────────────────────────────
echo "==> Deploying to ${REMOTE_HOST} (service: ${SERVICE})..."

ssh "${REMOTE_USER}@${REMOTE_HOST}" "sudo bash -s" <<EOF
set -euo pipefail
cd "${REMOTE_PROJECT}"

echo "--- git fetch + reset to origin/master ---"
git fetch origin
git reset --hard origin/master

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
