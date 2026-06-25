#!/bin/bash
# Stop service → run migration → start service (or rollback on failure).
# Launched via nohup so it survives the service being killed by systemctl stop.
set -uo pipefail

PROJECT_DIR="$1"
OLD_COMMIT="$2"
BACKUP_FILE="$3"
PORT="$4"
DB_FILE="${5:-claude_manager.db}"
SERVICE_NAME="ccm.service"
STATUS_FILE="/tmp/ccm-update-status-${PORT}.json"
LOG_FILE="/tmp/ccm-update-migrate-${PORT}.log"

cd "$PROJECT_DIR"

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

echo "=== update_migrate.sh started at $(date -Iseconds) ===" > "$LOG_FILE"

# 1. Stop service
write_status "stopping" "正在停止服务..." "stop_service"
if ! systemctl --user stop "$SERVICE_NAME"; then
    write_status "failed" "停止服务失败，中止迁移" "stop_service"
    exit 1
fi
sleep 1

# 2. Run migration with timeout
write_status "migrating" "正在执行数据库迁移..." "alembic_upgrade"
if timeout 120 uv run alembic upgrade head >> "$LOG_FILE" 2>&1; then
    write_status "starting" "迁移成功，正在启动服务..." "start_service"
    systemctl --user start "$SERVICE_NAME"
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

    systemctl --user start "$SERVICE_NAME"
    write_status "rolled_back" "迁移失败，已回滚到 $OLD_COMMIT" "alembic_upgrade"
fi

echo "=== update_migrate.sh finished at $(date -Iseconds) ===" >> "$LOG_FILE"
