#!/usr/bin/env bash
# The harness writes companies, branches and mappings, so it needs a database
# of its own. It also needs the server it talks to to be the one it started:
# an orphan from a previous run answered /health on the same port once and the
# harness spent a while asserting against someone else's data.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
REPO="$(cd ../.. && pwd)"
DB="$(mktemp -u /tmp/domharness-XXXXXX.duckdb)"
PORT="${PORT:-0}"
if [ "$PORT" = "0" ]; then
  PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
fi
cd "$REPO"
SOP_DB_PATH="$DB" python3 -m uvicorn web.app:app \
  --host 127.0.0.1 --port "$PORT" > /tmp/domharness.log 2>&1 &
SRV=$!
cleanup(){ kill "$SRV" 2>/dev/null || true; wait "$SRV" 2>/dev/null || true; rm -f "$DB"; }
trap cleanup EXIT
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$PORT/health" > /dev/null && break
  kill -0 "$SRV" 2>/dev/null || { echo "server died:"; tail -5 /tmp/domharness.log; exit 1; }
  sleep 0.5
done
# Prove the server is ours and the store is empty before a single assertion.
FRESH="$(curl -sf "http://127.0.0.1:$PORT/api/config" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["entities"]))')"
if [ "$FRESH" != "0" ]; then
  echo "refusing to run: the store on port $PORT already holds $FRESH companies"
  exit 1
fi
# node runs from the harness directory so it finds the local jsdom install
cd "$HERE"
BASE="http://127.0.0.1:$PORT" node render.mjs "$@"
