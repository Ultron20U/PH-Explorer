#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
URL="http://127.0.0.1:${PORT:-8765}"
echo "Starting PH-Explorer on $URL ..."
if command -v xdg-open >/dev/null 2>&1; then
  (sleep 1 && xdg-open "$URL") >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
  (sleep 1 && open "$URL") >/dev/null 2>&1 &
fi
exec python3 server.py
