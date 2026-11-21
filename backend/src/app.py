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
from .db import connection, init_schema, transaction
from .middlewares.request_context import AuditRequestMiddleware
from .schemas.domain import LoginIn, LiftIn, ItemIn, NoticeIn, RecheckIn, RecheckReviewIn
from .services import review
from .services.queries import fetch_lift
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
    current = review.current_analysis(lift_id)
    lift["analysis"] = current if current else row("SELECT * FROM analysis_jobs WHERE lift_id=%s ORDER BY id DESC LIMIT 1", (lift_id,))
    if lift["analysis"]:
        lift["analysis"]["stale"] = bool(lift["analysis"].get("superseded")) or int(lift["analysis"].get("input_version", -1)) != int(lift["data_version"])
    lift["certificate"] = row("SELECT * FROM certificates WHERE lift_id=%s AND status='issued' ORDER BY id DESC LIMIT 1", (lift_id,))
    return lift


@app.post("/api/lifts/{lift_id}/inspection-items")
def add_inspection_item(lift_id: int, payload: ItemIn, user=Depends(require_roles("operator", "supervisor"))):
    ensure_lift_access(lift_id)
    if payload.result not in {"pass", "fail", "na"}:
        raise HTTPException(422, "检验结果必须是 pass/fail/na")
    with transaction() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""INSERT INTO inspection_items(lift_id,code,title,category,critical,score,result,notes,photo_ref,inspected_at,status)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')""", (lift_id, payload.code, payload.title, payload.category, payload.critical, payload.score, payload.result, payload.notes, payload.photo_ref, datetime.utcnow()))
            except Exception as exc:
                if "Duplicate" in str(exc):
                    raise HTTPException(409, "同一电梯的检验项编码已存在")
                raise
            item_id = cur.lastrowid
            cur.execute("UPDATE lifts SET status='under_review' WHERE id=%s AND status='active'", (lift_id,))
            # 补录即事实变更：版本推进，旧复核（含在途任务）一律过期归档，签证必须等重算。
            new_version = review.bump_lift_data_version(conn, lift_id, "补录检验项 %s，旧复核结论已过期" % payload.code)
    log_action(int(user["sub"]), "add_inspection_item", "inspection_item", item_id, {"lift_id": lift_id, "code": payload.code, "result": payload.result, "data_version": new_version})
    return row("SELECT * FROM inspection_items WHERE id=%s", (item_id,))


@app.post("/api/lifts/{lift_id}/review")
def enqueue_review(lift_id: int, user=Depends(require_roles("operator", "supervisor"))):
    ensure_lift_access(lift_id)

    def push_queue(job_id):
        redis_client.lpush("lift_analysis", str(job_id))

    try:
        result = review.create_review_job(lift_id, int(user["sub"]), push_queue)
    except LookupError:
        raise HTTPException(404, "电梯档案不存在")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    log_action(int(user["sub"]), "enqueue_review", "analysis_job", result["job_id"], {"lift_id": lift_id, "features": result["features"], "input_version": result["input_version"]})
    return result


@app.get("/api/lifts/{lift_id}/analysis")
def get_analysis(lift_id: int, user=Depends(current_user)):
    ensure_lift_access(lift_id)
    result = review.current_analysis(lift_id)
    if not result:
        # 没有当前版本的有效结论时，仍把最新一条（可能已过期）告诉工作台，用于提示“待重算”。
        latest = row("SELECT * FROM analysis_jobs WHERE lift_id=%s ORDER BY id DESC LIMIT 1", (lift_id,))
        if latest:
            latest["stale"] = True
            return latest
        return {"status": "not_started"}
    result["stale"] = False
    if result.get("path_rules_json"):
        try:
            result["path_rules"] = json.loads(result["path_rules_json"])
        except Exception:
            result["path_rules"] = result["path_rules_json"]
    return result


@app.get("/api/lifts/{lift_id}/analysis-history")
def analysis_history(lift_id: int, user=Depends(current_user)):
    ensure_lift_access(lift_id)
    lift = row("SELECT id,data_version FROM lifts WHERE id=%s", (lift_id,))
    if not lift:
        raise HTTPException(404, "电梯档案不存在")
    jobs = review.latest_jobs(lift_id, limit=20)
    for job in jobs:
        job["stale"] = bool(job["superseded"]) or int(job["input_version"]) != int(lift["data_version"])
        if job.get("path_rules_json"):
            try:
                job["path_rules"] = json.loads(job["path_rules_json"])
            except Exception:
                job["path_rules"] = job["path_rules_json"]
        if job.get("input_snapshot_json"):
            try:
                job["input_snapshot"] = json.loads(job["input_snapshot_json"])
            except Exception:
                job["input_snapshot"] = job["input_snapshot_json"]
    return {"data_version": lift["data_version"], "jobs": normalize(jobs)}


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
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT r.*,n.lift_id,n.inspection_item_id,i.code item_code
              FROM rechecks r JOIN rectification_notices n ON n.id=r.notice_id
              JOIN inspection_items i ON i.id=n.inspection_item_id WHERE r.id=%s FOR UPDATE""", (recheck_id,))
            record = cur.fetchone()
            if not record:
                raise HTTPException(404, "复检预约不存在")
            status = "passed" if payload.result == "pass" else "failed"
            cur.execute("UPDATE rechecks SET status=%s,result=%s,remarks=%s,reviewed_by=%s,reviewed_at=%s WHERE id=%s", (status, payload.result, payload.remarks, int(user["sub"]), datetime.utcnow(), recheck_id))
            notice_status = "verified" if payload.result == "pass" else "rejected"
            cur.execute("UPDATE rectification_notices SET status=%s WHERE id=%s", (notice_status, record["notice_id"]))
            cur.execute("UPDATE inspection_items SET status=%s,result=%s WHERE id=%s", ("verified" if payload.result == "pass" else "open", payload.result, record["inspection_item_id"]))
            cur.execute("UPDATE lifts SET status=%s WHERE id=%s", ("under_review" if payload.result == "pass" else "rectification", record["lift_id"]))
            # 复检改判改变了检验事实（即便分值未变）：旧复核结论一律过期，签证等重算。
            new_version = review.bump_lift_data_version(conn, record["lift_id"], "复检 %s 改判为 %s，旧复核结论已过期" % (record["item_code"], payload.result))
    log_action(int(user["sub"]), "review_recheck", "recheck", recheck_id, {"result": payload.result, "notice_id": record["notice_id"], "data_version": new_version})
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rechecks WHERE id=%s", (recheck_id,))
            return normalize(cur.fetchone())


@app.get("/api/certificates")
def list_certificates(user=Depends(current_user)):
    return rows("""SELECT c.*,l.asset_code,l.building,u.name issuer,
      c.analysis_job_id AS based_on_job_id,
      j.input_version AS based_on_version, j.prediction AS based_on_prediction
      FROM certificates c JOIN lifts l ON l.id=c.lift_id JOIN users u ON u.id=c.issued_by
      LEFT JOIN analysis_jobs j ON j.id=c.analysis_job_id ORDER BY c.id DESC""")


@app.get("/api/certificates/{cert_id}/basis")
def certificate_basis(cert_id: int, user=Depends(current_user)):
    """签证采信的复核：原始输入快照 + 规则路径，支撑事后审计。"""
    cert = row("""SELECT c.*,l.asset_code,l.building FROM certificates c
      JOIN lifts l ON l.id=c.lift_id WHERE c.id=%s""", (cert_id,))
    if not cert:
        raise HTTPException(404, "证书不存在")
    job = None
    if cert.get("analysis_job_id"):
        job = row("SELECT * FROM analysis_jobs WHERE id=%s", (cert["analysis_job_id"],))
        if job:
            for key in ("path_rules_json", "input_snapshot_json", "features_json"):
                if job.get(key):
                    try:
                        job[key.replace("_json", "")] = json.loads(job[key])
                    except Exception:
                        pass
    return {"certificate": cert, "analysis_job": normalize(job) if job else None}


@app.post("/api/lifts/{lift_id}/certificate")
def issue_certificate(lift_id: int, user=Depends(require_roles("supervisor"))):
    ensure_lift_access(lift_id)
    blocked = None
    with transaction() as conn:
        with conn.cursor() as cur:
            # 锁定电梯行：检验变更与签证并发时，谁先拿到锁谁先落定事实，后到者按新版本裁决。
            try:
                lift, analysis = review.certifiable_analysis(conn, lift_id)
            except LookupError:
                raise HTTPException(404, "电梯档案不存在")
            cur.execute("SELECT * FROM inspection_items WHERE lift_id=%s", (lift_id,))
            items = normalize(cur.fetchall())
            cur.execute("SELECT COUNT(*) count FROM rectification_notices WHERE lift_id=%s AND status NOT IN ('verified','closed')", (lift_id,))
            open_notices = cur.fetchone()["count"]
            failed_critical = [x for x in items if x["critical"] and x["result"] == "fail"]
            cur.execute("""SELECT COUNT(*) count FROM rechecks r JOIN rectification_notices n ON n.id=r.notice_id
              WHERE n.lift_id=%s AND r.status='passed'""", (lift_id,))
            recheck_passed = cur.fetchone()["count"]
            blockers = []
            if failed_critical:
                blockers.append("安全回路或制动器关键项仍不合格，一票否决")
            if open_notices:
                blockers.append("仍有未验证的整改通知")
            if open_notices and not recheck_passed:
                blockers.append("存在整改项但没有通过复检")
            if not analysis:
                # 区分“没算过”和“算过但已过期”，让主管明确知道要等重算。
                cur.execute("SELECT 1 FROM analysis_jobs WHERE lift_id=%s LIMIT 1", (lift_id,))
                if cur.fetchone():
                    blockers.append("复核结论已过期：检验项补录或复检改判后需重新提交树模型复核")
                else:
                    blockers.append("尚未完成有效的树模型复核")
            elif analysis["prediction"] not in {"ready", "review"}:
                blockers.append("当前树模型分流结论为 %s，不满足签发条件" % analysis["prediction"])
            if blockers:
                # 审计写入与阻断判定在同一事务提交，避免抛出异常把审计记录一起回滚。
                cur.execute("INSERT INTO audit_logs(actor_id,action,entity_type,entity_id,detail) VALUES(%s,'certificate_blocked','lift',%s,%s)",
                            (int(user["sub"]), lift_id, json.dumps({"blockers": blockers, "data_version": lift["data_version"]}, ensure_ascii=False)))
                blocked = {"code": "CERTIFICATE_BLOCKED", "blockers": blockers, "data_version": lift["data_version"]}
            else:
                cert_no = f"NANJING-{datetime.utcnow().year}-{lift['asset_code']}-{uuid.uuid4().hex[:6].upper()}"
                decision_reason = "规则路径复核通过（复核任务 #%s，事实版本 v%s），关键安全项与复检证据均满足签发条件" % (analysis["id"], lift["data_version"])
                cur.execute("""INSERT INTO certificates(lift_id,certificate_no,issued_at,valid_until,status,decision_reason,issued_by,analysis_job_id)
                  VALUES(%s,%s,%s,%s,'issued',%s,%s,%s)""", (lift_id, cert_no, datetime.utcnow(), date.today().replace(year=date.today().year+1), decision_reason, int(user["sub"]), analysis["id"]))
                cert_id = cur.lastrowid
                cur.execute("UPDATE lifts SET status='certified' WHERE id=%s", (lift_id,))
                cur.execute("INSERT INTO audit_logs(actor_id,action,entity_type,entity_id,detail) VALUES(%s,'issue_certificate','certificate',%s,%s)",
                            (int(user["sub"]), cert_id, json.dumps({"lift_id": lift_id, "certificate_no": cert_no, "analysis_job_id": analysis["id"], "data_version": lift["data_version"]}, ensure_ascii=False)))
                cur.execute("SELECT * FROM certificates WHERE id=%s", (cert_id,))
                cert_row = normalize(cur.fetchone())
    # 事务已提交后再抛出，阻断审计得以保留。
    if blocked:
        raise HTTPException(409, blocked)
    return cert_row


@app.get("/api/audit-logs")
def audit_logs(user=Depends(require_roles("supervisor", "viewer"))):
    return rows("""SELECT a.*,u.name actor FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 100""")
