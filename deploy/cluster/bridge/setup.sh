#!/usr/bin/env bash
# Generate the bridge key pair, pin the MegaCube host key, store both in a Secret,
# and print the single authorized_keys line to append on MegaCube.
# Idempotent: an existing key is kept; known_hosts is re-scanned each run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SSH_DIR="$ROOT/.runtime/cluster/ssh"
HOST=47.103.22.160
PORT=6000
NAMESPACE=speechnexus

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

if [ ! -f "$SSH_DIR/id_ed25519" ]; then
  ssh-keygen -q -t ed25519 -N "" -C "speechnexus-bridge@minimal-cluster" -f "$SSH_DIR/id_ed25519"
fi
chmod 600 "$SSH_DIR/id_ed25519"

ssh-keyscan -p "$PORT" -t ed25519 "$HOST" 2>/dev/null > "$SSH_DIR/known_hosts"
[ -s "$SSH_DIR/known_hosts" ] || { echo "host key scan failed" >&2; exit 1; }

kubectl -n "$NAMESPACE" create secret generic bridge-ssh \
  --from-file=id_ed25519="$SSH_DIR/id_ed25519" \
  --from-file=id_ed25519.pub="$SSH_DIR/id_ed25519.pub" \
  --from-file=known_hosts="$SSH_DIR/known_hosts" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "Secret bridge-ssh applied in namespace $NAMESPACE."
echo "Append exactly this line to MegaCube ~/.ssh/authorized_keys (one line, no sudo):"
echo "no-pty,no-agent-forwarding,no-X11-forwarding,no-user-rc $(cat "$SSH_DIR/id_ed25519.pub")"
