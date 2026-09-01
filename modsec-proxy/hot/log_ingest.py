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


