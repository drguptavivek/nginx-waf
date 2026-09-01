# ModSecurity v3 + NGINX + DuckDB (hot) + MinIO (cold) — Docker Stack

This turns the README into a runnable Docker setup with:

* **nginx+modsecurity v3 + OWASP CRS** (reverse proxy)
* **Single audit log → hot store (DuckDB)** via a Python ingester that also categorizes **blocks vs detections**
* **Cold store (MinIO S3)** with a small **archiver** job exporting Parquet
* **Optional Flask viewer** for the last 6 hours & rule details

> Folder layout

```
modsec-docker/
├─ docker-compose.yml
├─ .env.example
├─ nginx/
│  ├─ Dockerfile
│  ├─ nginx.conf
│  ├─ modsecurity.conf
│  ├─ crs-setup.conf
│  ├─ rules/
│  │  ├─ REQUEST-900-EXCLUSION-RULES-BEFORE-CRS.conf.example
│  │  ├─ RESPONSE-999-EXCLUSION-RULES-AFTER-CRS.conf.example
│  │  └─ local-exclusions.conf
├─ hot/
│  ├─ log_ingest.py
│  └─ requirements.txt
├─ archiver/
│  ├─ archive_to_minio.py
│  └─ requirements.txt
└─ viewer/
   ├─ Dockerfile
   ├─ requirements.txt
   └─ app.py
```

---

## 1) docker-compose.yml

```yaml
version: "3.9"

services:
  nginx:
    build: ./nginx
    container_name: modsec-nginx
    ports:
      - "80:80"
    environment:
      - NGINX_WORKER_PROCESSES=auto
    volumes:
      - ./data/logs:/var/log/modsecurity
    depends_on:
      - minio
    restart: unless-stopped

  hot_ingest:
    image: python:3.11-slim
    container_name: modsec-hot-ingest
    working_dir: /app
    command: ["python","/app/log_ingest.py"]
    volumes:
      - ./hot:/app:ro
      - ./data/logs:/logs:ro
      - ./data/duckdb:/duckdb
    environment:
      - DUCKDB_PATH=/duckdb/modsec.duckdb
      - AUDIT_LOG=/logs/audit.json
      - TAIL_FROM_END=true
    restart: unless-stopped

  viewer:
    build: ./viewer
    container_name: modsec-viewer
    environment:
      - DUCKDB_PATH=/duckdb/modsec.duckdb
    volumes:
      - ./data/duckdb:/duckdb:ro
    ports:
      - "8080:8080"
    depends_on:
      - hot_ingest
    restart: unless-stopped

  minio:
    image: minio/minio:RELEASE.2025-09-12T00-00-00Z
    container_name: modsec-minio
    command: server /data --console-address ":9001"
    environment:
      - MINIO_ROOT_USER=${MINIO_ROOT_USER}
      - MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
    volumes:
      - ./data/minio:/data
    ports:
      - "9000:9000" # S3 API (bind to intranet only at host firewall)
      - "9001:9001" # Console (bind to intranet only at host firewall)
    restart: unless-stopped

  archiver:
    image: python:3.11-slim
    container_name: modsec-archiver
    working_dir: /app
    command: ["python","/app/archive_to_minio.py"]
    volumes:
      - ./archiver:/app:ro
      - ./data/duckdb:/duckdb:ro
    environment:
      - DUCKDB_PATH=/duckdb/modsec.duckdb
      - S3_ENDPOINT=http://minio:9000
      - S3_BUCKET=modsec-archive
      - S3_ACCESS_KEY=${MINIO_ROOT_USER}
      - S3_SECRET_KEY=${MINIO_ROOT_PASSWORD}
      - ARCHIVE_EVERY_N_MINUTES=1440
    depends_on:
      - minio
      - hot_ingest
    restart: unless-stopped

volumes:
  # Using bind-mounts above to keep data on host
  # Define named volumes here if you prefer
```

> Create the data dirs:

```
mkdir -p data/logs data/duckdb data/minio
```

---

## 2) .env.example

```bash
# Copy to .env and change in production
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin123
```

---

## 3) nginx/Dockerfile

> Builds NGINX + ModSecurity v3 + Connector from source (Ubuntu base), enables CRS, JSON audit logs

```dockerfile
FROM ubuntu:24.04 AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    git build-essential autoconf automake libtool pkg-config \
    libpcre3-dev libxml2-dev libyajl-dev zlib1g-dev libcurl4-openssl-dev \
    libgeoip-dev liblmdb-dev libmaxminddb-dev ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

# --- Build libmodsecurity (v3) ---
WORKDIR /opt
RUN git clone --depth 1 https://github.com/SpiderLabs/ModSecurity.git \
 && cd ModSecurity \
 && git submodule update --init --recursive \
 && ./build.sh \
 && ./configure \
 && make -j"$(nproc)" \
 && make install

# --- Build NGINX with ModSecurity connector ---
ARG NGINX_VER=1.31.4
WORKDIR /opt
RUN git clone --depth 1 https://github.com/SpiderLabs/ModSecurity-nginx.git
RUN wget http://nginx.org/download/nginx-${NGINX_VER}.tar.gz \
 && tar xzf nginx-${NGINX_VER}.tar.gz \
 && cd nginx-${NGINX_VER} \
 && ./configure --with-compat --with-http_ssl_module --with-http_v2_module \
      --add-module=../ModSecurity-nginx \
 && make -j"$(nproc)" \
 && make install

# Runtime image
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# Minimal runtime deps
RUN apt-get update && apt-get install -y ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy NGINX & ModSecurity artifacts
COPY --from=build /usr/local/nginx /usr/local/nginx
COPY --from=build /usr/local/lib/libmodsecurity.so* /usr/local/lib/
RUN ldconfig

# Add CRS
WORKDIR /etc/modsecurity
RUN mkdir -p /etc/nginx /var/log/modsecurity /etc/modsecurity/rules

# OWASP CRS
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/* \
 && git clone --depth 1 https://github.com/coreruleset/coreruleset.git /opt/coreruleset \
 && cp -r /opt/coreruleset/rules /etc/modsecurity/ \
 && cp /opt/coreruleset/crs-setup.conf.example /etc/modsecurity/crs-setup.conf

# Copy local configs
COPY nginx.conf /etc/nginx/nginx.conf
COPY modsecurity.conf /etc/modsecurity/modsecurity.conf
COPY crs-setup.conf /etc/modsecurity/crs-setup.conf
COPY rules/ /etc/modsecurity/rules/

# Log dir
VOLUME ["/var/log/modsecurity"]

EXPOSE 80

CMD ["/usr/local/nginx/sbin/nginx","-g","daemon off;"]
```

---

## 4) nginx/nginx.conf

```nginx
user  root;  # containerized
worker_processes  auto;

error_log  /usr/local/nginx/logs/error.log warn;
pid        /usr/local/nginx/logs/nginx.pid;

events { worker_connections  1024; }

http {
    include       mime.types;
    default_type  application/octet-stream;

    sendfile        on;
    keepalive_timeout  65;

    # ---- ModSecurity on (global) ----
    modsecurity on;
    modsecurity_rules_file /etc/modsecurity/modsecurity.conf;

    # Access log (standard nginx)
    access_log /usr/local/nginx/logs/access.log;

    # Simple reverse proxy example
    server {
        listen 80 default_server;
        server_name _;

        # Health
        location = /_/health { return 200 'ok'; }

        # Upstream demo (replace with your app)
        location / {
            proxy_pass http://httpbin.org/anything; # replace in prod
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
}
```

---

## 5) nginx/modsecurity.conf

> JSON audit logging enabled. CRS loaded. Conservative paranoia Level 1.

```apache
# Core engine
SecRuleEngine On

# Request body handling
SecRequestBodyAccess On
SecRequestBodyLimit 13107200
SecRequestBodyNoFilesLimit 131072
SecRequestBodyInMemoryLimit 131072
SecRequestBodyLimitAction Reject

# Response body handling (usually off for perf)
SecResponseBodyAccess Off

# Audit log (single JSON file)
SecAuditEngine RelevantOnly
SecAuditLogFormat JSON
SecAuditLog /var/log/modsecurity/audit.json
SecAuditLogType Serial
SecAuditLogParts ABIJDEFHZ

# Log only relevant statuses (keep broad; split at ingest)
SecAuditLogRelevantStatus "^(?:5|4)"

# CRS include
Include "/etc/modsecurity/crs-setup.conf"
Include "/etc/modsecurity/rules/*.conf"
Include "/etc/modsecurity/rules/*.conf.disabled"
Include "/etc/modsecurity/rules/REQUEST-*.conf"
Include "/etc/modsecurity/rules/RESPONSE-*.conf"

# Anomaly thresholds (blocking)
SecAction \
  "id:900110,phase:1,nolog,pass,t:none,\
   setvar:tx.inbound_anomaly_score_threshold=5,\
   setvar:tx.outbound_anomaly_score_threshold=4"

# Example local allowlist placeholder
# Include "/etc/modsecurity/rules/local-exclusions.conf"
```

---

## 6) nginx/crs-setup.conf

> Minimal, sane defaults; adjust as needed.

```apache
# Base CRS setup (copied from example, trimmed)
# Paranoia Level
SecAction "id:900000,phase:1,nolog,pass,t:none,setvar:tx.paranoia_level=1"

# Executing paranoia level in phase 2 as well
SecAction "id:900001,phase:2,nolog,pass,t:none,setvar:tx.paranoia_level=1"

# Enforce blocking by anomaly score (already set in modsecurity.conf)
```

> `rules/local-exclusions.conf` (optional)

```apache
# Example: allow health endpoint
<LocationMatch "^/_/health$">
    SecRuleEngine Off
</LocationMatch>
```

---

## 7) hot/log\_ingest.py

> Tail the JSON audit log; parse into DuckDB; split **blocks** vs **detections** by action field.

```python
import json, os, time, duckdb, pathlib

DUCKDB_PATH = os.environ.get("DUCKDB_PATH", "/duckdb/modsec.duckdb")
AUDIT_LOG = os.environ.get("AUDIT_LOG", "/logs/audit.json")
TAIL_FROM_END = os.environ.get("TAIL_FROM_END", "true").lower() == "true"

pathlib.Path(DUCKDB_PATH).parent.mkdir(parents=True, exist_ok=True)

con = duckdb.connect(DUCKDB_PATH)
con.execute(
    """
    CREATE TABLE IF NOT EXISTS detections (
      ts TIMESTAMP,
      txid TEXT,
      client_ip TEXT,
      host TEXT,
      uri TEXT,
      method TEXT,
      status INTEGER,
      rule_id TEXT,
      message TEXT,
      severity TEXT,
      phase INTEGER,
      action TEXT,
      raw JSON
    );
    """
)
con.execute(
    """
    CREATE TABLE IF NOT EXISTS blocks AS
      SELECT * FROM detections WHERE 1=0;
    """
)
con.execute("CREATE INDEX IF NOT EXISTS idx_det_ts ON detections(ts);")
con.execute("CREATE INDEX IF NOT EXISTS idx_blk_ts ON blocks(ts);")


def pick(v, *keys):
    cur = v
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def classify_action(msg):
    # ModSec JSON has 'transaction' + 'messages' array; each message has 'details' with 'action'
    # Treat actions with 'Intervention' or 'disruptive' as blocks
    a = pick(msg, 'details', 'action') or ''
    a_low = str(a).lower()
    is_block = ('intervention' in a_low) or ('disrupt' in a_low) or ('blocked' in a_low)
    return ('block' if is_block else 'detect'), a


def parse_and_insert(line):
    try:
        j = json.loads(line)
    except Exception:
        return

    tx = j.get('transaction', {})
    ts = pick(tx, 'time')
    txid = pick(tx, 'id')
    client_ip = pick(tx, 'client_ip')
    host = pick(tx, 'host_ip') or pick(tx, 'host')
    uri = pick(tx, 'request_uri')
    method = pick(tx, 'method')
    status = pick(tx, 'response_code')

    msgs = j.get('messages') or []
    rows = []
    for m in msgs:
        rule_id = pick(m, 'details', 'ruleId')
        message = m.get('message')
        severity = pick(m, 'details', 'severity')
        phase = pick(m, 'details', 'phase')
        kind, action = classify_action(m)
        rows.append((ts, txid, client_ip, host, uri, method, status, rule_id, message, severity, phase, action, json.dumps(j)))

    if not rows:
        return

    con.executemany("INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # Move any rows that represent blocks into blocks table
    con.execute("INSERT INTO blocks SELECT * FROM detections WHERE txid = ? AND action = 'block'", [txid])


with open(AUDIT_LOG, 'a+', buffering=1) as f:
    f.seek(0, os.SEEK_END)

while True:
    try:
        with open(AUDIT_LOG, 'r', encoding='utf-8', errors='ignore') as f:
            if TAIL_FROM_END:
                f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                parse_and_insert(line)
    except FileNotFoundError:
        time.sleep(1)
```

`hot/requirements.txt`

```text
duckdb==1.1.0
```

---

## 8) archiver/archive\_to\_minio.py

> Every N minutes: export yesterday’s detections/blocks to Parquet and push to MinIO (S3 API).

```python
import os, time, duckdb, datetime as dt
import pyarrow as pa
import pyarrow.parquet as pq
import boto3

DUCKDB = os.environ.get('DUCKDB_PATH', '/duckdb/modsec.duckdb')
ENDPOINT = os.environ.get('S3_ENDPOINT', 'http://minio:9000')
BUCKET = os.environ.get('S3_BUCKET', 'modsec-archive')
AK = os.environ.get('S3_ACCESS_KEY')
SK = os.environ.get('S3_SECRET_KEY')
EVERY_MIN = int(os.environ.get('ARCHIVE_EVERY_N_MINUTES', '1440'))

s3 = boto3.client('s3', endpoint_url=ENDPOINT, aws_access_key_id=AK, aws_secret_access_key=SK)

con = duckdb.connect(DUCKDB, read_only=True)

# Ensure bucket exists
try:
    s3.head_bucket(Bucket=BUCKET)
except Exception:
    s3.create_bucket(Bucket=BUCKET)

while True:
    try:
        today = dt.date.today()
        day = today - dt.timedelta(days=1)
        start = dt.datetime.combine(day, dt.time.min)
        end = dt.datetime.combine(day, dt.time.max)

        for table in ("detections","blocks"):
            q = f"""
            SELECT * FROM {table}
            WHERE ts BETWEEN ? AND ?
            ORDER BY ts
            """
            df = con.execute(q, [start, end]).df()
            if df.empty:
                continue
            table_path = f"{table}/dt={day.isoformat()}/{table}-{day.isoformat()}.parquet"
            # Write local parquet
            os.makedirs('/tmp/export', exist_ok=True)
            local = f"/tmp/export/{table}-{day.isoformat()}.parquet"
            table_pa = pa.Table.from_pandas(df, preserve_index=False)
            pq.write_table(table_pa, local)
            # Upload
            s3.upload_file(local, BUCKET, table_path)
            print(f"Uploaded {table_path}")
    except Exception as e:
        print("Archive error:", e)
    time.sleep(EVERY_MIN * 60)
```

`archiver/requirements.txt`

```text
boto3
pyarrow
pandas
duckdb
```

---

## 9) viewer (optional)

`viewer/Dockerfile`

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8080
CMD ["python","app.py"]
```

`viewer/requirements.txt`

```text
flask
pandas
duckdb
```

`viewer/app.py`

```python
from flask import Flask, request, jsonify
import duckdb, os, datetime as dt

app = Flask(__name__)
DB = os.environ.get('DUCKDB_PATH','/duckdb/modsec.duckdb')

@app.get('/api/last6h')
def last6h():
    con = duckdb.connect(DB, read_only=True)
    now = dt.datetime.utcnow()
    start = now - dt.timedelta(hours=6)
    q = """
      SELECT ts, txid, client_ip, host, method, uri, status, rule_id, message, severity, phase, action
      FROM detections
      WHERE ts BETWEEN ? AND ?
      ORDER BY ts DESC
      LIMIT 2000
    """
    rows = con.execute(q, [start, now]).fetchall()
    cols = [d[0] for d in con.description]
    return jsonify([dict(zip(cols, r)) for r in rows])

@app.get('/api/rule/<rule_id>')
def rule(rule_id):
    con = duckdb.connect(DB, read_only=True)
    q = """
      SELECT ts, txid, client_ip, method, uri, status, message, action
      FROM detections WHERE rule_id = ? ORDER BY ts DESC LIMIT 200
    """
    rows = con.execute(q, [rule_id]).fetchall()
    cols = [d[0] for d in con.description]
    return jsonify([dict(zip(cols, r)) for r in rows])

@app.get('/')
def home():
    return "ModSecurity Hot Viewer API: /api/last6h, /api/rule/<id>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
```

---

## 10) Bring-up & basics

```bash
# 0) prepare
cp .env.example .env
mkdir -p data/logs data/duckdb data/minio

# 1) build & run
docker compose up -d --build

# 2) test the proxy (gets inspected/blocked by ModSecurity)
curl -i http://localhost/

# 3) viewer (JSON)
curl -s http://localhost:8080/api/last6h | jq .

# 4) MinIO console → http://<your-host>:9001  (restrict at firewall to intranet)
```

---

## 11) Notes & tuning

* **Splitting blocks vs detections**: We keep a *single* JSON audit log (`audit.json`). The ingester classifies any message with an **intervention/disruptive** action as a **block**, pushing it into the `blocks` table. Both remain in `detections` for full fidelity.
* **NGINX upstream**: Replace the demo `proxy_pass` with your upstream(s). If you have multiple `server{}` blocks, keep `modsecurity on;` at the `http{}` level or per `server{}`.
* **Exclusions / allowlists**: add to `rules/local-exclusions.conf` and include it.
* **Console exposure**: bind/allow **9000/9001** only on intranet (host firewall or Docker network).
* **Cold archive cadence**: change `ARCHIVE_EVERY_N_MINUTES`.

---

## 12) Troubleshooting quickies

* If `audit.json` is empty: ensure `SecAuditEngine RelevantOnly` and `SecAuditLogRelevantStatus` match your test traffic (4xx/5xx). Temporarily use `SecAuditEngine On` to force writes while testing.
* Build time: source build is heavy but self-contained. For faster builds, you can pre-base from an image that already bundles **nginx+modsecurity**.
* SELinux/AppArmor: host may need allowances for bind-mounts.

---

## 13) Next (optional)

* Add a small UI for the viewer (charts, filters).
* Emit structured NGINX access logs and join with ModSec by `txid`.
* Add S3 lifecycle policy on MinIO bucket.

```
```
