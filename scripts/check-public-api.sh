#!/usr/bin/env bash
# ── check-public-api.sh — live checks of GET /reviews behaviour ──────────────
#
# The pytest suite simulates the public gateway by setting X-Bankiru-Gateway
# itself, which covers everything the application can distinguish. This script
# covers what only a real deployment can show:
#
#   * TLS termination and routing by the host Nginx;
#   * that Nginx *overwrites* X-Bankiru-Gateway, so a client cannot forge its
#     absence to skip the token check;
#   * that an error body from the API reaches the client unaltered (no
#     proxy_intercept_errors / error_page substitution);
#   * that the same request answered without a token through the gateway is
#     answered without one on the loopback address.
#
# Read-only: every request is a GET. Nothing is written or deleted.
#
# Usage (on the VM, where the loopback port is reachable):
#   set -a; . /dev/shm/bankiru-reviews-secrets/.env; set +a
#   ./scripts/check-public-api.sh
#
# Or from anywhere, public checks only:
#   GUEST_API_TOKEN=... SKIP_LOOPBACK=1 ./scripts/check-public-api.sh
#
# Tokens come from the environment only — none are stored in this repository.

set -uo pipefail

PUBLIC_URL="${PUBLIC_URL:-https://bankiru.uva-advanced.ru}"
LOOPBACK_URL="${LOOPBACK_URL:-http://127.0.0.1:1706}"
SKIP_LOOPBACK="${SKIP_LOOPBACK:-0}"

# GUEST_API_TOKEN is owner@example.org:token pairs; use the first token.
# Expanded with :- first: under `set -u` a bare ${GUEST_API_TOKEN%%,*} on an
# unset variable aborts the shell before the message below can be printed.
# A bare token (no colon) is left unchanged so a one-off GUEST_API_TOKEN=tok
# still works. Whitespace trim matches the Settings parser (spaces around `:`).
GUEST_TOKEN="${GUEST_API_TOKEN:-}"
GUEST_TOKEN="${GUEST_TOKEN%%,*}"
GUEST_TOKEN="${GUEST_TOKEN#*:}"
GUEST_TOKEN="${GUEST_TOKEN#"${GUEST_TOKEN%%[![:space:]]*}"}"
GUEST_TOKEN="${GUEST_TOKEN%"${GUEST_TOKEN##*[![:space:]]}"}"

if [[ -z "${GUEST_TOKEN}" ]]; then
    echo "GUEST_API_TOKEN is not set — source the secrets first." >&2
    exit 2
fi

passed=0
failed=0

# ── Helpers ─────────────────────────────────────────────────────────────────
# request <url> <token-or-empty> [curl args...]
# Prints the body, then the status code on the last line — the order curl's
# -w appends it in, which is what the callers below split on.
request() {
    local url="$1" token="$2"
    shift 2
    local -a auth=()
    [[ -n "${token}" ]] && auth=(-H "API-Token: ${token}")
    # ${a[@]+...} keeps an empty array from tripping `set -u` on older bash.
    curl -sS -m 120 -w '\n%{http_code}' \
        ${auth[@]+"${auth[@]}"} "$@" "${url}/reviews" 2>&1
}

# check <name> <expected-status> <actual-status> [expected-substring] [body]
check() {
    local name="$1" want="$2" got="$3" needle="${4:-}" body="${5:-}"
    if [[ "${got}" != "${want}" ]]; then
        printf '  FAIL  %-52s expected %s, got %s\n' "${name}" "${want}" "${got}"
        failed=$((failed + 1))
        return
    fi
    if [[ -n "${needle}" && "${body}" != *"${needle}"* ]]; then
        printf '  FAIL  %-52s status %s but body lacks %q\n' "${name}" "${got}" "${needle}"
        failed=$((failed + 1))
        return
    fi
    printf '  ok    %-52s %s\n' "${name}" "${got}"
    passed=$((passed + 1))
}

# run <name> <url> <token> <expected-status> <expected-substring> [curl args...]
run() {
    local name="$1" url="$2" token="$3" want="$4" needle="$5"
    shift 5
    local out status body
    out="$(request "${url}" "${token}" "$@")"
    status="${out##*$'\n'}"
    body="${out%$'\n'*}"
    check "${name}" "${want}" "${status}" "${needle}" "${body}"
}

# ── Through the public gateway ──────────────────────────────────────────────
echo "== ${PUBLIC_URL} (Nginx sets X-Bankiru-Gateway) =="

# Each of these requires "detail" in the body, not just the status: an Nginx
# error page substituted by proxy_intercept_errors would carry the right code
# with the wrong body, which is one of the things this script exists to catch.
run "no token is refused" \
    "${PUBLIC_URL}" "" 403 'detail'

run "a wrong token is refused" \
    "${PUBLIC_URL}" "definitely-not-a-token" 403 'detail'

# A client cannot pretend to be internal: Nginx overwrites the header.
run "a forged gateway header does not help" \
    "${PUBLIC_URL}" "" 403 'detail' -H 'X-Bankiru-Gateway: 0'

run "a guest token is accepted" \
    "${PUBLIC_URL}" "${GUEST_TOKEN}" 200 '"startDate"' \
    --get --data-urlencode 'startDate=2026-08-01' \
    --data-urlencode 'endDate=2026-08-02'

run "an unknown parameter is a 422" \
    "${PUBLIC_URL}" "${GUEST_TOKEN}" 422 'extra_forbidden' \
    --get --data-urlencode 'limit=1'

# The two 400s carry fixed wording; a change in either must be deliberate.
run "an inverted range is a 400" \
    "${PUBLIC_URL}" "${GUEST_TOKEN}" 400 'Empty date range' \
    --get --data-urlencode 'startDate=2026-08-02' \
    --data-urlencode 'endDate=2026-08-01'

# Explicit dates rather than relying on the stored range: with no dates the
# span is whatever the table happens to hold, so a freshly seeded database
# would answer 200 and this check would report a failure that is not one.
run "summarizing over three months is a 400" \
    "${PUBLIC_URL}" "${GUEST_TOKEN}" 400 'at most three' \
    --get --data-urlencode 'startDate=2026-01-01' \
    --data-urlencode 'endDate=2026-08-01' \
    --data-urlencode 'summarize=true'

# ── Semantic search: two acceptable outcomes ────────────────────────────────
# Requiring 503 here would turn into a false alarm the moment the embeddings
# provider is healthy again, so both a working search and the documented
# refusal count as a pass.
echo "== semantic search (200 with results or 503 with the documented detail) =="
out="$(request "${PUBLIC_URL}" "${GUEST_TOKEN}" \
    --get --data-urlencode 'keywords=очередь в отделении' \
    --data-urlencode 'startDate=2026-08-01' --data-urlencode 'endDate=2026-08-02')"
status="${out##*$'\n'}"
body="${out%$'\n'*}"
case "${status}" in
    200)
        check "semantic search works" 200 "${status}" '"startDate"' "${body}"
        ;;
    503)
        check "semantic search refuses in full" 503 "${status}" \
            'Semantic search is temporarily unavailable' "${body}"
        # The provider's own message names an internal endpoint.
        if [[ "${body}" == *"cloud.ru"* || "${body}" == *"/v1/embeddings"* ]]; then
            echo "  FAIL  the 503 body leaks the provider endpoint"
            failed=$((failed + 1))
        else
            echo "  ok    the 503 body names no internal endpoint"
            passed=$((passed + 1))
        fi
        ;;
    *)
        check "semantic search" "200 or 503" "${status}" "" "${body}"
        ;;
esac

# ── Past the gateway, on the loopback address ───────────────────────────────
if [[ "${SKIP_LOOPBACK}" == "1" ]]; then
    echo "== loopback checks skipped (SKIP_LOOPBACK=1) =="
else
    echo "== ${LOOPBACK_URL} (no gateway header, no token required) =="

    run "no token is fine off the gateway" \
        "${LOOPBACK_URL}" "" 200 '"startDate"' \
        --get --data-urlencode 'startDate=2026-08-01' \
        --data-urlencode 'endDate=2026-08-02'

    run "the same 400 arrives off the gateway" \
        "${LOOPBACK_URL}" "" 400 'Empty date range' \
        --get --data-urlencode 'startDate=2026-08-02' \
        --data-urlencode 'endDate=2026-08-01'

    run "the same 422 arrives off the gateway" \
        "${LOOPBACK_URL}" "" 422 'extra_forbidden' \
        --get --data-urlencode 'limit=1'
fi

# ── Verdict ─────────────────────────────────────────────────────────────────
echo
echo "passed: ${passed}, failed: ${failed}"
[[ "${failed}" -eq 0 ]]
