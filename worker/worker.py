import json
import os
import time
from datetime import datetime
import pymysql
import redis
from algorithm import classify

DB = {
    "host": os.getenv("MYSQL_HOST", "db"), "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "lift_user"), "password": os.getenv("MYSQL_PASSWORD", "lift_pass"),
    "database": os.getenv("MYSQL_DATABASE", "lift_review"), "cursorclass": pymysql.cursors.DictCursor, "autocommit": True,
}
rdb = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

def db_conn():
    return pymysql.connect(**DB)

def process(job_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM analysis_jobs WHERE id=%s", (job_id,))
            job = cur.fetchone()
            if not job:
                return
            cur.execute("UPDATE analysis_jobs SET status='running' WHERE id=%s", (job_id,))
    try:
        features = job["features_json"] if isinstance(job["features_json"], dict) else json.loads(job["features_json"])
        result = classify(features)
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE analysis_jobs SET status='completed',prediction=%s,probability=%s,path_rules_json=%s,completed_at=%s WHERE id=%s""", (result["prediction"], result["probability"], json.dumps({"rules": result["path_rules"], "model": result["model"], "features": features}, ensure_ascii=False), datetime.utcnow(), job_id))
                cur.execute("UPDATE lifts SET status=IF(status='under_review','rectification',status) WHERE id=%s", (job["lift_id"],))
    except Exception as exc:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE analysis_jobs SET status='failed',error=%s,completed_at=%s WHERE id=%s", (str(exc), datetime.utcnow(), job_id))

while True:
    try:
        open("/shared/worker_healthy", "w").write(datetime.utcnow().isoformat())
        message = rdb.brpop("lift_analysis", timeout=5)
        if message:
            process(int(message[1]))
    except Exception as exc:
        print(f"worker loop error: {exc}", flush=True)
        time.sleep(2)
