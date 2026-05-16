import sqlite3
db = "stock-dashboard/db/wsb.db"
conn = sqlite3.connect(db)
posts = conn.execute("SELECT COUNT(*) FROM wsb_posts WHERE ticker='NBIS'").fetchone()[0]
summaries = conn.execute("SELECT COUNT(*) FROM wsb_ticker_summaries WHERE ticker='NBIS'").fetchone()[0]
print(f"Cached posts: {posts}, summaries: {summaries}")
conn.execute("DELETE FROM wsb_posts WHERE ticker='NBIS'")
conn.execute("DELETE FROM wsb_ticker_summaries WHERE ticker='NBIS'")
conn.commit()
print("Cache cleared for NBIS.")
conn.close()
