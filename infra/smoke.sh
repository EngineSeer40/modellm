#!/usr/bin/env bash
set -euo pipefail
image="${1:-modellm:release-smoke}"
container="$(docker run -d --rm --init --read-only --network none -e MODELLM_PROD=true "$image")"
trap 'docker stop -t 10 "$container" >/dev/null' EXIT
for attempt in {1..30}; do
  if docker exec "$container" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=2)" 2>/dev/null; then
    break
  fi
  sleep 1
done
docker exec -i "$container" python - < infra/smoke.py
