#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# tofu state-rm utility, driven by the semaphore-mcp `run_tofu_state_rm` tool.
#
# `tofu state rm` is a CLI-only operation with no Semaphore REST endpoint, so it
# is run here as a Semaphore **bash-app template**. The MCP tool passes the
# arguments below; Semaphore forwards them to this script as CLI args.
#
# Contract (must match run_tofu_state_rm):
#   state_rm.sh <tofu_root> <mode> <address> [<address> ...]
#     tofu_root : repo-relative tofu root, e.g. opentofu/zephyrex
#     mode      : "list" (preview only) | "rm" (remove from state)
#     address.. : one or more resource addresses
#
# The backend (pg) connection and any state-encryption key come from the
# Semaphore template's environment (the same env the root's normal plan/apply
# template uses) — this script is env-driven and never embeds them. The root's
# own pg backend `schema_name` isolates its state, so <tofu_root> fully
# determines which state is touched.
set -euo pipefail

die() {
  echo "ERROR: $*" >&2
  exit 2
}

[ "$#" -ge 3 ] || die "usage: state_rm.sh <tofu_root> <list|rm> <address> [address...]"

TOFU_ROOT="$1"
MODE="$2"
shift 2
ADDRESSES=("$@")

case "$MODE" in
  list | rm) ;;
  *) die "mode must be 'list' or 'rm', got '$MODE'" ;;
esac

[ -d "$TOFU_ROOT" ] || die "tofu_root '$TOFU_ROOT' is not a directory (cwd: $(pwd))"
cd "$TOFU_ROOT"

echo "== tofu init ($TOFU_ROOT) =="
tofu init -input=false

echo
echo "== current state (matching entries) =="
# `tofu state list` exits non-zero on an empty state; tolerate that.
STATE_BEFORE="$(tofu state list 2>/dev/null || true)"

matches_for() {
  # Print state entries that equal, or are indexed instances of, an address.
  local addr="$1"
  printf '%s\n' "$STATE_BEFORE" | grep -F -e "$addr" || true
}

any_missing=0
for addr in "${ADDRESSES[@]}"; do
  found="$(matches_for "$addr")"
  if [ -n "$found" ]; then
    echo "MATCH  $addr:"
    printf '%s\n' "$found" | sed 's/^/  /'
  else
    echo "ABSENT $addr (no matching state entry)"
    any_missing=1
  fi
done

if [ "$MODE" = "list" ]; then
  echo
  echo "== preview only — no state entries were removed =="
  exit 0
fi

echo
echo "== tofu state rm =="
rm_failures=0
for addr in "${ADDRESSES[@]}"; do
  if [ -z "$(matches_for "$addr")" ]; then
    echo "SKIP   $addr (absent from state)"
    continue
  fi
  if tofu state rm "$addr"; then
    echo "REMOVED $addr"
  else
    echo "FAILED  $addr"
    rm_failures=$((rm_failures + 1))
  fi
done

echo
echo "== remaining state (matching entries after rm) =="
STATE_AFTER="$(tofu state list 2>/dev/null || true)"
for addr in "${ADDRESSES[@]}"; do
  remaining="$(printf '%s\n' "$STATE_AFTER" | grep -F -e "$addr" || true)"
  if [ -n "$remaining" ]; then
    echo "STILL PRESENT $addr:"
    printf '%s\n' "$remaining" | sed 's/^/  /'
  else
    echo "GONE   $addr"
  fi
done

[ "$rm_failures" -eq 0 ] || die "$rm_failures address(es) failed to remove"
exit 0
