#!/usr/bin/env bash
# deploy.sh — deploy or update MarketScanner on OMV
#
# Usage (run from your dev machine):
#   ./scripts/deploy.sh              # build + deploy / update full stack
#   ./scripts/deploy.sh --restart    # restart containers without pulling new code
#   ./scripts/deploy.sh --stop       # stop all containers
#   ./scripts/deploy.sh --status     # show container status
#   ./scripts/deploy.sh --logs       # tail scanner logs
#
# Requirements on the dev machine:
#   - SSH access to OMV (key-based auth recommended)
#
# First-time setup on OMV (one manual step):
#   Copy .env.example to /opt/MarketScanner/.env and fill in credentials.
#   Then run this script — it handles everything else.

set -euo pipefail

# ── Load .env from project root (if present on dev machine) ──────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"
if [ -f "$ENV_FILE" ]; then
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^(OVM_HOST|OVM_USER|DEPLOY_DIR)$ ]] && export "$key=${value//\"/}"
    done < <(grep -E '^(OVM_HOST|OVM_USER|DEPLOY_DIR)=' "$ENV_FILE")
fi

# ── Configuration ────────────────────────────────────────────────────────────
OVM_HOST="${OVM_HOST:-openmediavault.local}"
OVM_USER="${OVM_USER:-root}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/MarketScanner}"
REPO_URL="https://github.com/damyankasapov-wq/MarketScanner.git"
# OMV Compose plugin: stacks live under the DockerCompose shared folder.
# Each subdirectory is visible in Services → Compose → Docker Files.
COMPOSE_DISK_UUID="7b7a8925-0b41-4324-b00b-14e35480b383"
COMPOSE_DIR="/srv/dev-disk-by-uuid-${COMPOSE_DISK_UUID}/DockerCompose/marketscanner"
STACK_NAME="marketscanner"
# OMV sentinel UUID used when inserting a *new* compose record via omv-rpc:
OMV_NEW_UUID="fa4b1c66-ef79-11e5-87a0-0002b3a176b4"
# ─────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
error() { echo -e "${RED}[deploy]${NC} $*" >&2; }

CMD="${1:-deploy}"

ssh_run() {
    ssh -t "${OVM_USER}@${OVM_HOST}" "$@"
}

ssh_quiet() {
    ssh "${OVM_USER}@${OVM_HOST}" "$@"
}

# ── Subcommands ───────────────────────────────────────────────────────────────

cmd_status() {
    info "Container status on ${OVM_HOST}…"
    ssh_quiet "
        cd '${COMPOSE_DIR}' 2>/dev/null \
            && docker compose -f '${STACK_NAME}.yml' ps \
            || echo 'Stack not deployed yet — run ./scripts/deploy.sh'
    "
}

cmd_stop() {
    info "Stopping MarketScanner stack on ${OVM_HOST}…"
    ssh_quiet "
        cd '${COMPOSE_DIR}' 2>/dev/null \
            && docker compose -f '${STACK_NAME}.yml' down \
            || echo 'Nothing to stop.'
    "
}

cmd_restart() {
    info "Restarting MarketScanner stack on ${OVM_HOST} (no code update)…"
    ssh_quiet "
        cd '${COMPOSE_DIR}'
        docker compose -f '${STACK_NAME}.yml' restart
        docker compose -f '${STACK_NAME}.yml' ps
    "
}

cmd_logs() {
    info "Streaming scanner logs from ${OVM_HOST} (Ctrl-C to stop)…"
    ssh_run "
        cd '${COMPOSE_DIR}'
        docker compose -f '${STACK_NAME}.yml' logs -f scanner
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
            echo 'Fill in ${DEPLOY_DIR}/.env with real credentials, then re-run deploy.'
            echo ''
        fi

        # ── 3. Stop old screen session (legacy — pre-containerisation) ────
        screen -S marketscanner -X quit 2>/dev/null || true

        # ── 4. Place compose file in OMV-watched folder ───────────────────
        echo '--- Registering stack in OMV Compose plugin ---'
        mkdir -p '${COMPOSE_DIR}'
        cp '${DEPLOY_DIR}/docker-compose.yml' '${COMPOSE_DIR}/${STACK_NAME}.yml'
        # .env must be present in the same directory for docker compose env_file
        cp '${DEPLOY_DIR}/.env' '${COMPOSE_DIR}/.env'
        chown -R root:root '${COMPOSE_DIR}'
        chmod 700 '${COMPOSE_DIR}'
        chmod 600 '${COMPOSE_DIR}/${STACK_NAME}.yml' '${COMPOSE_DIR}/.env'

        # Register / update the stack in the OMV Compose plugin so it appears
        # in Services → Compose → Docker Files in the OMV web UI
        EXISTING_UUID=\$(omv-rpc 'Compose' 'getFileList' '{\"start\":0,\"limit\":100}' 2>/dev/null \
            | python3 -c \"import sys,json; data=json.load(sys.stdin); \
              matches=[f['uuid'] for f in data['data'] if f['name']=='${STACK_NAME}']; \
              print(matches[0] if matches else '')\" 2>/dev/null || echo '')
        UUID=\${EXISTING_UUID:-${OMV_NEW_UUID}}
        if [ -n \"\$EXISTING_UUID\" ]; then
            echo \"    Updating existing stack (UUID: \$UUID)...\"
        else
            echo \"    Registering new stack (UUID: \$UUID)...\"
        fi
        BODY=\$(cat '${COMPOSE_DIR}/${STACK_NAME}.yml' \
            | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')
        omv-rpc 'Compose' 'setFile' \"{
            \\\"uuid\\\": \\\"\$UUID\\\",
            \\\"name\\\": \\\"${STACK_NAME}\\\",
            \\\"description\\\": \\\"MarketScanner — PostgreSQL + scanner\\\",
            \\\"showenv\\\": false,
            \\\"showoverride\\\": false,
            \\\"body\\\": \$BODY,
            \\\"env\\\": \\\"\\\",
            \\\"override\\\": \\\"\\\"
        }\" > /dev/null && echo '    Stack registered in OMV GUI OK.' || \
            echo '    WARNING: omv-rpc registration failed — stack runs but may not appear in GUI.'

        # ── 5. Build scanner image and start full stack ───────────────────
        echo '--- Building scanner image and starting full stack (db + scanner) ---'
        # Run docker compose from DEPLOY_DIR so build context (.) resolves
        # to the git repo root where the Dockerfile lives.
        cd '${DEPLOY_DIR}'
        docker compose build
        docker compose up -d

        # Wait for containers to stabilise
        echo '--- Waiting for containers to stabilise ---'
        sleep 8
        docker compose ps

        # Refresh the OMV-watched copy of the compose file
        cp docker-compose.yml '${COMPOSE_DIR}/${STACK_NAME}.yml'

        echo ''
        echo '✓ MarketScanner stack is running.'
        echo \"  Status  : ./scripts/deploy.sh --status\"
        echo \"  Logs    : ./scripts/deploy.sh --logs\"
        echo \"  Stop    : ./scripts/deploy.sh --stop\"
        echo \"  OMV GUI : http://openmediavault.local/#/services/compose/dockerfiles\"
    "
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "$CMD" in
    deploy|"")  cmd_deploy  ;;
    --restart)  cmd_restart ;;
    --stop)     cmd_stop    ;;
    --status)   cmd_status  ;;
    --logs)     cmd_logs    ;;
    *)
        echo "Usage: $0 [deploy|--restart|--stop|--status|--logs]"
        exit 1
        ;;
esac
