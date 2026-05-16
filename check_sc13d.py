import sqlite3

conn = sqlite3.connect("stock-dashboard/db/cache.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print("Tables:", [r[0] for r in cur.fetchall()])

cur.execute("SELECT COUNT(*) FROM sc13d_cache")
print("Total rows:", cur.fetchone()[0])

cur.execute("SELECT filer_cik, accession_number, subject_company, filing_date FROM sc13d_cache LIMIT 20")
for r in cur.fetchall():
    print(dict(r))

conn.close()
