#!/usr/bin/env bash
# Vulgone Follower launcher.
#
#   ./startup.sh            build (if needed) and start in Docker, wait until it is up
#   ./startup.sh stop       stop the container (the automation stops with it)
#   ./startup.sh restart    rebuild and restart
#   ./startup.sh logs       follow the container logs
#   ./startup.sh status     show container state and health
#   ./startup.sh tests      run the unit tests (rules, gates, classifier) without a mailbox
#   ./startup.sh local      run without Docker (Python virtualenv in .venv)
#
# HOST_PORT=8080 ./startup.sh   publishes on another port (default 5055).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

HOST_PORT="${HOST_PORT:-5055}"
export HOST_PORT
URL="http://127.0.0.1:${HOST_PORT}"
REQUIRED_VARS=(MAIL_USER MAIL_PASS)

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

check_env() {
    [[ -f .env ]] || fail ".env not found. Create it with: MAIL_USER, MAIL_PASS, IMAP_HOST, IMAP_PORT, SMTP_HOST, SMTP_PORT, SENDER_NAME"
    for var in "${REQUIRED_VARS[@]}"; do
        grep -qE "^${var}=.+" .env || fail "${var} is missing or empty in .env"
    done
}

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose "$@"
    else
        fail "Docker Compose is not installed (try: ./startup.sh local)"
    fi
}

wait_until_up() {
    info "Waiting for the portal at ${URL} ..."
    for _ in $(seq 1 90); do
        if curl -fsS "${URL}/api/status" >/dev/null 2>&1; then
            info "Vulgone Follower is running: ${URL}"
            info "It starts in DRY RUN: nothing is emailed until you switch to LIVE in Settings."
            info "The first minutes build the mail cache and classify past replies."
            return 0
        fi
        sleep 1
    done
    compose logs --tail 40
    fail "the portal did not come up within 90s (logs above)"
}

start_docker() {
    check_env
    command -v docker >/dev/null 2>&1 || fail "Docker is not installed (try: ./startup.sh local)"
    docker info >/dev/null 2>&1 || fail "cannot reach the Docker daemon (is it running, and is your user in the docker group?)"
    info "Building and starting the container ..."
    compose up -d --build "$@"
    wait_until_up
}

venv() {
    command -v python3 >/dev/null 2>&1 || fail "python3 is not installed"
    if [[ ! -d .venv ]]; then
        info "Creating virtualenv in .venv ..."
        python3 -m venv .venv
    fi
    info "Installing requirements ..."
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt pytest
}

start_local() {
    check_env
    venv
    info "Starting on ${URL} (Ctrl+C to stop)"
    PORT="${HOST_PORT}" BIND_HOST=127.0.0.1 DATA_DIR="$PWD/data" exec .venv/bin/gunicorn \
        --workers 1 --threads 8 --timeout 300 --bind "127.0.0.1:${HOST_PORT}" app:app
}

run_tests() {
    venv
    AUTOMATION=0 DATA_DIR=/tmp/vulgone-follower-tests .venv/bin/python -m pytest tests/ -q
}

case "${1:-start}" in
    start)   start_docker ;;
    stop)    info "Stopping ..."; compose down ;;
    restart) compose down; start_docker ;;
    logs)    compose logs -f --tail 100 ;;
    status)  compose ps ;;
    tests)   run_tests ;;
    local)   start_local ;;
    -h|--help|help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)       fail "unknown command '$1' (use: start, stop, restart, logs, status, tests, local)" ;;
esac
