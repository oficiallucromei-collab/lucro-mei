#!/usr/bin/env bash
set -euo pipefail

image="$1"
container_id="$(docker run --detach --network none \
  --env K_SERVICE=lucromei-build-smoke \
  --env PORT=9090 \
  --env LUCROMEI_ENVIRONMENT=production \
  --env 'LUCROMEI_DATABASE_URL=postgresql+psycopg://smoke:smoke@localhost/smoke?sslmode=verify-full' \
  --env LUCROMEI_WHATSAPP_VERIFY_TOKEN=smoke-verify-token \
  --env LUCROMEI_WHATSAPP_APP_SECRET=smoke-app-secret \
  --env LUCROMEI_WHATSAPP_ACCESS_TOKEN=smoke-access-token \
  --env LUCROMEI_WHATSAPP_PHONE_NUMBER_ID=123456789 \
  --env LUCROMEI_TERMS_URL=https://example.invalid/termos \
  "$image")"
trap 'docker rm --force "$container_id" >/dev/null' EXIT

docker exec -i "$container_id" python - <<'PY'
import hmac
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

assert os.getuid() != 0, "The app container must run as a non-root user."


def request(path, data=None, headers=None):
    url = "http://127.0.0.1:9090" + path
    try:
        response = urlopen(Request(url, data=data, headers=headers or {}), timeout=2)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


for _ in range(30):
    try:
        code, body = request("/health")
        if code == 200 and json.loads(body) == {"status": "ok"}:
            break
    except (URLError, TimeoutError):
        pass
    time.sleep(1)
else:
    raise SystemExit("The Cloud Run image did not become healthy.")

assert request("/docs")[0] == 404
assert request("/webhooks/whatsapp", data=b"{}")[0] == 401
body = b'{"object":"whatsapp_business_account","entry":[]}'
signature = hmac.digest(b"smoke-app-secret", body, "sha256").hex()
assert request("/webhooks/whatsapp", body, {"X-Hub-Signature-256": "sha256=" + signature})[0] == 200
challenge = "/webhooks/whatsapp?hub.mode=subscribe&hub.challenge=build-smoke"
assert request(challenge + "&hub.verify_token=incorrect")[0] == 403
assert request(challenge + "&hub.verify_token=smoke-verify-token") == (200, b"build-smoke")
PY
