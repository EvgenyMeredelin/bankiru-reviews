#!/usr/bin/env bash
# ── start.sh — Bootstrap script for the bankiru-reviews stack ───────────────
#
# This is the ONLY way to start the stack in production. It performs two jobs:
#   1. Authenticate with Infisical (self-hosted secrets manager) and export
#      all project secrets as a .env file into tmpfs (/dev/shm).
#   2. Run `docker compose up` with that .env file.
#
# Security design: secrets are written to /dev/shm (a RAM-backed tmpfs
# filesystem). They never touch the SSD and are automatically wiped on
# host reboot. This means you must re-run this script after every reboot.
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

# Exit immediately on any error (-e), treat unset variables as errors (-u),
# and fail pipelines on the first failing command (-o pipefail).
set -euo pipefail

# ── Path constants ──────────────────────────────────────────────────────────
# Resolve the directory containing this script so we can find the compose
# file relative to it (works regardless of the caller's working directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Secrets are stored on tmpfs — RAM-only, never written to disk.
SECRETS_DIR="/dev/shm/bankiru-reviews-secrets"
SECRETS_FILE="${SECRETS_DIR}/.env"
# The compose file lives one directory up from scripts/.
COMPOSE_FILE="${SCRIPT_DIR}/../docker-compose.yml"

# ── Infisical project coordinates ───────────────────────────────────────────
# These identify the Infisical project and machine identity that holds all
# bankiru-reviews secrets. The client ID is not sensitive (like a username);
# the client secret (prompted interactively) is the actual credential.
PROJECT_ID="1038a643-15a6-42f5-9996-22cbc9b4738e"
INFISICAL_CLIENT_ID="b8be4a01-8d9c-4a6a-b85c-28ad705e6144"
# Allow overriding the Infisical API URL for testing against a different instance.
INFISICAL_API_URL="${INFISICAL_API_URL:-https://infisical.uva-advanced.ru/api}"

# ── Preflight check ────────────────────────────────────────────────────────
# Verify the Infisical CLI is installed before doing anything else.
if ! command -v infisical &>/dev/null; then
  echo "[error] The Infisical CLI is not installed."
  echo "        Install it with:"
  echo "          curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | sudo -E bash"
  echo "          sudo apt-get update && sudo apt-get install -y infisical"
  exit 1
fi

# ── Credentials ─────────────────────────────────────────────────────────────
# The client secret can be passed via environment variable (for CI/automation)
# or entered interactively. The `-rs` flags on `read` suppress echo so the
# secret doesn't appear on screen.
if [[ -z "${INFISICAL_TOKEN:-}" && -z "${INFISICAL_CLIENT_SECRET:-}" ]]; then
  echo -n "[auth] Enter Infisical client secret: "
  read -rs INFISICAL_CLIENT_SECRET
  echo
  if [[ -z "$INFISICAL_CLIENT_SECRET" ]]; then
    echo "[error] Client secret cannot be empty."
    exit 1
  fi
fi

# ── Parse command-line flags ────────────────────────────────────────────────
# --no-start: fetch secrets but don't start Docker (useful for debugging).
# --refresh:  re-fetch secrets AND force-recreate all containers.
# Any other arguments are passed through to `docker compose up`.
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

# ── Authenticate with Infisical ────────────────────────────────────────────
# Universal Auth is Infisical's machine-to-machine authentication method.
# The `--silent --plain` flags suppress interactive output and return only
# the raw JWT token, which is stored in INFISICAL_TOKEN for subsequent
# `infisical export` calls.
if [[ -n "${INFISICAL_TOKEN:-}" ]]; then
  echo "[secrets] Using existing INFISICAL_TOKEN (skip login)"
  export INFISICAL_TOKEN
  export INFISICAL_DISABLE_UPDATE_CHECK=true
else
  echo "[secrets] Authenticating with Infisical…"
  export INFISICAL_TOKEN
  INFISICAL_TOKEN=$(infisical login \
    --method=universal-auth \
    --client-id="$INFISICAL_CLIENT_ID" \
    --client-secret="$INFISICAL_CLIENT_SECRET" \
    --domain="$INFISICAL_API_URL" \
    --silent --plain)
  # Suppress the "new version available" nag from the Infisical CLI.
  export INFISICAL_DISABLE_UPDATE_CHECK=true
fi

# ── Prepare the secrets directory on tmpfs ──────────────────────────────────
# Handle the edge case where a previous run (as root) left a directory that
# the current user can't write to. Test writability with a probe file.
if [[ -d "${SECRETS_DIR}" ]] && ! touch "${SECRETS_DIR}/.write-test" 2>/dev/null; then
  echo "[secrets] ${SECRETS_DIR} is not writable (leftover from a previous root-owned run)."
  echo "[secrets] Removing it with sudo…"
  sudo rm -rf "${SECRETS_DIR}"
else
  # Clean up the probe file if the directory was writable.
  rm -f "${SECRETS_DIR}/.write-test" 2>/dev/null
fi
# Create the directory with restrictive permissions (owner-only read/write/exec).
mkdir -p "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"

# ── Export secrets from Infisical to the .env file ──────────────────────────
# Pull all secrets from the project root path ("/") in the "prod" environment
# and write them as KEY=VALUE pairs (dotenv format) to the tmpfs file.
echo "[secrets] Exporting secrets from Infisical project root…"
infisical export \
  --projectId="$PROJECT_ID" \
  --env=prod \
  --path="/" \
  --format=dotenv \
  --domain="$INFISICAL_API_URL" \
  > "${SECRETS_FILE}"

# Sanity check: the file must exist and be non-empty.
if [[ ! -s "${SECRETS_FILE}" ]]; then
  echo "[secrets] ERROR: ${SECRETS_FILE} is missing or empty."
  exit 1
fi
echo "[secrets] Secret file ready at ${SECRETS_FILE}"

# ── Start (or refresh) the Docker Compose stack ────────────────────────────
# If --no-start was passed, stop here (useful for just refreshing secrets).
if [[ "$NO_START" == true ]]; then
  echo "[secrets] --no-start: skipping docker compose."
  exit 0
fi

# Pass --env-file so docker compose ${VAR:-default} substitution resolves
# against the Infisical-exported values (not just the shell env). Without
# this flag, port overrides and other compose-layer substitutions silently
# fall back to their defaults because Docker Compose doesn't automatically
# read the env_file directive for variable interpolation in the YAML itself.
#
# The "${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"}" pattern safely expands the
# array even when it's empty (avoids "unbound variable" error with set -u).
if [[ "$REFRESH" == true ]]; then
  # --refresh: rebuild the image from scratch and force-recreate all three
  # service containers. Use this after code changes or secret rotation.
  echo "[stack] --refresh: rebuilding image and recreating api/parser/ui…"
  docker compose --env-file "${SECRETS_FILE}" -f "${COMPOSE_FILE}" \
    up -d --build --force-recreate \
    api parser ui "${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"}"
else
  # Normal start: create containers if they don't exist, start stopped ones,
  # but don't recreate running containers.
  echo "[stack] Starting docker compose stack…"
  docker compose --env-file "${SECRETS_FILE}" -f "${COMPOSE_FILE}" \
    up -d "${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"}"
fi
