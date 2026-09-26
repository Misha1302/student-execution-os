FROM debian:13-slim

ARG TOR_VERSION=0.4.9.12-0+deb13u2

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tor="${TOR_VERSION}" \
       netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY torrc /etc/tor/torrc
COPY healthcheck.sh /usr/local/bin/tor-healthcheck
RUN chmod 0755 /usr/local/bin/tor-healthcheck \
    && tor --verify-config -f /etc/tor/torrc

USER debian-tor
ENTRYPOINT ["tor", "-f", "/etc/tor/torrc"]
