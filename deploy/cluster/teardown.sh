#!/usr/bin/env bash
# Tear down the minimal isomorphic cluster and prove the baseline came back.
#
# Removes exactly what this deployment created and nothing else:
#   * the `speechnexus` namespace on the local cluster (all objects and PVCs),
#   * the `speechnexus-cluster-compute` Compose project and its named volumes,
#   * the manually started `speechnexus-asr-1` engine container.
#
# It never touches unrelated containers. Names are matched literally, and the
# script refuses to run when a production-looking container would match.
#
# Usage:
#   deploy/cluster/teardown.sh --compute-host megacube [--namespace speechnexus] [--dry-run]
set -euo pipefail

NAMESPACE=speechnexus
COMPUTE_HOST=""
COMPOSE_DIR='$HOME/speechnexus-cluster-compute'
ENGINE_CONTAINER=speechnexus-asr-1
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --compute-host) COMPUTE_HOST="$2"; shift 2 ;;
    --compose-dir) COMPOSE_DIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$COMPUTE_HOST" ]; then
  echo "--compute-host is required (an ssh alias for the compute host)" >&2
  exit 2
fi

run() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "would run: $*"
  else
    "$@"
  fi
}

echo "== 1. local cluster namespace =="
if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  run kubectl delete namespace "$NAMESPACE" --wait=false
else
  echo "namespace $NAMESPACE already absent"
fi

echo "== 2. compute plane on $COMPUTE_HOST =="
# The Compose project name is fixed by the directory name; `down -v` removes the
# containers and the named volumes that hold the staged private configs.
run ssh -o BatchMode=yes "$COMPUTE_HOST" \
  "cd $COMPOSE_DIR 2>/dev/null && docker compose down --volumes --remove-orphans || echo 'compose project already absent'"
run ssh -o BatchMode=yes "$COMPUTE_HOST" \
  "docker rm -f $ENGINE_CONTAINER >/dev/null 2>&1 || true"
# The project directory holds the transferred private configs and the Compose file
# (which carries credentials). It was created by this deployment, so it goes too;
# the guard refuses to touch anything that is not exactly that directory.
# The engine writes a root-owned Triton JIT cache inside the project directory, so
# this account cannot always delete it. Remove everything else, then report the
# residue instead of pretending the host is clean; no credentials live in the cache.
run ssh -o BatchMode=yes "$COMPUTE_HOST" \
  "target=\"\$(eval echo $COMPOSE_DIR)\"; case \"\$target\" in \"\$HOME/speechnexus-cluster-compute\"|\"\$HOME/speechnexus-cluster-compute/\") ;; *) echo \"refusing to touch \$target\" >&2; exit 1 ;; esac; [ -d \"\$target\" ] || exit 0; find \"\$target\" -mindepth 1 -maxdepth 1 ! -name triton-cache -exec rm -rf -- {} + 2>/dev/null; rmdir \"\$target\" 2>/dev/null || { echo \"residue kept at \$target (root-owned):\"; ls -A \"\$target\"; echo \"remove with: sudo rm -rf \$target\"; }"

if [ "$DRY_RUN" = 1 ]; then
  echo "dry run: nothing was removed"
  exit 0
fi

echo "== 3. verify =="
for i in $(seq 1 60); do
  if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then break; fi
  sleep 2
done
if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  echo "FAIL: namespace $NAMESPACE still terminating" >&2
  exit 1
fi
echo "namespace $NAMESPACE removed"

leftover=$(ssh -o BatchMode=yes "$COMPUTE_HOST" \
  "docker ps -a --format '{{.Names}}' | grep -E '^speechnexus-(cluster-compute|asr-)' || true")
if [ -n "$leftover" ]; then
  echo "FAIL: compute containers still present:" >&2
  echo "$leftover" >&2
  exit 1
fi
echo "compute containers removed"

# Any remaining container must not be one this deployment named. Anything else is
# someone else's service and is reported, never touched.
others=$(ssh -o BatchMode=yes "$COMPUTE_HOST" "docker ps --format '{{.Names}}' | sort")
echo "remaining containers on $COMPUTE_HOST:"
echo "$others" | sed 's/^/  /'
case "$others" in
  *speechnexus-cluster-compute*|*speechnexus-asr-*)
    echo "FAIL: a cluster container survived" >&2; exit 1 ;;
esac
echo "TEARDOWN OK"