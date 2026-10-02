#!/bin/sh
set -eu

warp-svc &
warp_pid=$!
socat_pid=""
trap 'test -z "$socat_pid" || kill -TERM "$socat_pid" 2>/dev/null || true; kill -TERM "$warp_pid" 2>/dev/null || true; wait' INT TERM

attempt=0
until warp-cli --accept-tos status >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    exit 1
  fi
  sleep 1
done

if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
  warp-cli --accept-tos registration new >/dev/null
fi
warp-cli --accept-tos mode proxy >/dev/null
warp-cli --accept-tos proxy port 40000 >/dev/null
warp-cli --accept-tos connect >/dev/null

socat TCP-LISTEN:40001,reuseaddr,fork,bind=0.0.0.0 TCP:127.0.0.1:40000 &
socat_pid=$!

while kill -0 "$warp_pid" 2>/dev/null && kill -0 "$socat_pid" 2>/dev/null; do
  sleep 5
done
exit 1
