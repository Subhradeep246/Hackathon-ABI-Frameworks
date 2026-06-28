#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "Creating venv…"
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r backend/requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

PORT="${PORT:-8000}"
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  echo "Port $PORT in use — stopping existing process…"
  lsof -ti ":$PORT" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo "Starting Pulse on http://127.0.0.1:$PORT"
exec uvicorn backend.api.main:app --host 127.0.0.1 --port "$PORT"
