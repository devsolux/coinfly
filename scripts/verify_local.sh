#!/usr/bin/env bash
# Runs the full local verification on macOS and records exact evidence under
# build/verification/<timestamp>/ so results can be reported verbatim.
#
#   ./scripts/verify_local.sh            # check + native UI + research suite
#   ./scripts/verify_local.sh --no-ui    # skip the native Tk smoke tests
#
# Nothing here touches application data, credentials or the network beyond
# what ./dev.sh check already does (the test suite forbids Internet sockets).
# Exits non-zero when any recorded step fails.
set -u
cd "$(dirname "$0")/.." || exit 1
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="build/verification/$stamp"
mkdir -p "$out"
run_ui=1
[ "${1:-}" = "--no-ui" ] && run_ui=0
failures=0

record() {
  local name="$1"; shift
  echo "== $name: $*" | tee -a "$out/summary.txt"
  local started; started=$(date +%s)
  "$@" >"$out/$name.log" 2>&1
  local status=$?
  local elapsed=$(( $(date +%s) - started ))
  echo "   exit=$status elapsed=${elapsed}s log=$out/$name.log" | tee -a "$out/summary.txt"
  tail -n 5 "$out/$name.log" | sed 's/^/   | /' | tee -a "$out/summary.txt"
  [ "$status" -eq 0 ] || failures=$((failures + 1))
}

{
  echo "verification $stamp"
  echo "commit: $(git --no-optional-locks rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "python: $(.venv/bin/python --version 2>&1 || echo 'no .venv')"
  echo "host: $(uname -srm)"
} | tee "$out/summary.txt"

record check ./dev.sh check
if [ "$run_ui" = 1 ]; then
  record test-ui ./dev.sh test-ui
fi
record research ./dev.sh test tests/research
echo "== done: failures=$failures summary=$out/summary.txt" | tee -a "$out/summary.txt"
[ "$failures" -eq 0 ]
