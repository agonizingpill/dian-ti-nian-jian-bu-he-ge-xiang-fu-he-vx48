"""复核版本与签证采信裁决。

设计要点：
- lifts.data_version 是检验事实的版本号。补录检验项、复检改判都会让它 +1。
- analysis_jobs.input_version 记录任务快照对应的事实版本；superseded=1 表示结论已过期，只作归档。
- 签证只采信 input_version 等于当前 data_version、superseded=0 且 completed 的任务，并把该任务 id 落到证书上。
"""
import json

from ..algorithm import build_features
from ..db import connection, transaction

# 只有这些状态的任务还能代表当前事实；排队/计算中的任务同样要在事实变更时作废。
ACTIVE_JOB_STATUSES = ("queued", "running", "completed", "failed")


def item_snapshot(items):
    """检验事实快照：复核任务保留它所看的那一批检验项原始输入。"""
    return [
        {
            "id": x["id"],
            "code": x["code"],
            "title": x["title"],
            "category": x["category"],
            "critical": bool(x["critical"]),
            "score": float(x["score"]),
            "result": x["result"],
            "status": x["status"],
        }
        for x in items
    ]


def bump_lift_data_version(conn, lift_id, reason):
    """推进事实版本，并把该电梯所有未归档任务标记为 superseded。

    必须在调用方的事务连接内执行，保证“事实落定 + 旧结论作废”原子可见。
    返回推进后的版本号。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT data_version FROM lifts WHERE id=%s FOR UPDATE", (lift_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError("电梯档案不存在")
        new_version = int(row["data_version"]) + 1
        cur.execute("UPDATE lifts SET data_version=%s WHERE id=%s", (new_version, lift_id))
        cur.execute(
            "UPDATE analysis_jobs SET superseded=1, superseded_reason=%s "
            "WHERE lift_id=%s AND superseded=0",
            (reason, lift_id),
        )
        return new_version


def has_active_job(conn, lift_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM analysis_jobs WHERE lift_id=%s AND superseded=0 AND status IN ('queued','running') LIMIT 1",
            (lift_id,),
        )
        return cur.fetchone() is not None


def create_review_job(lift_id, actor_id, enqueue_callback):
    """入队一次复核：快照当前检验事实，写入版本，推送 worker 队列。

    enqueue_callback(job_id) 负责把 job_id 推到 Redis，放在事务提交后执行，
    避免 worker 取到尚未提交的任务。
    """
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, data_version, status FROM lifts WHERE id=%s FOR UPDATE", (lift_id,))
            lift = cur.fetchone()
            if lift is None:
                raise LookupError("电梯档案不存在")
            cur.execute(
                "SELECT * FROM inspection_items WHERE lift_id=%s ORDER BY critical DESC, id",
                (lift_id,),
            )
            items = cur.fetchall()
            if len(items) < 3:
                raise ValueError("至少录入 3 个检验项后才能复核")
            if has_active_job(conn, lift_id):
                raise RuntimeError("该电梯已有复核任务在处理中")
            features = build_features(items)
            snapshot = item_snapshot(items)
            cur.execute(
                "INSERT INTO analysis_jobs(lift_id,status,features_json,input_version,input_snapshot_json,superseded) "
                "VALUES(%s,'queued',%s,%s,%s,0)",
                (lift_id, json.dumps(features, ensure_ascii=False), lift["data_version"],
                 json.dumps(snapshot, ensure_ascii=False)),
            )
            job_id = cur.lastrowid
            cur.execute("UPDATE lifts SET status='under_review' WHERE id=%s", (lift_id,))
    enqueue_callback(job_id)
    return {"job_id": job_id, "status": "queued", "features": features,
            "input_version": lift["data_version"], "snapshot_size": len(snapshot)}


def mark_facts_changed(lift_id, reason):
    """检验事实变更入口（补录检验项 / 复检改判）：版本 +1 并作废旧结论。"""
    with transaction() as conn:
        return bump_lift_data_version(conn, lift_id, reason)


def latest_jobs(lift_id, limit=10):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM analysis_jobs WHERE lift_id=%s ORDER BY id DESC LIMIT %s",
                (lift_id, limit),
            )
            return cur.fetchall()


def current_analysis(lift_id):
    """返回代表当前事实的复核结论；没有有效任务时返回 None。"""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.* FROM analysis_jobs j JOIN lifts l ON l.id=j.lift_id "
                "WHERE j.lift_id=%s AND j.superseded=0 AND j.input_version=l.data_version "
                "ORDER BY j.id DESC LIMIT 1",
                (lift_id,),
            )
            return cur.fetchone()


def certifiable_analysis(conn, lift_id):
    """签证事务内调用：锁定电梯行后，只认版本匹配、未过期、已完成且分流达标的任务。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM lifts WHERE id=%s FOR UPDATE", (lift_id,))
        lift = cur.fetchone()
        if lift is None:
            raise LookupError("电梯档案不存在")
        cur.execute(
            "SELECT * FROM analysis_jobs WHERE lift_id=%s AND superseded=0 AND input_version=%s "
            "AND status='completed' ORDER BY id DESC LIMIT 1",
            (lift_id, int(lift["data_version"])),
        )
        job = cur.fetchone()
        return lift, job
