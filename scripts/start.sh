#!/usr/bin/env bash
# start.sh — Fetch secrets from Infisical and bring up the bankiru-reviews stack.
#
# Uses the Infisical CLI's `export` command to pull all secrets from the
# project root and write them as a single .env file into
# /dev/shm/bankiru-reviews-secrets/ (a tmpfs-backed path — secrets never
# touch the SSD and are wiped on host reboot).
#
# PREREQUISITES
# ─────────────
# 1. Install the Infisical CLI:
#      curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | sudo -E bash
#      sudo apt-get update && sudo apt-get install -y infisical
#
# 2. The script prompts for the client secret interactively (never stored
#    on disk). The client ID is hardcoded below since it is not sensitive
#    (equivalent to a username).
#
# USAGE
#   ./start.sh               # fetch secrets + docker compose up -d
#   ./start.sh --no-start    # fetch secrets only (skip compose up)
#   ./start.sh --refresh     # re-fetch secrets then recreate api/parser/ui
#
# After a host reboot /dev/shm is cleared; re-run this script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="/dev/shm/bankiru-reviews-secrets"
SECRETS_FILE="${SECRETS_DIR}/.env"
COMPOSE_FILE="${SCRIPT_DIR}/../docker-compose.yml"

# Infisical project hosting all bankiru-reviews secrets at the root path.
PROJECT_ID="1038a643-15a6-42f5-9996-22cbc9b4738e"
INFISICAL_CLIENT_ID="b8be4a01-8d9c-4a6a-b85c-28ad705e6144"
INFISICAL_API_URL="${INFISICAL_API_URL:-https://infisical.uva-advanced.ru/api}"

# ── Preflight ───────────────────────────────────────────────────────────────
if ! command -v infisical &>/dev/null; then
  echo "[error] The Infisical CLI is not installed."
  echo "        Install it with:"
  echo "          curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | sudo -E bash"
  echo "          sudo apt-get update && sudo apt-get install -y infisical"
  exit 1
fi

# ── Credentials ─────────────────────────────────────────────────────────────
if [[ -z "${INFISICAL_CLIENT_SECRET:-}" ]]; then
  echo -n "[auth] Enter Infisical client secret: "
  read -rs INFISICAL_CLIENT_SECRET
  echo
  if [[ -z "$INFISICAL_CLIENT_SECRET" ]]; then
    echo "[error] Client secret cannot be empty."
    exit 1
  fi
fi

# ── Parse flags ─────────────────────────────────────────────────────────────
NO_START=false
REFRESH=false
COMPOSE_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --no-start) NO_START=true ;;
    --refresh)  REFRESH=true ;;
    *)          COMPOSE_ARGS+=("$arg") ;;
  esac
done

# ── Authenticate ────────────────────────────────────────────────────────────
echo "[secrets] Authenticating with Infisical…"
export INFISICAL_TOKEN
INFISICAL_TOKEN=$(infisical login \
  --method=universal-auth \
  --client-id="$INFISICAL_CLIENT_ID" \
  --client-secret="$INFISICAL_CLIENT_SECRET" \
  --domain="$INFISICAL_API_URL" \
  --silent --plain)
export INFISICAL_DISABLE_UPDATE_CHECK=true

# ── Prepare secrets directory ───────────────────────────────────────────────
if [[ -d "${SECRETS_DIR}" ]] && ! touch "${SECRETS_DIR}/.write-test" 2>/dev/null; then
  echo "[secrets] ${SECRETS_DIR} is not writable (leftover from a previous root-owned run)."
  echo "[secrets] Removing it with sudo…"
  sudo rm -rf "${SECRETS_DIR}"
else
  rm -f "${SECRETS_DIR}/.write-test" 2>/dev/null
fi
mkdir -p "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"

# ── Export secrets ──────────────────────────────────────────────────────────
echo "[secrets] Exporting secrets from Infisical project root…"
infisical export \
  --projectId="$PROJECT_ID" \
  --env=prod \
  --path="/" \
  --format=dotenv \
  --domain="$INFISICAL_API_URL" \
  > "${SECRETS_FILE}"

if [[ ! -s "${SECRETS_FILE}" ]]; then
  echo "[secrets] ERROR: ${SECRETS_FILE} is missing or empty."
  exit 1
fi
echo "[secrets] Secret file ready at ${SECRETS_FILE}"

# ── Start (or refresh) the stack ────────────────────────────────────────────
if [[ "$NO_START" == true ]]; then
  echo "[secrets] --no-start: skipping docker compose."
  exit 0
fi

# Pass --env-file so docker compose ${VAR:-default} substitution resolves
# against the Infisical-exported values (not just the shell env). Without
# this, port overrides and other compose-layer substitutions silently fall
# back to their defaults.
if [[ "$REFRESH" == true ]]; then
  echo "[stack] --refresh: rebuilding image and recreating api/parser/ui…"
  docker compose --env-file "${SECRETS_FILE}" -f "${COMPOSE_FILE}" \
    up -d --build --force-recreate \
    api parser ui "${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"}"
else
  echo "[stack] Starting docker compose stack…"
  docker compose --env-file "${SECRETS_FILE}" -f "${COMPOSE_FILE}" \
    up -d "${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"}"
fi
