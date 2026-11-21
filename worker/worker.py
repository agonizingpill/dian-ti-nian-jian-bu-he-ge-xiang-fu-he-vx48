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
            # 排队期间事实已变更：任务被标记 superseded，直接归档，不再占用模型也不回写电梯状态。
            if job.get("superseded"):
                print(f"job {job_id} superseded before run, archive only", flush=True)
                return
            cur.execute("UPDATE analysis_jobs SET status='running' WHERE id=%s AND superseded=0", (job_id,))
    try:
        features = job["features_json"] if isinstance(job["features_json"], dict) else json.loads(job["features_json"])
        result = classify(features)
        with db_conn() as conn:
            conn.autocommit(False)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT superseded FROM analysis_jobs WHERE id=%s FOR UPDATE", (job_id,))
                    latest = cur.fetchone()
                    if latest is None:
                        conn.commit()
                        return
                    if latest["superseded"]:
                        # 计算期间事实发生变更：结果照常落库归档，但标记过期，不更新电梯状态、不影响新复核与签证。
                        cur.execute("""UPDATE analysis_jobs SET status='completed',prediction=%s,probability=%s,
                            path_rules_json=%s,completed_at=%s,superseded=1,
                            superseded_reason=COALESCE(superseded_reason,'计算期间检验事实已变更，结果仅归档') WHERE id=%s""",
                            (result["prediction"], result["probability"],
                             json.dumps({"rules": result["path_rules"], "model": result["model"], "features": features}, ensure_ascii=False),
                             datetime.utcnow(), job_id))
                        print(f"job {job_id} finished stale, archived as superseded", flush=True)
                    else:
                        cur.execute("""UPDATE analysis_jobs SET status='completed',prediction=%s,probability=%s,path_rules_json=%s,completed_at=%s WHERE id=%s""",
                            (result["prediction"], result["probability"], json.dumps({"rules": result["path_rules"], "model": result["model"], "features": features}, ensure_ascii=False), datetime.utcnow(), job_id))
                        cur.execute("UPDATE lifts SET status=IF(status='under_review','rectification',status) WHERE id=%s", (job["lift_id"],))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit(True)
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
