# NGINX ModSecurity WAF

Public multi-architecture NGINX reverse proxy with Jonas Certbot, ModSecurity,
headers-more, and pinned OWASP CRS.

## Versions

- `jonasal/nginx-certbot:6.2.0-nginx1.31.4`
- NGINX `1.31.4`
- ModSecurity `v3.0.16`
- ModSecurity-nginx `v1.0.4`
- OWASP CRS `v4.25.1` LTS

Images target `linux/amd64` and `linux/arm64`.
CRS is Apache-2.0 licensed; see `modsec-proxy/nginx/THIRD-PARTY-NOTICES`.

## Pull and run

```bash
docker pull ghcr.io/drguptavivek/nginx-waf:nginx1.31.4-modsec3.0.16-crs4.25.1
```

```yaml
services:
  proxy:
    image: ghcr.io/drguptavivek/nginx-waf:nginx1.31.4-modsec3.0.16-crs4.25.1
    ports: ["80:80", "443:443"]
    environment:
      CERTBOT_EMAIL: admin@example.org
      STAGING: "1"                 # change to 0 for production
      MODSEC_ENGINE_MODE: DetectionOnly
    volumes:
      - ./letsencrypt:/etc/letsencrypt
      - ./modsec/rules:/etc/modsecurity/rules:ro
      - ./nginx/templates:/etc/nginx/templates:ro
```

Set `MODSEC_ENGINE_MODE` to `On` after tuning. Consumers may replace
`/etc/modsecurity/crs-setup.conf` to set CRS paranoia and thresholds. Mounted
rules are loaded in addition to the pinned CRS rules; use unique IDs.

## ODK Central

Use the image as the `nginx` service replacement. Preserve Jonas’s inherited
entrypoint and mount the ODK-generated template rather than replacing the main
NGINX configuration:

```yaml
services:
  nginx:
    image: ghcr.io/drguptavivek/nginx-waf:v1.0.0
    environment:
      DOMAIN: ${DOMAIN}
      CERTBOT_EMAIL: ${SYSADMIN_EMAIL}
      SSL_TYPE: ${SSL_TYPE:-letsencrypt}
      STAGING: ${STAGING:-0}
      MODSEC_ENGINE_MODE: ${MODSEC_ENGINE_MODE:-DetectionOnly}
    volumes:
      - ./files/nginx/odk.conf.template:/etc/nginx/templates/odk.conf.template:ro
      - ./files/nginx:/usr/share/odk/nginx:ro
      - ./files/local/customssl:/etc/customssl/live/local:ro
      - ./files/modsecurity/rules:/etc/modsecurity/rules:ro
      - ./letsencrypt:/etc/letsencrypt
```

If the ODK setup script generates files under `/usr/share/odk/nginx`, retain
that script and let it invoke Jonas’s normal NGINX/Certbot startup. Do not
mount over `/etc/nginx/nginx.conf` unless intentionally replacing the full
Jonas configuration and entrypoint contract.

## Checks

```bash
docker compose --env-file .env config --quiet
bash tests/waf-smoke.sh modsec-proxy-proxy:latest
```

GitHub Actions runs the amd64 smoke gate, then publishes all four platforms to
public GHCR with BuildKit caching. Create a release tag only after the gate
passes:

```bash
git tag v1.0.0 && git push origin v1.0.0
```
