#!/usr/bin/env bash
# =============================================================================
# start_bot.sh - Standalone Launcher for ReelsBot
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "================================================================="
echo "   🚀 Starting ReelsBot (TikTok to Instagram Reel Generator)     "
echo "================================================================="

# 1. Check Virtual Environment
if [ ! -d ".venv" ]; then
    echo "[+] Creating virtual environment (.venv)..."
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -r requirements.txt
fi

# 2. Check Configuration File (.env)
if [ ! -f ".env" ]; then
    echo "[!] .env file not found. Copying from .env.example..."
    cp .env.example .env
    echo "[!] Created .env. Please edit it with your TELEGRAM_BOT_TOKEN before running."
    exit 1
fi

# 3. Quick Token Check
if grep -q "TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here" .env || grep -q "^TELEGRAM_BOT_TOKEN=$" .env; then
    echo "[!] Warning: TELEGRAM_BOT_TOKEN is not set in .env."
    echo "[!] Please open .env and add your Telegram bot token from @BotFather."
    exit 1
fi

echo "[+] Virtual environment: OK"
echo "[+] Configuration: OK"
echo "[+] Launching reels_bot.py..."
echo "================================================================="

exec ./.venv/bin/python3 reels_bot.py "$@"
