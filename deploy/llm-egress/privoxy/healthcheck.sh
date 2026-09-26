#!/bin/sh
set -eu

# p.p is Privoxy's internal status URL, so this proves the local HTTP proxy is
# accepting requests without consuming an AI-provider key or requiring Tor/network.
curl --fail --silent --show-error --max-time 2 \
  --proxy http://127.0.0.1:8118 http://p.p/ >/dev/null
