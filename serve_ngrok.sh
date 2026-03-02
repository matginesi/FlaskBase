#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-5000}"
APP_CMD="${APP_CMD:-python cli.py serve}"
STARTUP_TIMEOUT_SEC="${STARTUP_TIMEOUT_SEC:-45}"
NGROK_API_URL="${NGROK_API_URL:-http://127.0.0.1:4040/api/tunnels}"
APP_ENV_VALUE="$(printf '%s' "${APP_ENV:-${FLASK_ENV:-${ENV:-}}}" | tr '[:upper:]' '[:lower:]')"

if [[ "$APP_ENV_VALUE" == "production" || "$APP_ENV_VALUE" == "prod" ]]; then
  echo "Bloccato: serve_ngrok.sh non può essere eseguito con APP_ENV=production."
  exit 1
fi

# deps
command -v ngrok >/dev/null 2>&1 || {
  echo "ngrok non trovato nel PATH."
  exit 1
}
command -v nc >/dev/null 2>&1 || {
  echo "nc (netcat) non trovato nel PATH."
  exit 1
}

# start app stack
$APP_CMD &
FLASK_PID=$!
NGROK_PID=""

cleanup() {
  set +e
  if [[ -n "${NGROK_PID:-}" ]]; then
    kill "$NGROK_PID" 2>/dev/null || true
  fi
  if kill -0 "$FLASK_PID" 2>/dev/null; then
    kill -TERM "$FLASK_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if ! kill -0 "$FLASK_PID" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    kill -KILL "$FLASK_PID" 2>/dev/null || true
  fi
}
trap cleanup INT TERM

# wait for port
deadline=$((SECONDS + STARTUP_TIMEOUT_SEC))
while ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; do
  if ! kill -0 "$FLASK_PID" 2>/dev/null; then
    echo "Il processo app si e' chiuso prima dell'avvio."
    exit 1
  fi
  if (( SECONDS >= deadline )); then
    echo "Timeout avvio app su 127.0.0.1:${PORT}."
    exit 1
  fi
  sleep 0.2
done

echo "App started on http://127.0.0.1:${PORT}"

# start ngrok
ngrok http "$PORT" >/tmp/ngrok.log 2>&1 &
NGROK_PID=$!

# fetch public url from ngrok API (retry)
URL=""
for _ in $(seq 1 40); do
  JSON="$(curl -s "$NGROK_API_URL" || true)"
  URL="$(printf '%s' "$JSON" | python -c 'import json,sys; data=json.load(sys.stdin); ts=data.get("tunnels",[]); print(next((t.get("public_url","") for t in ts if t.get("public_url","").startswith("https://")), ""))' 2>/dev/null || true)"
  if [[ -n "${URL:-}" ]]; then
    break
  fi
  sleep 0.25
done

if [[ -z "${URL:-}" ]]; then
  echo "ngrok did not start correctly. Logs:"
  tail -n 80 /tmp/ngrok.log || true
  echo "Flask is still running on http://127.0.0.1:$PORT"
  wait "$FLASK_PID"
  exit 0
fi

echo "Public URL: $URL"
echo "Press CTRL+C to stop"

wait "$FLASK_PID"
