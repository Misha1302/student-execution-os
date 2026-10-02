FROM debian:13-slim

# A version family, not one Debian revision: an exact `-0+deb13uN` pin stops
# resolving as soon as Debian ships the next security revision (that broke CI
# before). The 0.4.9 series keeps behaviour stable while security fixes still
# install. Pass an exact version with --build-arg for a fully pinned build.
ARG TOR_VERSION=0.4.9.*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tor="${TOR_VERSION}" \
       netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY torrc /etc/tor/torrc
COPY healthcheck.sh /usr/local/bin/tor-healthcheck
RUN chmod 0755 /usr/local/bin/tor-healthcheck

USER debian-tor
RUN tor --verify-config -f /etc/tor/torrc

ENTRYPOINT ["tor", "-f", "/etc/tor/torrc"]
