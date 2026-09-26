#!/bin/sh
set -eu

cookie=/var/lib/tor/control_auth_cookie
[ -r "$cookie" ] || exit 1

cookie_hex="$(od -An -tx1 -v "$cookie" | tr -d ' \n')"
[ -n "$cookie_hex" ] || exit 1

response="$(
  printf 'AUTHENTICATE %s\r\nGETINFO status/bootstrap-phase\r\nQUIT\r\n' "$cookie_hex" \
    | nc -w 2 127.0.0.1 9051
)"

printf '%s\n' "$response" \
  | grep -q '250-status/bootstrap-phase=.*BOOTSTRAP .*PROGRESS=100'
