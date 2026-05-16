import sqlite3

conn = sqlite3.connect("stock-dashboard/db/cache.db")
cur = conn.cursor()
cur.execute("DELETE FROM sc13d_cache")
conn.commit()
print(f"Deleted {cur.rowcount} rows from sc13d_cache")
conn.close()
