#!/bin/sh
# Turn a fresh Ubuntu/Debian VM into a private LLM egress proxy (Squid, HTTP CONNECT).
#
#   sudo sh setup-egress-host.sh <SEOS_PRODUCTION_PUBLIC_IP> [allowed-host ...]
#
# Only the given /32 may use the proxy, only CONNECT to port 443 of the exact
# allowlisted hosts is permitted, nothing is cached and TLS is never intercepted:
# the API's TLS session (and its Authorization header) terminates at the provider.
# Re-running is safe; the previous squid.conf is kept once as squid.conf.dist.
set -eu

SOURCE_IP=${1:?usage: setup-egress-host.sh <SEOS_PRODUCTION_PUBLIC_IP> [allowed-host ...]}
shift
[ "$#" -gt 0 ] || set -- api.groq.com
case "$SOURCE_IP" in
  *[!0-9.]* | "" ) echo "source must be a single IPv4 address" >&2; exit 2 ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends squid

[ -e /etc/squid/squid.conf.dist ] || cp /etc/squid/squid.conf /etc/squid/squid.conf.dist
cat >/etc/squid/squid.conf <<EOF
# Managed by student-execution-os deploy/llm-egress/squid/setup-egress-host.sh
http_port 3128

acl seos_server src ${SOURCE_IP}/32
acl SSL_ports port 443
acl CONNECT method CONNECT
# No leading dot: exact hosts only. -n: never reverse-resolve IP-literal targets.
acl llm_hosts dstdomain -n $*

http_access deny !seos_server
http_access allow CONNECT SSL_ports llm_hosts
http_access deny all

cache deny all
cache_mem 8 MB
forwarded_for delete
httpd_suppress_version_string on
# Tunnels log only client, CONNECT host:443, status and byte counts.
access_log daemon:/var/log/squid/access.log squid
coredump_dir /var/spool/squid
shutdown_lifetime 3 seconds
EOF
squid -k parse
systemctl enable squid
systemctl restart squid

# Host firewall. Oracle's Ubuntu images ship iptables-persistent with a final
# INPUT REJECT; insert the one allowed source ahead of it and drop everyone else.
if command -v netfilter-persistent >/dev/null 2>&1; then
  while iptables -C INPUT -p tcp --dport 3128 -j DROP 2>/dev/null; do
    iptables -D INPUT -p tcp --dport 3128 -j DROP
  done
  iptables -S INPUT | grep -- '--dport 3128 .*ACCEPT' | sed 's/^-A /-D /' | while read -r rule; do
    # shellcheck disable=SC2086
    iptables $rule
  done
  iptables -I INPUT 1 -p tcp -s "${SOURCE_IP}/32" --dport 3128 -j ACCEPT
  iptables -I INPUT 2 -p tcp --dport 3128 -j DROP
  netfilter-persistent save
elif command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH
  ufw allow proto tcp from "${SOURCE_IP}" to any port 3128
  ufw --force enable
else
  echo "no persistent host firewall found; restrict 3128 manually" >&2
  exit 3
fi

squid -v | head -n 1
systemctl is-enabled squid
systemctl is-active squid
