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
