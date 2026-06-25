#!/bin/bash
# CCM 新机器一键部署脚本
# 用法：git clone 后执行 ./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== CCM 环境初始化 ==="

# ── 1. 系统依赖（Xvfb 虚拟显示 + xdotool 模拟点击，CDP 登录需要）──
echo "[1/5] 安装系统依赖..."
PACKAGES=(xvfb xdotool)
MISSING=()
for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        MISSING+=("$pkg")
    fi
done
if [ ${#MISSING[@]} -gt 0 ]; then
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING[@]}"
fi

# ── 2. Google Chrome（CDP 登录使用系统 Chrome）─────────────────────
echo "[2/5] 安装 Google Chrome..."
if ! command -v google-chrome &>/dev/null; then
    wget -q -O /tmp/google-chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo dpkg -i /tmp/google-chrome.deb 2>/dev/null || sudo apt-get install -f -y
    rm -f /tmp/google-chrome.deb
fi
echo "  Chrome: $(google-chrome --version)"

# ── 3. Node.js + Claude CLI ────────────────────────────────────────
echo "[3/5] 安装 Node.js 和 Claude CLI..."
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi
if ! command -v claude &>/dev/null; then
    sudo npm install -g @anthropic-ai/claude-code
fi
echo "  Node: $(node --version)  Claude CLI: $(claude --version 2>&1 | head -1)"

# ── 4. Python 后端依赖 ─────────────────────────────────────────────
echo "[4/5] 安装 Python 依赖..."
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
uv sync

# ── 5. 前端构建 ────────────────────────────────────────────────────
echo "[5/5] 构建前端..."
cd frontend
npm install --no-fund --no-audit
npm run build
cd ..

echo ""
echo "=== 初始化完成 ==="
echo "创建 .env 文件后启动服务："
echo "  uv run uvicorn backend.main:app --host 127.0.0.1 --port 8002"
