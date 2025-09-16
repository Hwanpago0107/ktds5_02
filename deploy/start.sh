#!/usr/bin/env sh
# POSIX sh-compatible entrypoint; no bashisms
set -eu

# Graceful shutdown
trap 'kill -TERM "${UVICORN_PID:-0}" 2>/dev/null || true; kill -TERM "${NGINX_PID:-0}" 2>/dev/null || true; exit 0' TERM INT

# Ensure data dir for app usage
mkdir -p /home/data || true

# Start FastAPI via uvicorn in background
uvicorn app_sms:app --host 0.0.0.0 --port 8000 &
UVICORN_PID=$!

# Start Streamlit UI in background on 8501
streamlit run /app/query.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true \
  --browser.gatherUsageStats false \
  --server.enableCORS false \
  --server.enableXsrfProtection false &
STREAMLIT_PID=$!

# Start nginx (foreground mode) in background so we can supervise both
nginx -g 'daemon off;' &
NGINX_PID=$!

# Portable supervision loop (avoid wait -n which is not in POSIX sh)
while :; do
  if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    echo "uvicorn exited; stopping container" >&2
    kill -TERM "$NGINX_PID" 2>/dev/null || true
    kill -TERM "$STREAMLIT_PID" 2>/dev/null || true
    wait "$NGINX_PID" 2>/dev/null || true
    wait "$STREAMLIT_PID" 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$STREAMLIT_PID" 2>/dev/null; then
    echo "streamlit exited; stopping container" >&2
    kill -TERM "$NGINX_PID" 2>/dev/null || true
    kill -TERM "$UVICORN_PID" 2>/dev/null || true
    wait "$NGINX_PID" 2>/dev/null || true
    wait "$UVICORN_PID" 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$NGINX_PID" 2>/dev/null; then
    echo "nginx exited; stopping container" >&2
    kill -TERM "$UVICORN_PID" 2>/dev/null || true
    kill -TERM "$STREAMLIT_PID" 2>/dev/null || true
    wait "$UVICORN_PID" 2>/dev/null || true
    wait "$STREAMLIT_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 1
done
