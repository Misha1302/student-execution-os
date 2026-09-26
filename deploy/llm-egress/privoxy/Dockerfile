FROM debian:13-slim

ARG PRIVOXY_VERSION=4.0.0-2

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       privoxy="${PRIVOXY_VERSION}" \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY config /etc/privoxy/config
COPY healthcheck.sh /usr/local/bin/privoxy-healthcheck
RUN chmod 0755 /usr/local/bin/privoxy-healthcheck \
    && privoxy --config-test /etc/privoxy/config

USER privoxy
ENTRYPOINT ["privoxy", "--no-daemon", "/etc/privoxy/config"]
