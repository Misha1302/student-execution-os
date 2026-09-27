#!/bin/sh
# Provision a dedicated Ubuntu/Debian host as a private HTTP CONNECT egress
# for Student Execution OS LLM traffic.
#
# Usage:
#   sudo sh setup-egress-host.sh <SEOS_PRODUCTION_PUBLIC_IPV4> [allowed-host ...]
#
# Security invariants:
# - only the supplied production /32 can reach TCP 3128;
# - only CONNECT to TCP 443 is allowed;
# - destinations are exact DNS hostnames (no wildcard / suffix ACLs);
# - Squid never intercepts TLS and never caches tunneled traffic.
set -eu

usage() {
  echo "usage: setup-egress-host.sh <SEOS_PRODUCTION_PUBLIC_IPV4> [allowed-host ...]" >&2
  exit 2
}

[ "$#" -ge 1 ] || usage
SOURCE_IP=$1
shift
[ "$#" -gt 0 ] || set -- api.groq.com

is_ipv4() {
  printf '%s\n' "$1" | awk -F. '
    NF != 4 { exit 1 }
    {
      for (i = 1; i <= 4; i++) {
        if ($i !~ /^[0-9]+$/ || $i < 0 || $i > 255) exit 1
      }
      exit 0
    }'
}

is_exact_hostname() {
  [ "${#1}" -le 253 ] || return 1
  printf '%s\n' "$1" | awk -F. '
    NF < 1 { exit 1 }
    {
      for (i = 1; i <= NF; i++) {
        if (length($i) < 1 || length($i) > 63) exit 1
        if ($i !~ /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$/) exit 1
      }
      exit 0
    }'
}

is_ipv4 "$SOURCE_IP" || {
  echo "source must be one valid IPv4 address" >&2
  exit 2
}

for host in "$@"; do
  is_exact_hostname "$host" || {
    echo "allowed host must be one exact DNS hostname: $host" >&2
    exit 2
  }
done

[ "$(id -u)" -eq 0 ] || {
  echo "must run as root" >&2
  exit 2
}

export DEBIAN_FRONTEND=noninteractive

# Put the host firewall in place before Squid is installed, so package startup never
# creates a window where the proxy is reachable from arbitrary IPv4 sources.
apt-get update -q
apt-get install -y -q --no-install-recommends iptables iptables-persistent

CHAIN=SEOS_SQUID
iptables -N "$CHAIN" 2>/dev/null || true
iptables -F "$CHAIN"
iptables -A "$CHAIN" -p tcp -s "${SOURCE_IP}/32" --dport 3128 -j ACCEPT
iptables -A "$CHAIN" -p tcp --dport 3128 -j DROP

while iptables -C INPUT -p tcp --dport 3128 -j "$CHAIN" 2>/dev/null; do
  iptables -D INPUT -p tcp --dport 3128 -j "$CHAIN"
done
iptables -I INPUT 1 -p tcp --dport 3128 -j "$CHAIN"
netfilter-persistent save

apt-get install -y -q --no-install-recommends squid
[ -e /etc/squid/squid.conf.dist ] || cp /etc/squid/squid.conf /etc/squid/squid.conf.dist

HOSTS=$(printf '%s ' "$@" | sed 's/[[:space:]]*$//')
cat >/etc/squid/squid.conf <<EOF
# Managed by student-execution-os deploy/llm-egress/squid/setup-egress-host.sh
# Bind IPv4 only; the source ACL and host firewall are intentionally IPv4 /32 scoped.
http_port 0.0.0.0:3128

acl seos_server src ${SOURCE_IP}/32
acl SSL_ports port 443
acl CONNECT method CONNECT
# No leading dot and no wildcard: exact provider hostnames only.
acl llm_hosts dstdomain -n ${HOSTS}

http_access deny !seos_server
http_access allow CONNECT SSL_ports llm_hosts
http_access deny all

cache deny all
cache_mem 8 MB
forwarded_for delete
httpd_suppress_version_string on
# CONNECT tunnels expose only client, host:443, status and byte counts to Squid.
access_log daemon:/var/log/squid/access.log squid
coredump_dir /var/spool/squid
shutdown_lifetime 3 seconds
EOF

squid -k parse
systemctl enable squid
systemctl restart squid
systemctl is-enabled squid
systemctl is-active squid
squid -v | head -n 1
