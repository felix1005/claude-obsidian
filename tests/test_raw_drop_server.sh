#!/usr/bin/env bash
# test_raw_drop_server.sh — smoke tests for scripts/raw-drop-server.py.
#
# Boots the server against a throwaway temp vault on an OS-assigned port, then
# exercises auth, path-traversal sanitization, the pending list, size/extension
# limits, the raw-body and base64 code paths, and the short-token startup guard.
# Never touches a real vault. Exits non-zero on any failure.
#
# Usage: bash tests/test_raw_drop_server.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_ROOT="$(dirname "$SCRIPT_DIR")"
SERVER="$VAULT_ROOT/scripts/raw-drop-server.py"

PASS=0
FAIL=0
pass() { echo "OK   $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL $1"; FAIL=$((FAIL+1)); }
assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then pass "$label (got $actual)"
  else fail "$label: expected '$expected', got '$actual'"; fi
}

command -v curl >/dev/null 2>&1 || { echo "SKIP: curl not available"; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not available"; exit 0; }

TMP_VAULT="$(mktemp -d)"
mkdir -p "$TMP_VAULT/.raw"
LOGF="$TMP_VAULT/server.stderr"
TOKEN="test-secret-token-0123456789abcdef"

cleanup() { [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null || true; rm -rf "$TMP_VAULT"; }
trap cleanup EXIT

# Port 0 -> OS assigns a free port; we read it back from the startup line.
# A small MAX_BYTES makes the oversized-body case cheap to exercise.
RAW_DROP_TOKEN="$TOKEN" RAW_DROP_VAULT="$TMP_VAULT" RAW_DROP_PORT=0 \
  RAW_DROP_MAX_BYTES=4096 python3 "$SERVER" >"$LOGF" 2>&1 &
SRV=$!

PORT=""
for _ in $(seq 1 50); do
  PORT="$(sed -n 's#.*listening on http://[^:]*:\([0-9]\+\).*#\1#p' "$LOGF" 2>/dev/null | head -1)"
  [ -n "$PORT" ] && break
  sleep 0.1
done
[ -n "$PORT" ] && pass "server bound an OS-assigned port ($PORT)" || { fail "server never reported a port"; cat "$LOGF"; exit 1; }
BASE="http://127.0.0.1:$PORT"

# 1. health, no auth, and must NOT leak the vault path
health="$(curl -sS "$BASE/health")"
case "$health" in *'"ok": true'*) pass "health returns ok without auth" ;; *) fail "health body: $health" ;; esac
case "$health" in *"$TMP_VAULT"*) fail "health leaks vault path" ;; *) pass "health does not leak vault path" ;; esac

# 2. drop rejected without / with wrong token
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/drop" \
  -H "Content-Type: application/json" -d '{"content":"x"}')
assert_eq "drop without token is 401" "401" "$code"
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/drop" \
  -H "Authorization: Bearer wrong" -H "Content-Type: application/json" -d '{"content":"x"}')
assert_eq "drop with wrong token is 401" "401" "$code"

# 3. valid drop with a path-traversal filename — succeeds AND stays inside .raw/
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/drop" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"filename":"../../etc/pwn notes!!.md","agent":"scout-1","content":"# F\nbody"}')
assert_eq "authed drop returns 201" "201" "$code"
esc_count=$(find "$TMP_VAULT/.raw" -type f | wc -l | tr -d ' ')
assert_eq "exactly one file landed in .raw/" "1" "$esc_count"
[ ! -e "$TMP_VAULT/etc" ] && [ ! -e "/etc/pwn notes!!.md" ] \
  && pass "no path traversal escaped the sandbox" || fail "path traversal escaped the sandbox"
landed="$(find "$TMP_VAULT/.raw" -type f -name '*.md' | head -1)"
case "$(basename "$landed")" in
  *\ *) fail "filename contains a space: $(basename "$landed")" ;;
  *pwn*notes*) pass "sanitized filename is space-free and keeps a safe stem" ;;
  *) fail "sanitized filename unexpected: $(basename "$landed")" ;;
esac
grep -q "raw-drop-provenance" "$landed" && pass "provenance header present" || fail "provenance header missing"

# 4. pending lists the un-ingested drop
pending=$(curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/pending")
case "$pending" in *pwn*) pass "pending lists the drop" ;; *) fail "pending did not list the drop: $pending" ;; esac

# 5. disallowed extension rejected (415)
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/drop" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"filename":"x.exe","content":"x"}')
assert_eq "disallowed extension is 415" "415" "$code"

# 6. oversized body rejected (413) — body exceeds RAW_DROP_MAX_BYTES=4096
big=$(head -c 5000 /dev/zero | tr '\0' 'a')
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/drop" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: text/plain" \
  -H "X-Filename: big.md" --data-binary "$big")
assert_eq "oversized body is 413" "413" "$code"

# 7. raw-body path (non-JSON Content-Type + X-Filename header)
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/drop" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: text/plain" \
  -H "X-Filename: raw-note.md" --data-binary "hello from raw body")
assert_eq "raw-body drop returns 201" "201" "$code"
find "$TMP_VAULT/.raw" -name '*raw-note*' | grep -q . && pass "raw-body file landed" || fail "raw-body file missing"

# 8. base64 binary drop -> written verbatim (no provenance header injected)
b64=$(printf '%%PDF-1.4 fake' | base64)
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/drop" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"filename\":\"doc.pdf\",\"encoding\":\"base64\",\"content\":\"$b64\"}")
assert_eq "base64 drop returns 201" "201" "$code"
pdf="$(find "$TMP_VAULT/.raw" -name '*doc*.pdf' | head -1)"
[ -n "$pdf" ] && ! grep -q "raw-drop-provenance" "$pdf" \
  && pass "binary drop written verbatim (no provenance header)" || fail "binary drop wrong"

# 9. short-token startup guard (separate invocation; must exit 2, not serve)
set +e
RAW_DROP_TOKEN="short" RAW_DROP_VAULT="$TMP_VAULT" RAW_DROP_PORT=0 \
  python3 "$SERVER" >/dev/null 2>&1
rc=$?
set -e
assert_eq "short token aborts startup (exit 2)" "2" "$rc"

echo
echo "raw-drop-server: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
