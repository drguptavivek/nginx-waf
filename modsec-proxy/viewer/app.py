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