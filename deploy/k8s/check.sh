#!/usr/bin/env bash
# Checks the deployed demo with real requests: through kubectl port-forward, then from a pod inside the cluster.
# Run after the rollout: deploy/k8s/check.sh. Any failed check stops the script with a non-zero exit.
set -euo pipefail

LOCAL=http://localhost:18000
kubectl port-forward service/agentic-ems 18000:8000 >/dev/null &
forward=$!
trap 'kill "$forward"' EXIT

get() { curl --silent --show-error --fail --retry 10 --retry-connrefused --retry-delay 1 "$@"; }

get --output /dev/null "$LOCAL/"
echo "ok: the page answers 200"

get "$LOCAL/samples/1.json" | grep --quiet '"question": "How much electricity did the whole plant use each day in March 2026?"'
echo "ok: the first recorded answer comes back"

get "$LOCAL/api/health" | grep --quiet '"has_key":false'
echo "ok: no API key, recorded answers only"

status=$(curl --silent --output /dev/null --write-out '%{http_code}' --header 'Host: evil.example' "$LOCAL/api/health")
[ "$status" = 400 ] || { echo "a host not on the list got $status, not 400" >&2; exit 1; }
echo "ok: a host not on the list gets 400"

# The same image has Python, so no second image is pulled; urlopen raises on any status but 2xx.
kubectl run "check-$RANDOM" --rm --attach --quiet --restart=Never \
  --image=agentic-ems:local --image-pull-policy=IfNotPresent --command -- python -c '
from urllib.request import urlopen
for name in ("agentic-ems", "agentic-ems.default.svc.cluster.local"):
    with urlopen(f"http://{name}:8000/api/health", timeout=10) as response:
        print(f"ok: {name} answers {response.status} inside the cluster")
'
