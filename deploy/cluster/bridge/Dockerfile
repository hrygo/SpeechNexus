# syntax=docker/dockerfile:1.18
# The bridge needs only an SSH client: the tunnel forwards ports, it never runs
# application code. The dedicated uid must exist in /etc/passwd — OpenSSH calls
# getpwuid and exits with "No user exists for uid" when the uid is unknown.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client netcat-openbsd ca-certificates \
    && useradd --uid 10001 --shell /usr/sbin/nologin --no-create-home bridge \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 tunnel.sh /usr/local/bin/tunnel.sh

USER 10001:10001
ENV HOME=/tmp
ENTRYPOINT ["/usr/local/bin/tunnel.sh"]
