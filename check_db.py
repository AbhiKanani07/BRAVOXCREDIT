import os
from dotenv import load_dotenv
load_dotenv()
try:
    import psycopg
    c = psycopg.connect(os.environ["DATABASE_URL"])
    tables = [r[0] for r in c.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ).fetchall()]
    print("TABLES:", tables)
    rows = c.execute("SELECT card_id, outcome, ts FROM feedback").fetchall()
    print("FEEDBACK ROWS:", rows)
except Exception as e:
    print("ERROR:", repr(e))
