#!/bin/bash
# Stop service → run migration (or rollback) → start service.
# Must be launched OUTSIDE the service's cgroup (systemd-run) — otherwise the
# `systemctl stop` below kills this script too and the service never restarts.
# SERVICE_NAME="-" means a bare-uvicorn deployment: stop = kill $SERVER_PID,
# start = respawn uvicorn (hardcoded args, custom flags are not preserved).
set -uo pipefail

PROJECT_DIR="$1"
OLD_COMMIT="$2"
BACKUP_FILE="$3"
PORT="$4"
DB_FILE="${5:-claude_manager.db}"
SERVICE_NAME="${6:-ccm.service}"
MODE="${7:-migrate}"     # migrate | rollback
SERVER_PID="${8:-}"      # only used when SERVICE_NAME="-"
PYTHON_BIN="${9:-python3}"
STATUS_FILE="/tmp/ccm-update-status-${PORT}.json"
LOG_FILE="/tmp/ccm-update-migrate-${PORT}.log"

# ---- self-escape trampoline -------------------------------------------------
# Old (pre-systemd-run) backends spawn this script as a plain uvicorn child, so
# it starts INSIDE the service's cgroup and `systemctl stop` below would kill it
# mid-stop (KillMode=control-group ignores setsid/nohup), leaving the service
# permanently down with the status file frozen at "stopping". The script itself
# is freshly pulled during the update, so fixing it HERE reaches those old
# deployments: detect the situation and re-exec into a transient unit first.
# Backends that already launch us via systemd-run don't match and skip this.
SELF="$(readlink -f "$0")"
if [ -z "${CCM_ESCAPED:-}" ] && [ "$SERVICE_NAME" != "-" ] \
    && command -v systemd-run >/dev/null 2>&1; then
    case "$(cat /proc/self/cgroup 2>/dev/null)" in
        *"/${SERVICE_NAME}"*)
            # transient units inherit neither cwd nor env — pin both, and pass
            # $SELF (absolute) because a relative $0 breaks after the re-exec
            if systemd-run --user --collect --unit="ccm-update-escape-${PORT}-$$" \
                --working-directory="$PROJECT_DIR" \
                --setenv=CCM_ESCAPED=1 \
                --setenv=PATH="$PATH" \
                --property=StandardOutput="append:${LOG_FILE}" \
                --property=StandardError=inherit \
                /bin/bash "$SELF" "$@"; then
                exit 0  # escaped copy owns the update from here
            fi
            echo "cgroup escape failed at $(date -Iseconds) — continuing in-place (may die at svc_stop)" >> "$LOG_FILE"
            ;;
    esac
fi

# Everything below (git/uv/alembic/relative DB paths) assumes the project dir;
# abort outright if it is gone rather than running them somewhere random.
cd "$PROJECT_DIR" || exit 1

write_status() {
    local status="$1" message="$2" step="${3:-}"
    cat > "$STATUS_FILE" <<EOJSON
{
  "status": "$status",
  "message": "$message",
  "step": "$step",
  "old_commit": "$OLD_COMMIT",
  "backup_file": "$BACKUP_FILE",
  "port": $PORT,
  "timestamp": "$(date -Iseconds)"
}
EOJSON
}

svc_stop() {
    if [ "$SERVICE_NAME" != "-" ]; then
        systemctl --user stop "$SERVICE_NAME"
    else
        [ -n "$SERVER_PID" ] || return 1
        kill "$SERVER_PID" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$SERVER_PID" 2>/dev/null || return 0
            sleep 0.5
        done
        kill -9 "$SERVER_PID" 2>/dev/null || true
    fi
}

STARTED=0
svc_start() {
    # guard: the EXIT trap re-runs this after the normal-path start; a second
    # bare-uvicorn spawn would just lose the port race, so start only once
    [ "$STARTED" = 1 ] && return 0
    STARTED=1
    if [ "$SERVICE_NAME" != "-" ]; then
        systemctl --user start "$SERVICE_NAME"
    else
        cd "$PROJECT_DIR" && nohup "$PYTHON_BIN" -m uvicorn backend.main:app \
            --host 0.0.0.0 --port "$PORT" >> "/tmp/ccm-restart-${PORT}.log" 2>&1 &
    fi
}

echo "=== update_migrate.sh started at $(date -Iseconds) (mode=$MODE) ===" > "$LOG_FILE"
echo "cgroup: $(cat /proc/self/cgroup 2>/dev/null) (escaped=${CCM_ESCAPED:-0})" >> "$LOG_FILE"

# 1. Stop service
write_status "stopping" "正在停止服务..." "stop_service"
if ! svc_stop; then
    write_status "failed" "停止服务失败，中止" "stop_service"
    exit 1
fi
# From here on the service is down: whatever happens to this script (crash,
# SIGTERM, timeout), always bring the service back up. Start is idempotent,
# and on boot init_db() runs `alembic upgrade head` anyway.
trap 'svc_start || true' EXIT
sleep 1

if [ "$MODE" = "rollback" ]; then
    # Rollback: restore DB backup + reset code, service is already down so
    # touching the SQLite files is safe (no live connections).
    write_status "rolling_back" "正在回滚..." "rollback"
    echo "=== Rollback mode: restoring $BACKUP_FILE, reset to $OLD_COMMIT ===" >> "$LOG_FILE"
    if [ -n "$BACKUP_FILE" ] && [ -f "$BACKUP_FILE" ]; then
        rm -f "${DB_FILE}-wal" "${DB_FILE}-shm"
        cp "$BACKUP_FILE" "$DB_FILE"
    fi
    git reset --hard "$OLD_COMMIT" >> "$LOG_FILE" 2>&1
    uv sync >> "$LOG_FILE" 2>&1 || true
    svc_start
    write_status "rolled_back" "已回滚到 $OLD_COMMIT" "rollback"
    echo "=== update_migrate.sh (rollback) finished at $(date -Iseconds) ===" >> "$LOG_FILE"
    exit 0
fi

# 2. Run migration with timeout
write_status "migrating" "正在执行数据库迁移..." "alembic_upgrade"
if timeout 120 uv run alembic upgrade head >> "$LOG_FILE" 2>&1; then
    write_status "starting" "迁移成功，正在启动服务..." "start_service"
    svc_start
    write_status "completed" "更新完成" "start_service"
else
    EXIT_CODE=$?
    write_status "rolling_back" "迁移失败(exit=$EXIT_CODE)，正在回滚..." "alembic_upgrade"
    echo "=== Migration failed (exit=$EXIT_CODE), rolling back ===" >> "$LOG_FILE"

    # Restore DB backup — must remove WAL/SHM residuals first
    rm -f "${DB_FILE}-wal" "${DB_FILE}-shm"
    cp "$BACKUP_FILE" "$DB_FILE"

    git reset --hard "$OLD_COMMIT"
    uv sync >> "$LOG_FILE" 2>&1 || true

    svc_start
    write_status "rolled_back" "迁移失败，已回滚到 $OLD_COMMIT" "alembic_upgrade"
fi

echo "=== update_migrate.sh finished at $(date -Iseconds) ===" >> "$LOG_FILE"
