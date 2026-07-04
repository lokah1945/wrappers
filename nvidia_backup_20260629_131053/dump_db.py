import sqlite3

db_path = '/root/wrapper/nvidia/metrics.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("--- TABLES ---")
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [row[0] for row in cur.fetchall()]
print(tables)

for table in tables:
    print(f"\n--- TABLE: {table} ---")
    cur.execute(f"PRAGMA table_info({table})")
    info = cur.fetchall()
    print("Schema:", [f"{i['name']} ({i['type']})" for i in info])
    
    cur.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 10")
    rows = cur.fetchall()
    for row in rows:
        print(dict(row))

conn.close()
