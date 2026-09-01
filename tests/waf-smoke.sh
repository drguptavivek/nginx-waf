#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-modsec-proxy-proxy:latest}"
COMPOSE_FILE="${COMPOSE_FILE:-modsec-proxy/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-modsec-proxy/.env.example}"

echo "[1/4] Checking NGINX configuration and dynamic modules"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
  run --rm --no-deps --entrypoint nginx proxy -t

echo "[2/4] Checking module files"
docker run --rm --entrypoint sh "$IMAGE" -ceu '
  test -s /usr/lib/nginx/modules/ngx_http_modsecurity_module.so
  test -s /usr/lib/nginx/modules/ngx_http_headers_more_filter_module.so
  grep -q "ngx_http_modsecurity_module.so" /etc/nginx/nginx.conf
  grep -q "ngx_http_headers_more_filter_module.so" /etc/nginx/nginx.conf
'

echo "[3/4] Starting proxy"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d proxy
trap 'docker compose -f "$COMPOSE_FILE" down' EXIT

echo "[4/4] Checking live HTTP and WAF processing"
ready=0
for _ in $(seq 1 30); do
  if curl --silent --show-error --max-time 2 http://localhost/ -o /dev/null 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
test "$ready" = 1
sleep 5

curl --silent --show-error --max-time 10 -G http://localhost/ \
  --data-urlencode "q=<script>alert(1)</script>" -o /dev/null

status="$(curl --silent --show-error --max-time 10 \
  -A sqlmap -o /dev/null -w '%{http_code}' http://localhost/)"
case "$status" in
  2*|3*) ;;
  *) echo "Unexpected HTTP status from WAF path: $status" >&2; exit 1 ;;
esac

echo "WAF smoke tests passed"
