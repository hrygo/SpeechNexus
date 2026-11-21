#!/bin/sh
# Copy the key material off the Secret mount (root-owned, 0444) into a private
# copy with 0600, because OpenSSH refuses keys that others can read, then hold
# the tunnel until ssh exits.
set -eu

install -m 600 /etc/speechnexus-ssh/id_ed25519 /tmp/id_ed25519
install -m 600 /etc/speechnexus-ssh/known_hosts /tmp/known_hosts

exec ssh -N -T \
  -i /tmp/id_ed25519 \
  -o IdentitiesOnly=yes \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/tmp/known_hosts \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -o ControlMaster=no \
  -L 0.0.0.0:18010:127.0.0.1:8010 \
  -L 0.0.0.0:18020:127.0.0.1:8022 \
  -L 0.0.0.0:18190:127.0.0.1:18190 \
  -R 127.0.0.1:15440:postgres.speechnexus.svc:5432 \
  -R 127.0.0.1:19100:minio.speechnexus.svc:9000 \
  -p "${SSH_PORT}" "${SSH_USER}@${SSH_HOST}"
