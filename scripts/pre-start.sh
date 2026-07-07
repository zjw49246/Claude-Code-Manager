#!/usr/bin/env bash
# systemd ExecStartPre：服务启动前自动同步依赖和数据库。
# 轻量设计：每步都先检测是否需要更新，无变化秒过。
set -uo pipefail
cd "$(dirname "$0")/.."

UV="${UV:-$HOME/.local/bin/uv}"
LOG_PREFIX="[pre-start]"

echo "$LOG_PREFIX 检查依赖..."

# 1. Python 依赖（uv sync —— 仅 uv.lock 与 venv 不一致时才安装）
"$UV" sync --quiet 2>&1 || echo "$LOG_PREFIX uv sync 失败（非致命，继续）"

# 2. claude-pty git 依赖（安装时快照，不随 git pull 更新）
if [ -x scripts/refresh_pty.sh ]; then
    scripts/refresh_pty.sh 2>&1 || echo "$LOG_PREFIX refresh_pty 失败（非致命，继续）"
fi

# 3. 数据库迁移（init_db 启动时也会跑，这里提前跑避免启动报错）
"$UV" run alembic upgrade head 2>&1 || echo "$LOG_PREFIX alembic upgrade 失败（非致命，继续）"

echo "$LOG_PREFIX 完成"
