#!/usr/bin/env bash
set -u

node /app/dist-server/index.js &
control_plane_pid=$!
python -u /app/ultimate_hosting_bot_fixed.py &
telegram_worker_pid=$!

shutdown_children() {
  trap - TERM INT
  kill -TERM "$control_plane_pid" "$telegram_worker_pid" 2>/dev/null || true
  wait "$control_plane_pid" 2>/dev/null || true
  wait "$telegram_worker_pid" 2>/dev/null || true
}

handle_signal() {
  shutdown_children
  exit 0
}
trap handle_signal TERM INT

set +e
wait -n "$control_plane_pid" "$telegram_worker_pid"
status=$?
set -e

printf '%s\n' "A service process exited (status ${status}); stopping the remaining process."
shutdown_children
exit "$status"
