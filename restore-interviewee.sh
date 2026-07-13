#!/usr/bin/env bash
# Reset the interviewee service to a clean baseline between candidates.
# Restores source from ~/ccm-pristine, wipes the candidate's DB + projects, restarts.
# Does NOT touch: .env (tokens), .venv, node_modules, Claude auth, the interviewer service.
set -euo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

echo "[restore] stopping ccm-interviewee ..."
systemctl --user stop ccm-interviewee || true

echo "[restore] resetting source from ~/ccm-pristine ..."
rsync -a --delete \
  --exclude '.env' --exclude '.venv' --exclude 'frontend/node_modules' \
  --exclude '*.db' --exclude '*.db-shm' --exclude '*.db-wal' \
  ~/ccm-pristine/ ~/ccm-interviewee/

echo "[restore] wiping candidate data (db + workspace) ..."
rm -f ~/ccm-interviewee/ccm_interviewee.db ~/ccm-interviewee/ccm_interviewee.db-shm ~/ccm-interviewee/ccm_interviewee.db-wal
rm -rf ~/ccm-interviewee-projects/* 2>/dev/null || true

echo "[restore] uv sync + db migrate ..."
cd ~/ccm-interviewee
~/.local/bin/uv sync -q 2>/dev/null || true
~/.local/bin/uv run alembic upgrade head >/dev/null 2>&1 || true

echo "[restore] starting ccm-interviewee ..."
systemctl --user start ccm-interviewee

for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8002/api/system/health 2>/dev/null || echo 000)
  [ "$code" = "200" ] && { echo "[restore] DONE — interviewee healthy (HTTP 200)"; exit 0; }
  sleep 2
done
echo "[restore] WARNING: interviewee did not report healthy in time; check 'systemctl --user status ccm-interviewee'"
exit 1
