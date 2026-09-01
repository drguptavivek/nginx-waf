All set—I turned your README into a full Docker stack. You’ll see a canvas on the right with:

* `docker-compose.yml` wiring up: **nginx+modsecurity v3 + CRS**, a **DuckDB hot ingester**, **MinIO** cold store, an **archiver** to Parquet→S3 (MinIO), and an optional **Flask viewer**.
* A source-built **nginx+modsecurity** Dockerfile plus **nginx.conf**, **modsecurity.conf**, **crs-setup.conf**, and a `rules/` folder scaffold.
* Python scripts for hot ingest (`hot/log_ingest.py`) and archival (`archiver/archive_to_minio.py`).
* A minimal **viewer** service to query the last 6 hours and rule hits.

### Quick start

1. In your project root:

```bash
cp .env.example .env
mkdir -p data/logs data/duckdb data/minio
docker compose up -d --build
```

2. Test requests go through ModSecurity:

```bash
curl -i http://localhost/
```

3. Hot viewer API:

```bash
curl -s http://localhost:8080/api/last6h | jq .
```

4. MinIO console (restrict to intranet at the host firewall): `http://<host>:9001`

Want me to add a small HTML UI for the viewer and a Makefile with common ops (`up`, `down`, `logs`, `shell`)?
