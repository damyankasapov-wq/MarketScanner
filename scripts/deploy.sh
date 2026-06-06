#!/usr/bin/env bash
# deploy.sh — deploy or update MarketScanner on OMV
#
# Usage (run from your dev machine):
#   ./scripts/deploy.sh              # deploy / update
#   ./scripts/deploy.sh --restart    # restart without pulling new code
#   ./scripts/deploy.sh --stop       # stop the running process
#   ./scripts/deploy.sh --status     # show whether the process is running
#
# Requirements on the dev machine:
#   - SSH access to OMV (key-based auth recommended)
#
# First-time setup on OMV (one manual step):
#   1. SSH in and create /opt/MarketScanner/.env from .env.example
#   2. Run this script — it handles everything else

set -euo pipefail

# ── Load .env from project root (if present on dev machine) ──────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"
if [ -f "$ENV_FILE" ]; then
    # Export only the deploy-relevant vars; never eval the whole file
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^(OVM_HOST|OVM_USER|DEPLOY_DIR)$ ]] && export "$key=${value//\"/}"
    done < <(grep -E '^(OVM_HOST|OVM_USER|DEPLOY_DIR)=' "$ENV_FILE")
fi

# ── Configuration ────────────────────────────────────────────────────────────
OVM_HOST="${OVM_HOST:-openmediavault.local}"
OVM_USER="${OVM_USER:-root}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/MarketScanner}"
REPO_URL="https://github.com/damyankasapov-wq/MarketScanner.git"
SCREEN_SESSION="marketscanner"
PYTHON="${DEPLOY_DIR}/.venv/bin/python"
# ─────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()    { echo -e "${YELLOW}[deploy]${NC} $*"; }
error()   { echo -e "${RED}[deploy]${NC} $*" >&2; }

CMD="${1:-deploy}"

ssh_run() {
    # Run a command on OMV, inheriting terminal so interactive prompts work
    ssh -t "${OVM_USER}@${OVM_HOST}" "$@"
}

ssh_quiet() {
    # Run a command on OMV, capture output
    ssh "${OVM_USER}@${OVM_HOST}" "$@"
}

# ── Subcommands ───────────────────────────────────────────────────────────────

cmd_status() {
    info "Checking process status on ${OVM_HOST}…"
    ssh_quiet "screen -list | grep -q '${SCREEN_SESSION}' \
        && echo 'RUNNING' || echo 'STOPPED'"
}

cmd_stop() {
    info "Stopping MarketScanner on ${OVM_HOST}…"
    ssh_quiet "screen -S ${SCREEN_SESSION} -X quit 2>/dev/null \
        && echo 'Stopped.' || echo 'Not running — nothing to stop.'"
}

cmd_restart() {
    info "Restarting MarketScanner on ${OVM_HOST} (no code update)…"
    ssh_run "
        set -e
        # Stop existing session if running
        screen -S ${SCREEN_SESSION} -X quit 2>/dev/null || true
        sleep 1

        # Verify env file exists
        if [ ! -f '${DEPLOY_DIR}/.env' ]; then
            echo 'ERROR: ${DEPLOY_DIR}/.env not found.'
            echo 'Copy .env.example to .env and fill in credentials.'
            exit 1
        fi

        # Start in a new detached screen session
        cd '${DEPLOY_DIR}'
        screen -dmS ${SCREEN_SESSION} \
            bash -c 'source .venv/bin/activate && python main.py 2>&1 | tee -a /var/log/marketscanner.log'

        sleep 2
        if screen -list | grep -q '${SCREEN_SESSION}'; then
            echo 'MarketScanner started. Session: ${SCREEN_SESSION}'
            echo \"Attach with: screen -r ${SCREEN_SESSION}\"
        else
            echo 'ERROR: process did not stay running. Check /var/log/marketscanner.log'
            exit 1
        fi
    "
}

cmd_deploy() {
    info "Deploying MarketScanner to ${OVM_HOST}:${DEPLOY_DIR}…"
    ssh_run "
        set -e

        # ── 1. Clone or pull ──────────────────────────────────────────────
        if [ -d '${DEPLOY_DIR}/.git' ]; then
            echo '--- Pulling latest code ---'
            cd '${DEPLOY_DIR}'
            git fetch origin
            git reset --hard origin/main
        else
            echo '--- Cloning repository ---'
            mkdir -p '$(dirname ${DEPLOY_DIR})'
            git clone '${REPO_URL}' '${DEPLOY_DIR}'
            cd '${DEPLOY_DIR}'
        fi

        # ── 2. Check .env ─────────────────────────────────────────────────
        if [ ! -f '${DEPLOY_DIR}/.env' ]; then
            echo ''
            echo 'NOTICE: .env not found. Creating from .env.example…'
            cp '${DEPLOY_DIR}/.env.example' '${DEPLOY_DIR}/.env'
            echo 'Edit ${DEPLOY_DIR}/.env and fill in credentials, then re-run deploy.'
            echo ''
        fi

        # ── 3. Python venv ────────────────────────────────────────────────
        cd '${DEPLOY_DIR}'
        if [ ! -f '.venv/bin/python' ]; then
            echo '--- Creating virtual environment ---'
            python3 -m venv .venv
        fi
        source .venv/bin/activate

        # ── 4. Install / update dependencies ─────────────────────────────
        echo '--- Installing dependencies ---'
        pip install --quiet --upgrade pip
        # TA-Lib C library (skip if already installed)
        if ! python -c 'import talib' 2>/dev/null; then
            if command -v apt-get >/dev/null 2>&1; then
                apt-get install -y --no-install-recommends ta-lib 2>/dev/null || true
            fi
        fi
        pip install --quiet -r requirements.txt

        # ── 5. Docker Compose (PostgreSQL) ────────────────────────────────
        echo '--- Ensuring PostgreSQL is running ---'
        docker compose up -d
        # Wait up to 15s for DB to be ready
        for i in \$(seq 1 15); do
            docker compose exec -T db pg_isready -U ms -d marketscanner -q && break
            sleep 1
        done
        docker compose exec -T db pg_isready -U ms -d marketscanner -q \
            || { echo 'ERROR: PostgreSQL did not become ready in time.'; exit 1; }

        # ── 6. Smoke-test DB connection ───────────────────────────────────
        echo '--- Verifying DB connection ---'
        python -c 'from marketscanner.state.store import init_db; init_db(); print(\"DB OK\")'

        # ── 7. Stop existing screen session ───────────────────────────────
        screen -S ${SCREEN_SESSION} -X quit 2>/dev/null || true
        sleep 1

        # ── 8. Start MarketScanner ────────────────────────────────────────
        mkdir -p /var/log
        screen -dmS ${SCREEN_SESSION} \
            bash -c 'cd ${DEPLOY_DIR} && source .venv/bin/activate && python main.py 2>&1 | tee -a /var/log/marketscanner.log'

        sleep 2
        if screen -list | grep -q '${SCREEN_SESSION}'; then
            echo ''
            echo '✓ MarketScanner is running.'
            echo \"  Attach : screen -r ${SCREEN_SESSION}\"
            echo \"  Logs   : tail -f /var/log/marketscanner.log\"
            echo \"  Stop   : ./scripts/deploy.sh --stop\"
        else
            echo 'ERROR: process did not stay running. Check /var/log/marketscanner.log'
            tail -20 /var/log/marketscanner.log 2>/dev/null || true
            exit 1
        fi
    "
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "$CMD" in
    deploy|"")      cmd_deploy  ;;
    --restart)      cmd_restart ;;
    --stop)         cmd_stop    ;;
    --status)       cmd_status  ;;
    *)
        echo "Usage: $0 [deploy|--restart|--stop|--status]"
        exit 1
        ;;
esac
