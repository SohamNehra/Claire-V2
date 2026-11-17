#!/bin/bash
# Browser test for the app. Needs Google Chrome and Node 22+.
#
#   tests/ui/run.sh            run the checks
#   tests/ui/run.sh shots      also keep screenshots in tests/ui/shots/
#
# Serves the app against a scripted model, drives it in headless Chrome, and
# cleans up after itself: the demo server and the browser are both stopped
# whether the checks pass, fail or hang, and the script confirms none is left.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/shots"; [ "${1:-}" = "shots" ] || OUT="$(mktemp -d)"
mkdir -p "$OUT"

python3 "$HERE/demo_server.py" 300 >/dev/null 2>&1 &
SERVER=$!
cleanup() {
  kill "$SERVER" 2>/dev/null; wait "$SERVER" 2>/dev/null
  rm -rf "$HOME/.claire-ui-test"
  [ "${1:-}" = "shots" ] || rm -rf "$OUT"
  # Processes take a moment to exit after being signalled; wait up to 3 s
  # before counting, and name anything that is still there.
  for _ in $(seq 1 10); do
    pgrep -f "claire-drive-|tests/ui/demo_server.py" >/dev/null || break
    sleep 0.3
  done
  left=$(pgrep -f "claire-drive-|tests/ui/demo_server.py" | wc -l | tr -d ' ')
  echo "demo server stopped; stray processes: $left"
  [ "$left" = "0" ] || pgrep -fl "claire-drive-|tests/ui/demo_server.py"
}
trap 'cleanup "${1:-}"' EXIT

for _ in $(seq 1 40); do [ -f "$HOME/.claire-ui-test/info.txt" ] && break; sleep 1; done
if [ ! -f "$HOME/.claire-ui-test/info.txt" ]; then echo "demo server did not start"; exit 1; fi
node "$HERE/drive.mjs" "$(head -1 "$HOME/.claire-ui-test/info.txt")" "$OUT" | sed 's/token=[A-Za-z0-9_-]*/token=…/g'
exit "${PIPESTATUS[0]}"
