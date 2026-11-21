import json
from ..db import connection

def log_action(actor_id, action, entity_type, entity_id, detail):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO audit_logs(actor_id,action,entity_type,entity_id,detail) VALUES(%s,%s,%s,%s,%s)", (actor_id, action, entity_type, entity_id, json.dumps(detail, ensure_ascii=False)))
