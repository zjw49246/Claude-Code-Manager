#!/bin/bash
set -euo pipefail

DEV_DIR="/home/ubuntu/Claude-Code-Manager-dev"
DEV_PORT=8003

# Safety: abort if production CCM is using our port
if ss -tlnp 2>/dev/null | grep -q ":${DEV_PORT} "; then
    echo "ERROR: port ${DEV_PORT} is already in use — aborting" >&2
    exit 1
fi

cd "$DEV_DIR"

# Load .env as the single source of truth (pydantic also reads it)
set -a
source .env
set +a

exec .venv/bin/python3 -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"
