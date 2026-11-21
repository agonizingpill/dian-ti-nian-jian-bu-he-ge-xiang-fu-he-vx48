from ..db import connection

def fetch_lift(lift_id):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM lifts WHERE id=%s", (lift_id,))
            return cur.fetchone()

def fetch_items(lift_id):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM inspection_items WHERE lift_id=%s ORDER BY critical DESC, id", (lift_id,))
            return cur.fetchall()
