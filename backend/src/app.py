import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Any

import redis
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .auth import create_token, current_user, hash_password, require_roles
from .db import connection, init_schema
from .middlewares.request_context import AuditRequestMiddleware
from .schemas.domain import LoginIn, LiftIn, ItemIn, NoticeIn, RecheckIn, RecheckReviewIn
from .services.queries import fetch_items, fetch_lift
from .utils.audit import log_action

redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()
    yield

app = FastAPI(title="电梯年检不合格项复核", version="1.0.0", lifespan=lifespan)
app.add_middleware(AuditRequestMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def normalize(value: Any):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    return value


def rows(sql, params=()):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return normalize(cur.fetchall())


def row(sql, params=()):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            result = cur.fetchone()
            return normalize(result) if result else None


def execute(sql, params=()):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.lastrowid


def ensure_lift_access(lift_id: int):
    if not fetch_lift(lift_id):
        raise HTTPException(404, "电梯档案不存在")


@app.get("/api/health")
def health():
    db_ok = False
    redis_ok = False
    try:
        row("SELECT 1 AS ok")
        db_ok = True
    except Exception:
        pass
    try:
        redis_ok = redis_client.ping()
    except Exception:
        pass
    if not (db_ok and redis_ok):
        raise HTTPException(503, {"db": db_ok, "redis": redis_ok})
    return {"status": "healthy", "service": "backend", "db": db_ok, "redis": redis_ok}


@app.post("/api/auth/login")
def login(payload: LoginIn):
    user = row("SELECT id,email,name,role,password_hash FROM users WHERE email=%s", (payload.email,))
    if not user or user["password_hash"] != hash_password(payload.password):
        raise HTTPException(401, "邮箱或密码错误")
    token = create_token(user)
    return {"access_token": token, "token_type": "bearer", "user": {k: user[k] for k in ["id", "email", "name", "role"]}}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"id": int(user["sub"]), "email": user["email"], "name": user["name"], "role": user["role"]}


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    stats = row("""SELECT
      (SELECT COUNT(*) FROM lifts) AS lifts,
      (SELECT COUNT(*) FROM inspection_items WHERE result='fail') AS failures,
      (SELECT COUNT(*) FROM rectification_notices WHERE status IN ('open','submitted','rejected')) AS open_notices,
      (SELECT COUNT(*) FROM certificates WHERE status='issued') AS certificates""")
    recent = rows("""SELECT a.id,a.action,a.entity_type,a.entity_id,a.detail,a.created_at,u.name actor
      FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 8""")
    return {"stats": stats, "recent": recent, "role": user["role"]}


@app.get("/api/lifts")
def list_lifts(status: str | None = Query(None), user=Depends(current_user)):
    if status:
        return rows("SELECT * FROM lifts WHERE status=%s ORDER BY id DESC", (status,))
    return rows("SELECT * FROM lifts ORDER BY id DESC")


@app.post("/api/lifts")
def create_lift(payload: LiftIn, user=Depends(require_roles("operator", "supervisor"))):
    try:
        lift_id = execute("""INSERT INTO lifts(asset_code,building,address,manufacturer,model,rated_load,installed_year,inspection_due,status)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'active')""", (payload.asset_code, payload.building, payload.address, payload.manufacturer, payload.model, payload.rated_load, payload.installed_year, payload.inspection_due))
    except Exception as exc:
        if "Duplicate" in str(exc):
            raise HTTPException(409, "设备编号已存在")
        raise
    log_action(int(user["sub"]), "create_lift", "lift", lift_id, {"asset_code": payload.asset_code})
    return row("SELECT * FROM lifts WHERE id=%s", (lift_id,))


@app.get("/api/lifts/{lift_id}")
def get_lift(lift_id: int, user=Depends(current_user)):
    lift = row("SELECT * FROM lifts WHERE id=%s", (lift_id,))
    if not lift:
        raise HTTPException(404, "电梯档案不存在")
    lift["inspection_items"] = rows("SELECT * FROM inspection_items WHERE lift_id=%s ORDER BY critical DESC,id", (lift_id,))
    lift["notices"] = rows("""SELECT n.*,i.code item_code,i.title item_title FROM rectification_notices n
      JOIN inspection_items i ON i.id=n.inspection_item_id WHERE n.lift_id=%s ORDER BY n.id DESC""", (lift_id,))
    lift["analysis"] = row("SELECT * FROM analysis_jobs WHERE lift_id=%s ORDER BY id DESC LIMIT 1", (lift_id,))
    lift["certificate"] = row("SELECT * FROM certificates WHERE lift_id=%s AND status='issued' ORDER BY id DESC LIMIT 1", (lift_id,))
    return lift


@app.post("/api/lifts/{lift_id}/inspection-items")
def add_inspection_item(lift_id: int, payload: ItemIn, user=Depends(require_roles("operator", "supervisor"))):
    ensure_lift_access(lift_id)
    if payload.result not in {"pass", "fail", "na"}:
        raise HTTPException(422, "检验结果必须是 pass/fail/na")
    try:
        item_id = execute("""INSERT INTO inspection_items(lift_id,code,title,category,critical,score,result,notes,photo_ref,inspected_at,status)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')""", (lift_id, payload.code, payload.title, payload.category, payload.critical, payload.score, payload.result, payload.notes, payload.photo_ref, datetime.utcnow()))
    except Exception as exc:
        if "Duplicate" in str(exc):
            raise HTTPException(409, "同一电梯的检验项编码已存在")
        raise
    execute("UPDATE lifts SET status='under_review' WHERE id=%s AND status='active'", (lift_id,))
    log_action(int(user["sub"]), "add_inspection_item", "inspection_item", item_id, {"lift_id": lift_id, "code": payload.code, "result": payload.result})
    return row("SELECT * FROM inspection_items WHERE id=%s", (item_id,))


@app.post("/api/lifts/{lift_id}/review")
def enqueue_review(lift_id: int, user=Depends(require_roles("operator", "supervisor"))):
    ensure_lift_access(lift_id)
    items = fetch_items(lift_id)
    if len(items) < 3:
        raise HTTPException(409, "至少录入 3 个检验项后才能复核")
    if row("SELECT id FROM analysis_jobs WHERE lift_id=%s AND status IN ('queued','running')", (lift_id,)):
        raise HTTPException(409, "该电梯已有复核任务在处理中")
    features = {
        "critical_fail_count": sum(1 for x in items if x["result"] == "fail" and x["critical"]),
        "average_score": round(sum(float(x["score"]) for x in items if x["result"] != "na") / max(1, sum(1 for x in items if x["result"] != "na")), 2),
        "failed_item_count": sum(1 for x in items if x["result"] == "fail"),
        "documentation_gap": sum(1 for x in items if x["category"] == "documentation" and x["result"] == "fail"),
    }
    job_id = execute("INSERT INTO analysis_jobs(lift_id,status,features_json) VALUES(%s,'queued',%s)", (lift_id, json.dumps(features)))
    redis_client.lpush("lift_analysis", str(job_id))
    execute("UPDATE lifts SET status='under_review' WHERE id=%s", (lift_id,))
    log_action(int(user["sub"]), "enqueue_review", "analysis_job", job_id, {"lift_id": lift_id, "features": features})
    return {"job_id": job_id, "status": "queued", "features": features}


@app.get("/api/lifts/{lift_id}/analysis")
def get_analysis(lift_id: int, user=Depends(current_user)):
    ensure_lift_access(lift_id)
    result = row("SELECT * FROM analysis_jobs WHERE lift_id=%s ORDER BY id DESC LIMIT 1", (lift_id,))
    if result and result.get("path_rules_json"):
        try:
            result["path_rules"] = json.loads(result["path_rules_json"])
        except Exception:
            result["path_rules"] = result["path_rules_json"]
    return result or {"status": "not_started"}


@app.get("/api/notices")
def list_notices(user=Depends(current_user)):
    return rows("""SELECT n.*,l.asset_code,l.building,i.code item_code,i.title item_title,i.critical
      FROM rectification_notices n JOIN lifts l ON l.id=n.lift_id JOIN inspection_items i ON i.id=n.inspection_item_id ORDER BY n.id DESC""")


@app.post("/api/lifts/{lift_id}/notices")
def create_notice(lift_id: int, payload: NoticeIn, user=Depends(require_roles("operator", "supervisor"))):
    ensure_lift_access(lift_id)
    item = row("SELECT * FROM inspection_items WHERE id=%s AND lift_id=%s", (payload.inspection_item_id, lift_id))
    if not item or item["result"] != "fail":
        raise HTTPException(409, "整改通知必须关联本电梯的失败检验项")
    if payload.severity not in {"critical", "major", "minor"}:
        raise HTTPException(422, "严重级别无效")
    notice_id = execute("""INSERT INTO rectification_notices(lift_id,inspection_item_id,issue_summary,severity,deadline,status,evidence_required,created_by)
      VALUES(%s,%s,%s,%s,%s,'open',%s,%s)""", (lift_id, payload.inspection_item_id, payload.issue_summary, payload.severity, payload.deadline, payload.evidence_required, int(user["sub"])))
    execute("UPDATE lifts SET status='rectification' WHERE id=%s", (lift_id,))
    log_action(int(user["sub"]), "create_notice", "rectification_notice", notice_id, {"lift_id": lift_id, "item_code": item["code"], "severity": payload.severity})
    return row("SELECT * FROM rectification_notices WHERE id=%s", (notice_id,))


@app.get("/api/rechecks")
def list_rechecks(user=Depends(current_user)):
    return rows("""SELECT r.*,n.lift_id,n.issue_summary,n.evidence_required,l.asset_code,i.code item_code
      FROM rechecks r JOIN rectification_notices n ON n.id=r.notice_id JOIN lifts l ON l.id=n.lift_id JOIN inspection_items i ON i.id=n.inspection_item_id ORDER BY r.id DESC""")


@app.post("/api/rechecks")
def create_recheck(payload: RecheckIn, user=Depends(require_roles("operator", "supervisor"))):
    notice = row("""SELECT n.*,i.code item_code,i.result item_result FROM rectification_notices n
      JOIN inspection_items i ON i.id=n.inspection_item_id WHERE n.id=%s""", (payload.notice_id,))
    if not notice:
        raise HTTPException(404, "整改通知不存在")
    if notice["status"] not in {"open", "rejected", "submitted"}:
        raise HTTPException(409, "当前整改通知状态不能预约复检")
    if payload.photo_item_code != notice["item_code"]:
        raise HTTPException(409, {"code": "PHOTO_ITEM_MISMATCH", "message": "整改照片与复检项不匹配，请补传对应项证据", "expected": notice["item_code"], "received": payload.photo_item_code})
    recheck_id = execute("""INSERT INTO rechecks(notice_id,scheduled_at,inspector,status,photo_ref,photo_item_code,remarks,created_by)
      VALUES(%s,%s,%s,'scheduled',%s,%s,%s,%s)""", (payload.notice_id, payload.scheduled_at, payload.inspector, payload.photo_ref, payload.photo_item_code, payload.remarks, int(user["sub"])))
    execute("UPDATE rectification_notices SET status='submitted' WHERE id=%s", (payload.notice_id,))
    execute("UPDATE lifts SET status='recheck' WHERE id=%s", (notice["lift_id"],))
    log_action(int(user["sub"]), "schedule_recheck", "recheck", recheck_id, {"notice_id": payload.notice_id, "photo_item_code": payload.photo_item_code})
    return row("SELECT * FROM rechecks WHERE id=%s", (recheck_id,))


@app.post("/api/rechecks/{recheck_id}/review")
def review_recheck(recheck_id: int, payload: RecheckReviewIn, user=Depends(require_roles("supervisor"))):
    if payload.result not in {"pass", "fail"}:
        raise HTTPException(422, "复检结果必须是 pass/fail")
    record = row("""SELECT r.*,n.lift_id,n.inspection_item_id FROM rechecks r JOIN rectification_notices n ON n.id=r.notice_id WHERE r.id=%s""", (recheck_id,))
    if not record:
        raise HTTPException(404, "复检预约不存在")
    status = "passed" if payload.result == "pass" else "failed"
    execute("UPDATE rechecks SET status=%s,result=%s,remarks=%s,reviewed_by=%s,reviewed_at=%s WHERE id=%s", (status, payload.result, payload.remarks, int(user["sub"]), datetime.utcnow(), recheck_id))
    notice_status = "verified" if payload.result == "pass" else "rejected"
    execute("UPDATE rectification_notices SET status=%s WHERE id=%s", (notice_status, record["notice_id"]))
    execute("UPDATE inspection_items SET status=%s,result=%s WHERE id=%s", ("verified" if payload.result == "pass" else "open", payload.result, record["inspection_item_id"]))
    execute("UPDATE lifts SET status=%s WHERE id=%s", ("under_review" if payload.result == "pass" else "rectification", record["lift_id"]))
    log_action(int(user["sub"]), "review_recheck", "recheck", recheck_id, {"result": payload.result, "notice_id": record["notice_id"]})
    return row("SELECT * FROM rechecks WHERE id=%s", (recheck_id,))


@app.get("/api/certificates")
def list_certificates(user=Depends(current_user)):
    return rows("""SELECT c.*,l.asset_code,l.building,u.name issuer FROM certificates c JOIN lifts l ON l.id=c.lift_id JOIN users u ON u.id=c.issued_by ORDER BY c.id DESC""")


@app.post("/api/lifts/{lift_id}/certificate")
def issue_certificate(lift_id: int, user=Depends(require_roles("supervisor"))):
    ensure_lift_access(lift_id)
    lift = row("SELECT * FROM lifts WHERE id=%s", (lift_id,))
    items = rows("SELECT * FROM inspection_items WHERE lift_id=%s", (lift_id,))
    open_notices = row("SELECT COUNT(*) count FROM rectification_notices WHERE lift_id=%s AND status NOT IN ('verified','closed')", (lift_id,))["count"]
    failed_critical = [x for x in items if x["critical"] and x["result"] == "fail"]
    recheck_passed = row("""SELECT COUNT(*) count FROM rechecks r JOIN rectification_notices n ON n.id=r.notice_id
      WHERE n.lift_id=%s AND r.status='passed'""", (lift_id,))["count"]
    analysis = row("SELECT * FROM analysis_jobs WHERE lift_id=%s AND status='completed' ORDER BY id DESC LIMIT 1", (lift_id,))
    blockers = []
    if failed_critical:
        blockers.append("安全回路或制动器关键项仍不合格，一票否决")
    if open_notices:
        blockers.append("仍有未验证的整改通知")
    if not analysis or analysis["prediction"] not in {"ready", "review"}:
        blockers.append("尚未完成有效的树模型复核")
    if open_notices and not recheck_passed:
        blockers.append("存在整改项但没有通过复检")
    if blockers:
        log_action(int(user["sub"]), "certificate_blocked", "lift", lift_id, {"blockers": blockers})
        raise HTTPException(409, {"code": "CERTIFICATE_BLOCKED", "blockers": blockers})
    cert_no = f"NANJING-{datetime.utcnow().year}-{lift['asset_code']}-{uuid.uuid4().hex[:6].upper()}"
    cert_id = execute("""INSERT INTO certificates(lift_id,certificate_no,issued_at,valid_until,status,decision_reason,issued_by)
      VALUES(%s,%s,%s,%s,'issued',%s,%s)""", (lift_id, cert_no, datetime.utcnow(), date.today().replace(year=date.today().year+1), "规则路径复核通过，关键安全项与复检证据均满足签发条件", int(user["sub"])))
    execute("UPDATE lifts SET status='certified' WHERE id=%s", (lift_id,))
    log_action(int(user["sub"]), "issue_certificate", "certificate", cert_id, {"lift_id": lift_id, "certificate_no": cert_no})
    return row("SELECT * FROM certificates WHERE id=%s", (cert_id,))


@app.get("/api/audit-logs")
def audit_logs(user=Depends(require_roles("supervisor", "viewer"))):
    return rows("""SELECT a.*,u.name actor FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 100""")
