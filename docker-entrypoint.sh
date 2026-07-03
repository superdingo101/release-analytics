#!/usr/bin/env sh
set -eu

: "${DB_PATH:=/data/release_analytics.sqlite3}"
: "${RUN_INTERVAL_SECONDS:=21600}"
: "${COLLECT_ON_START:=true}"
: "${STREAMLIT_SERVER_PORT:=8501}"
: "${STREAMLIT_SERVER_ADDRESS:=0.0.0.0}"

export DB_PATH RUN_INTERVAL_SECONDS COLLECT_ON_START STREAMLIT_SERVER_PORT STREAMLIT_SERVER_ADDRESS

run_collector_loop() {
  python scheduler.py || true
}

case "${1:-web}" in
  web)
    mkdir -p "$(dirname "$DB_PATH")"
    run_collector_loop &
    exec streamlit run dashboard.py \
      --server.address="${STREAMLIT_SERVER_ADDRESS}" \
      --server.port="${STREAMLIT_SERVER_PORT}" \
      --server.headless=true \
      --browser.gatherUsageStats=false
    ;;
  collect)
    shift
    exec python collector.py "$@"
    ;;
  import-history)
    shift
    exec python import_history.py "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
