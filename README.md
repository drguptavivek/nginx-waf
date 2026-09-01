# OVERVIEW

* ModSecurity v3 install on Ubuntu 24.04 (note for 22.04),
* NGINX integration (`sites-available`/`sites-enabled`),
* OWASP CRS install + sensible defaults,
* tuning patterns (exclusions/allowlists),
* separate logs for **blocks** vs **detections** (Option A),
* hot store (DuckDB) + viewer,
* cold store (MinIO in Docker) + archive + recall,
* architecture diagram.


---

# ModSecurity 3 + NGINX + Logging + Hot/Cold Storage

This guide sets up **ModSecurity v3** with **NGINX** on Ubuntu, enables **OWASP CRS**, splits logs into **blocks** vs **detections**, and implements a **hot (DuckDB)** → **cold (MinIO/Parquet)** pipeline with an optional Flask viewer.

---

## 0) Architecture (at a glance)

```
              +--------------------+
              |   Internet traffic |
              +----------+---------+
                         |
                         v
+------------------------+-------------------------+
|                    NGINX                        |
|        (ngx_http_modsecurity_module)            |
|   modsecurity on; rules -> ModSecurity (v3)     |
+------------------------+------------------------+
       | audit.json                | error.log
       v                           v
 /var/log/modsec/audit.log     /var/log/nginx/error.log
                 \               /
                  \             /
                   \           /
                    \         /
                   rsyslog split (Option A)
                 /                     \
                v                       v
  /var/log/modsec/detections.log   /var/log/modsec/blocked.log
                   \               /
                    \             /
                     \           /
                     Ingestor (every 30s) -> DuckDB (hot: 6-24h)
                                   |
                                   v
                            Flask viewer (UI)

                Archive (hourly) -> Parquet partitions -> MinIO (Docker)
                                               |
                                               v
                                   Recall/Analysis (DuckDB on MinIO)
```

---



## 1) Install NGINX + ModSecurity v3

### Ubuntu 24.04 (easiest: APT packages)

```bash
sudo apt update
# NGINX with connector + ModSecurity v3 engine
sudo apt install -y nginx libnginx-mod-http-modsecurity libmodsecurity3 git
```

> On 24.04, `libnginx-mod-http-modsecurity` auto-loads the module.
> On **22.04**: `libmodsecurity3` is available; if the connector package isn’t, build the dynamic module against your NGINX version (instructions similar to below, but you won’t need to build libmodsecurity).

### (Only if you need to build connector on 22.04)

```bash
# Build connector module matched to your running nginx version
sudo apt install -y build-essential libpcre3-dev zlib1g-dev
cd /usr/local/src
sudo git clone --depth 1 https://github.com/owasp-modsecurity/ModSecurity-nginx.git
NGINX_VERSION=$(nginx -v 2>&1 | grep -o '[0-9.]\+')
sudo curl -LO http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz
sudo tar -xf nginx-${NGINX_VERSION}.tar.gz
cd nginx-${NGINX_VERSION}
sudo ./configure --with-compat --add-dynamic-module=../ModSecurity-nginx
sudo make modules
sudo cp objs/ngx_http_modsecurity_module.so /usr/lib/nginx/modules/
echo 'load_module modules/ngx_http_modsecurity_module.so;' | sudo tee /etc/nginx/modules-enabled/50-mod-http-modsecurity.conf
```

---

## 2) Base ModSecurity + CRS setup

```bash
# Create ModSecurity directory
sudo mkdir -p /etc/nginx/modsec

# Base ModSecurity config + unicode mapping
sudo wget -O /etc/nginx/modsec/modsecurity.conf \
  https://raw.githubusercontent.com/owasp-modsecurity/ModSecurity/v3/master/modsecurity.conf-recommended
sudo wget -O /etc/nginx/modsec/unicode.mapping \
  https://raw.githubusercontent.com/owasp-modsecurity/ModSecurity/v3/master/unicode.mapping

# Start in DetectionOnly (switch to On after tuning)
sudo sed -i 's/^SecRuleEngine .*/SecRuleEngine DetectionOnly/' /etc/nginx/modsec/modsecurity.conf

# JSON audit logging & sensible audit parts
sudo tee /etc/nginx/modsec/audit.conf >/dev/null <<'CONF'
SecStatusEngine Off
SecAuditEngine RelevantOnly
SecAuditLogType Serial
SecAuditLogFormat JSON
SecAuditLog /var/log/modsec/audit.log
SecAuditLogParts ABCEFHJKZ
CONF

# OWASP CRS v4
cd /etc/nginx/modsec
sudo git clone https://github.com/coreruleset/coreruleset.git crs
sudo cp /etc/nginx/modsec/crs/crs-setup.conf.example /etc/nginx/modsec/crs/crs-setup.conf

# Tuning placeholders
sudo tee /etc/nginx/modsec/exclusions.conf >/dev/null <<'CONF'
# Path-scoped allowlists / rule removals here (examples later)
CONF
sudo tee /etc/nginx/modsec/local_rules.conf >/dev/null <<'CONF'
# Your custom rules here (e.g., bad UAs, IP allowlists/blocks)
CONF

# Aggregate include file ModSecurity will load via nginx
sudo tee /etc/nginx/modsec/main.conf >/dev/null <<'CONF'
Include /etc/nginx/modsec/modsecurity.conf
Include /etc/nginx/modsec/audit.conf
Include /etc/nginx/modsec/crs/crs-setup.conf
Include /etc/nginx/modsec/crs/rules/*.conf
Include /etc/nginx/modsec/exclusions.conf
Include /etc/nginx/modsec/local_rules.conf
CONF
```

---

## 3) Integrate with NGINX sites

### Example site (`/etc/nginx/sites-available/example`)

```nginx
server {
    listen 80;
    server_name example.local;

    root /var/www/html;

    # Enable ModSecurity for this server
    modsecurity on;
    modsecurity_rules_file /etc/nginx/modsec/main.conf;

    location / {
        try_files $uri $uri/ =404;
    }

    # Example: disable response inspection for large downloads
    location /downloads/ {
        modsecurity on;
        modsecurity_rules "
            SecRuleEngine DetectionOnly
            SecResponseBodyAccess Off
        ";
        try_files $uri =404;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/example /etc/nginx/sites-enabled/example
sudo nginx -t
sudo systemctl reload nginx
```

> Tip: You can also enable ModSecurity at the `http` level in `nginx.conf` (applies to all servers), then override per `server`/`location` using `modsecurity on|off`.

---

## 4) Quick verification

```bash
# Trigger a noisy request
curl -A "Mozilla/5.0" "http://example.local/?q=<script>alert(1)</script>"

# Check nginx error & audit logs
sudo tail -n 50 /var/log/nginx/error.log
sudo tail -n 50 /var/log/modsec/audit.log
```

In **DetectionOnly** mode, ModSecurity logs detections but does not block.

To **enable blocking** (after tuning):

```bash
sudo sed -i 's/^SecRuleEngine .*/SecRuleEngine On/' /etc/nginx/modsec/modsecurity.conf
sudo systemctl reload nginx
```

---

## 5) Tuning patterns (exclusions/allowlists)

Edit `/etc/nginx/modsec/exclusions.conf`:

```apache
# A) Skip static assets
SecRule REQUEST_URI "\.(?:css|js|gif|jpe?g|png|webp|svg|ico|woff2?)$" \
    "id:10001,phase:1,pass,nolog,ctl:ruleEngine=Off"

# B) Health checks
SecRule REQUEST_URI "^/healthz$" \
    "id:10002,phase:1,pass,nolog,ctl:ruleEngine=Off"

# C) Allow internal subnet to bypass WAF
SecRule REMOTE_ADDR "^10\.0\." \
    "id:10003,phase:1,pass,nolog,ctl:ruleEngine=Off"

# D) Path-scoped suppression of specific noisy CRS rules
<LocationMatch "^/api/upload$">
    SecRuleRemoveById 942100 941100
</LocationMatch>

# E) Rich text editors that legitimately post angle brackets
<LocationMatch "^/admin/editor/save$">
    SecRuleRemoveById 941100
</LocationMatch>
```

> Always prefer **path-scoped** tuning over global `SecRuleRemoveById`.

---

## 6) Separate logs for **blocks** vs **detections** (Option A)

We’ll keep:

* **Blocks** in a dedicated file (`blocked.log`) by filtering **nginx error.log** lines that contain “Access denied”.
* **Detections (full JSON)** in `detections.log` (copy of `audit.log` stream).

Create directory & permissions:

```bash
sudo mkdir -p /var/log/modsec
sudo chown -R www-data:www-data /var/log/modsec
sudo touch /var/log/modsec/{audit.log,blocked.log,detections.log}
sudo chown www-data:www-data /var/log/modsec/{audit.log,blocked.log,detections.log}
```

Rsyslog splitter: `/etc/rsyslog.d/30-modsec-separation.conf`

```
module(load="imfile")

# --- NGINX error.log: capture only hard blocks (403 interventions) ---
input(type="imfile"
      File="/var/log/nginx/error.log"
      Tag="nginx-error:"
      addMetadata="on"
      ruleset="modsec_blocks")

ruleset(name="modsec_blocks") {
  if $msg contains "ModSecurity: Access denied" then {
    action(type="omfile" file="/var/log/modsec/blocked.log")
  }
  stop
}

# --- ModSecurity audit log: full detections stream (JSON) ---
input(type="imfile"
      File="/var/log/modsec/audit.log"
      Tag="modsec-audit:"
      addMetadata="on"
      ruleset="modsec_detections")

ruleset(name="modsec_detections") {
  action(type="omfile" file="/var/log/modsec/detections.log")
}
```

Enable:

```bash
sudo systemctl restart rsyslog
```

Logrotate (optional) `/etc/logrotate.d/modsecurity`:

```
/var/log/modsec/*.log {
    daily
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    create 0640 www-data www-data
    postrotate
        /bin/systemctl kill -s USR1 nginx.service >/dev/null 2>&1 || true
    endscript
}
```

---

## 7) Hot store (DuckDB) + Flask viewer (optional)

**Schema init** (tables + views for fast joins):

```bash
# If you have db_init.sql in your repo:
duckdb /opt/modsec-alerts/modsec.duckdb -c ".read /opt/modsec-alerts/ingest/db_init.sql"
```

**Ingestor** (runs every 30s; parses `blocked.log` & `audit.log`):

```bash
# systemd unit
sudo tee /etc/systemd/system/modsec-ingest.service >/dev/null <<'UNIT'
[Unit]
Description=Ingest ModSecurity logs into DuckDB

[Service]
Type=oneshot
User=www-data
Group=www-data
WorkingDirectory=/opt/modsec-alerts/ingest
Environment=MODSEC_DB=/opt/modsec-alerts/viewer/modsec.duckdb
Environment=MODSEC_BLOCKED=/var/log/modsec/blocked.log
Environment=MODSEC_AUDIT=/var/log/modsec/audit.log
ExecStart=/opt/modsec-alerts/venv/bin/python ingest.py
UNIT

sudo tee /etc/systemd/system/modsec-ingest.timer >/dev/null <<'TIMER'
[Unit]
Description=Run modsec ingestor every 30 seconds
[Timer]
OnBootSec=5s
OnUnitActiveSec=30s
AccuracySec=1s
[Install]
WantedBy=timers.target
TIMER

sudo systemctl daemon-reload
sudo systemctl enable --now modsec-ingest.timer
```

**Viewer** (run locally; reverse proxy with NGINX if you want):

```bash
cd /opt/modsec-alerts/viewer
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
# http://127.0.0.1:5090
```

---

## 8) Cold store (MinIO in Docker) + archive to Parquet

On a **cold-store VM**:

```bash
sudo mkdir -p /opt/minio
cat >/opt/minio/docker-compose.yml <<'YAML'
version: "3.8"
services:
  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: admin
      MINIO_ROOT_PASSWORD: changeit-please
    ports:
      - "9000:9000"  # S3 API
      - "9001:9001"  # Console
    volumes:
      - minio-data:/data
    restart: unless-stopped
volumes:
  minio-data:
YAML
cd /opt/minio && docker compose up -d
```

Create bucket and users:

```bash
curl -sSL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc && chmod +x /usr/local/bin/mc
mc alias set localminio http://<COLD_VM>:9000 admin changeit-please
mc mb -p localminio/waf-logs

# Policies
cat >/tmp/waf-writer.json <<'JSON'
{"Version":"2012-10-17","Statement":[
{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::waf-logs"]},
{"Effect":"Allow","Action":["s3:PutObject","s3:AbortMultipartUpload"],"Resource":["arn:aws:s3:::waf-logs/*"]}
]}
JSON
cat >/tmp/waf-reader.json <<'JSON'
{"Version":"2012-10-17","Statement":[
{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::waf-logs"]},
{"Effect":"Allow","Action":["s3:GetObject"],"Resource":["arn:aws:s3:::waf-logs/*"]}
]}
JSON
mc admin policy create localminio waf-writer /tmp/waf-writer.json
mc admin policy create localminio waf-reader /tmp/waf-reader.json
mc admin user add localminio web-writer  REPLACE-WRITER-PASS
mc admin user add localminio read-only   REPLACE-READER-PASS
mc admin policy attach localminio waf-writer --user web-writer
mc admin policy attach localminio waf-reader --user read-only
```

Archive job on **web VM** (hourly):

```bash
sudo mkdir -p /var/lib/modsec-archive && sudo chown -R www-data:www-data /var/lib/modsec-archive
sudo tee /etc/systemd/system/modsec-archive.service >/dev/null <<'UNIT'
[Unit]
Description=Archive ModSecurity logs to Parquet + MinIO
[Service]
Type=oneshot
User=www-data
Group=www-data
WorkingDirectory=/opt/modsec-alerts/archive
Environment=DUCK=/opt/modsec-alerts/viewer/modsec.duckdb
Environment=STAGE=/var/lib/modsec-archive
Environment=BUCKET=waf-logs
Environment=MC=/usr/local/bin/mc
Environment=MC_ALIAS=localminio
ExecStart=/opt/modsec-alerts/venv/bin/python archive.py
UNIT

sudo tee /etc/systemd/system/modsec-archive.timer >/dev/null <<'TIMER'
[Unit]
Description=Run modsec archive hourly
[Timer]
OnBootSec=2m
OnUnitActiveSec=1h
AccuracySec=10s
[Install]
WantedBy=timers.target
TIMER

sudo systemctl daemon-reload
sudo systemctl enable --now modsec-archive.timer
```

Archive action (what it does):

* Export rows **older than 6h** from DuckDB to **Parquet**:

  ```
  /var/lib/modsec-archive/schema-version=1/year=YYYY/month=MM/day=DD/blocks.parquet
  /var/lib/modsec-archive/schema-version=1/year=YYYY/month=MM/day=DD/detections.parquet
  ```
* `mc mirror` → upload to `localminio/waf-logs/schema-version=1/...`
* `DELETE` old rows from DuckDB + `VACUUM`.

---

## 9) Recall & analysis (direct on MinIO)

On an **analysis VM** with DuckDB:

```sql
INSTALL httpfs; LOAD httpfs;
SET s3_endpoint='http://<COLD_VM>:9000';
SET s3_url_style='path';
SET s3_access_key_id='read-only';
SET s3_secret_access_key='REPLACE-READER-PASS';

-- Query one day of blocks
SELECT ruleid, count(*) AS c
FROM read_parquet('s3://waf-logs/schema-version=1/year=2025/month=09/day=21/blocks.parquet')
GROUP BY ruleid ORDER BY c DESC LIMIT 20;

-- Query a range (glob)
SELECT count(*) FROM read_parquet('s3://waf-logs/schema-version=1/year=2025/month=09/day=*/blocks.parquet');

-- Investigate URIs with /admin
SELECT ts, ip, uri, ruleid
FROM read_parquet('s3://waf-logs/schema-version=1/**/blocks.parquet')
WHERE uri ILIKE '%/admin%'
ORDER BY ts DESC
LIMIT 200;
```

---

## 10) Security & Ops tips

* Keep the viewer **internal-only** (reverse proxy + auth or VPN).
* Restrict MinIO by firewall; terminate TLS in front if exposed.
* Rotate `web-writer` / `read-only` credentials.
* Start in **DetectionOnly**; tune; then switch to `SecRuleEngine On`.
* Use **path-scoped** tuning to avoid global blind spots.
* If volume is high, run archive every **15–30 min** instead of hourly.

---

## 11) Troubleshooting

* No ModSecurity activity?

  * Confirm `modsecurity on;` and `modsecurity_rules_file` are present in your site/server.
  * `nginx -T | grep -i modsecurity`
* Connector not loaded?

  * On 24.04 APT, it’s auto; on 22.04 manual: ensure `load_module ...modsecurity_module.so`.
* Audit log empty?

  * Verify `SecAuditEngine RelevantOnly` and file permissions on `/var/log/modsec/audit.log`.
* rsyslog not splitting?

  * `sudo journalctl -u rsyslog -f` and re-check `/etc/rsyslog.d/30-modsec-separation.conf`.
* Viewer shows empty?

  * Ensure ingestor has read access to logs and DuckDB path is correct.

---

### Done!
