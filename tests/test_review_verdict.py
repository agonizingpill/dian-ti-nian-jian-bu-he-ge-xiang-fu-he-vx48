"""不依赖 MySQL/Redis/sklearn 的裁决逻辑测试。

用内存假库驱动真实的 src/services/review.py 与 worker.py 的 process()，
验证版本推进、任务过期、签证采信、旧任务迟到归档等关键规则。
运行：python3 -I tests/test_review_verdict.py
"""
import ast
import importlib.util
import json
import re
import sys
import textwrap
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
WORKER = ROOT / "worker"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


# ---------------------------------------------------------------- 内存假库
class FakeDB:
    def __init__(self):
        self.lifts = {}          # id -> row
        self.items = {}          # id -> row
        self.jobs = {}           # id -> row
        self._id = {"lifts": 0, "items": 0, "jobs": 0}
        self.events = []         # 记录提交/入队顺序
        self.committed = 0

    def next_id(self, table):
        self._id[table] += 1
        return self._id[table]

    def add_lift(self, status="under_review", version=0):
        lid = self.next_id("lifts")
        self.lifts[lid] = {"id": lid, "asset_code": f"NJ-{lid}", "status": status, "data_version": version}
        return lid

    def add_item(self, lid, code, result="pass", score=90.0, critical=False, category="safety"):
        iid = self.next_id("items")
        self.items[iid] = {"id": iid, "lift_id": lid, "code": code, "title": code,
                           "category": category, "critical": critical, "score": score,
                           "result": result, "status": "open"}
        return iid

    def add_job(self, lid, status="completed", input_version=0, superseded=0, prediction="ready"):
        jid = self.next_id("jobs")
        self.jobs[jid] = {"id": jid, "lift_id": lid, "status": status,
                          "features_json": json.dumps({"k": 1}), "prediction": prediction,
                          "probability": 0.9, "path_rules_json": json.dumps({"rules": "r"}),
                          "error": None, "input_version": input_version,
                          "input_snapshot_json": json.dumps([{"code": "X"}]),
                          "superseded": superseded, "superseded_reason": None,
                          "completed_at": None}
        return jid


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = None
        self.lastrowid = None

    def execute(self, sql, params=()):
        db = self.db
        p = params
        s = " ".join(sql.split())

        if s.startswith("SELECT data_version FROM lifts WHERE id=%s FOR UPDATE"):
            self._result = [dict(db.lifts[p[0]])] if p[0] in db.lifts else []
        elif s.startswith("UPDATE lifts SET data_version=%s WHERE id=%s"):
            db.lifts[p[1]]["data_version"] = p[0]
            self._result = []
        elif s.startswith("UPDATE analysis_jobs SET superseded=1"):
            for j in db.jobs.values():
                if j["lift_id"] == p[1] and j["superseded"] == 0:
                    j["superseded"] = 1
                    j["superseded_reason"] = p[0]
            self._result = []
        elif s.startswith("SELECT 1 FROM analysis_jobs WHERE lift_id=%s AND superseded=0 AND status IN"):
            hit = any(j["lift_id"] == p[0] and j["superseded"] == 0 and j["status"] in ("queued", "running")
                      for j in db.jobs.values())
            self._result = [{"1": 1}] if hit else []
        elif s.startswith("SELECT id, data_version, status FROM lifts WHERE id=%s FOR UPDATE"):
            self._result = [dict(db.lifts[p[0]])] if p[0] in db.lifts else []
        elif s.startswith("SELECT * FROM inspection_items WHERE lift_id=%s ORDER BY critical DESC, id"):
            self._result = [dict(v) for v in db.items.values() if v["lift_id"] == p[0]]
        elif s.startswith("INSERT INTO analysis_jobs"):
            # 真实参数顺序：lift_id, features_json, input_version, input_snapshot_json
            jid = db.add_job(p[0], status="queued", input_version=p[2], superseded=0)
            db.jobs[jid]["features_json"] = p[1]
            db.jobs[jid]["input_snapshot_json"] = p[3]
            db.jobs[jid]["prediction"] = None
            db.jobs[jid]["path_rules_json"] = None
            self.lastrowid = jid
            self._result = []
        elif s.startswith("UPDATE lifts SET status='under_review' WHERE id=%s"):
            if db.lifts[p[0]]["status"] == "active":
                db.lifts[p[0]]["status"] = "under_review"
            self._result = []
        elif s.startswith("SELECT * FROM analysis_jobs WHERE lift_id=%s ORDER BY id DESC LIMIT %s"):
            rows = [dict(v) for v in db.jobs.values() if v["lift_id"] == p[0]]
            self._result = sorted(rows, key=lambda r: -r["id"])[: p[1]]
        elif s.startswith("SELECT j.* FROM analysis_jobs j JOIN lifts l"):
            lid = p[0]
            lift = db.lifts.get(lid)
            rows = [dict(j) for j in db.jobs.values()
                    if j["lift_id"] == lid and j["superseded"] == 0
                    and lift and j["input_version"] == lift["data_version"]]
            self._result = sorted(rows, key=lambda r: -r["id"])[:1]
        elif s.startswith("SELECT * FROM lifts WHERE id=%s FOR UPDATE"):
            self._result = [dict(db.lifts[p[0]])] if p[0] in db.lifts else []
        elif s.startswith("SELECT * FROM analysis_jobs WHERE lift_id=%s AND superseded=0 AND input_version=%s AND status='completed'"):
            rows = [dict(j) for j in db.jobs.values()
                    if j["lift_id"] == p[0] and j["superseded"] == 0
                    and j["input_version"] == p[1] and j["status"] == "completed"]
            self._result = sorted(rows, key=lambda r: -r["id"])[:1]
        # ---- worker.py 使用的语句
        elif s.startswith("SELECT * FROM analysis_jobs WHERE id=%s"):
            self._result = [dict(db.jobs[p[0]])] if p[0] in db.jobs else []
        elif s.startswith("UPDATE analysis_jobs SET status='running' WHERE id=%s AND superseded=0"):
            if db.jobs[p[0]]["superseded"] == 0:
                db.jobs[p[0]]["status"] = "running"
            self._result = []
        elif s.startswith("SELECT superseded FROM analysis_jobs WHERE id=%s FOR UPDATE"):
            self._result = [{"superseded": db.jobs[p[0]]["superseded"]}] if p[0] in db.jobs else []
        elif s.startswith("UPDATE analysis_jobs SET status='completed',prediction=%s") and "completed_at=%s,superseded=1" in s:
            j = db.jobs[p[4]]
            j.update(status="completed", prediction=p[0], probability=p[1], path_rules_json=p[2],
                     completed_at=p[3], superseded=1)
            if not j["superseded_reason"]:
                j["superseded_reason"] = "计算期间检验事实已变更，结果仅归档"
            self._result = []
        elif s.startswith("UPDATE analysis_jobs SET status='completed',prediction=%s"):
            j = db.jobs[p[4]]
            j.update(status="completed", prediction=p[0], probability=p[1], path_rules_json=p[2], completed_at=p[3])
            self._result = []
        elif s.startswith("UPDATE lifts SET status=IF(status='under_review','rectification',status) WHERE id=%s"):
            if db.lifts[p[0]]["status"] == "under_review":
                db.lifts[p[0]]["status"] = "rectification"
            self._result = []
        elif s.startswith("UPDATE analysis_jobs SET status='failed'"):
            j = db.jobs[p[2]]
            j.update(status="failed", error=p[0], completed_at=p[1])
            self._result = []
        else:
            raise AssertionError(f"未实现的假库语句: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None
    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _AutoCommit:
    """兼容 pymysql：既可 conn.autocommit=False 赋值，也可 conn.autocommit(False) 调用。"""
    def __init__(self, value=True):
        self.value = value

    def __call__(self, value):
        self.value = bool(value)

    def __bool__(self):
        return self.value

    def __eq__(self, other):
        return self.value == other


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.autocommit = _AutoCommit(True)

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db.committed += 1
        self.db.events.append(("commit", self.db.committed))

    def rollback(self):
        self.db.events.append(("rollback", None))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_transaction(db):
    import contextlib

    @contextlib.contextmanager
    def connection():
        conn = FakeConn(db)
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction():
        conn = FakeConn(db)
        conn.autocommit(False)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit(True)
    return connection, transaction


# ---------------------------------------------------------------- 加载真实模块
def load_review_service(db):
    # 从真实 algorithm.py 中 AST 抽取 build_features（绕过 sklearn 导入）
    alg_src = (BACKEND / "src" / "algorithm.py").read_text(encoding="utf-8")
    tree = ast.parse(alg_src)
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_features")
    ns = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(BACKEND / "src" / "algorithm.py"), "exec"), ns)

    pkg = types.ModuleType("src")
    pkg.__path__ = []
    svc_pkg = types.ModuleType("src.services")
    svc_pkg.__path__ = []
    db_mod = types.ModuleType("src.db")
    db_mod.connection, db_mod.transaction = make_transaction(db)
    algo_mod = types.ModuleType("src.algorithm")
    algo_mod.build_features = ns["build_features"]
    sys.modules.update({"src": pkg, "src.services": svc_pkg, "src.db": db_mod, "src.algorithm": algo_mod})

    spec = importlib.util.spec_from_file_location(
        "src.services.review", BACKEND / "src" / "services" / "review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_worker_module(db, classify_result=None):
    pymysql_stub = types.ModuleType("pymysql")
    pymysql_stub.cursors = types.SimpleNamespace(DictCursor=object)
    pymysql_stub.connect = lambda **kw: FakeConn(db)
    redis_stub = types.ModuleType("redis")
    redis_stub.from_url = lambda *a, **k: types.SimpleNamespace(brpop=lambda *a, **k: None)
    algo_stub = types.ModuleType("algorithm")
    algo_stub.classify = lambda features: {
        "prediction": (classify_result or "ready"), "probability": 0.91,
        "path_rules": "tree rules text", "model": "stub"}
    sys.modules.update({"pymysql": pymysql_stub, "redis": redis_stub, "algorithm": algo_stub})

    src = (WORKER / "worker.py").read_text(encoding="utf-8")
    src = src[: src.index("while True:")]  # 去掉常驻循环，只加载 process()
    mod = types.ModuleType("worker_under_test")
    mod.__dict__["__name__"] = "worker_under_test"
    exec(compile(src, str(WORKER / "worker.py"), "exec"), mod.__dict__)
    return mod


# ---------------------------------------------------------------- 测试用例
def test_review_lifecycle():
    print("\n[1] 复核任务生命周期：快照、版本、入队顺序")
    db = FakeDB()
    review = load_review_service(db)
    lid = db.add_lift()
    for i, code in enumerate(["SAFETY_CIRCUIT", "BRAKE_TEST", "DOOR_FORCE"]):
        db.add_item(lid, code, result="pass" if i else "fail", critical=(i < 2), score=88.0 - i)

    pushed = []
    result = review.create_review_job(lid, 7, lambda jid: (pushed.append(jid), db.events.append(("lpush", jid))))
    job = db.jobs[result["job_id"]]
    check("任务记录入队时的事实版本 input_version=0", job["input_version"] == 0)
    check("任务保留检验项原始输入快照", json.loads(job["input_snapshot_json"])[0]["code"] == "SAFETY_CIRCUIT")
    check("快照包含全部 3 个检验项", len(json.loads(job["input_snapshot_json"])) == 3)
    check("Redis 入队发生在事务提交之后",
          [e[0] for e in db.events] == ["commit", "lpush"], str(db.events))
    check("返回入队版本号", result["input_version"] == 0)


def test_less_than_three_and_concurrent_guard():
    print("\n[2] 入队前置：不足 3 项拒绝、在途任务拒绝重复入队")
    db = FakeDB()
    review = load_review_service(db)
    lid = db.add_lift()
    db.add_item(lid, "A")
    db.add_item(lid, "B")
    try:
        review.create_review_job(lid, 1, lambda j: None)
        check("不足 3 项必须拒绝", False)
    except ValueError as e:
        check("不足 3 项必须拒绝", "3 个检验项" in str(e))

    db.add_item(lid, "C")
    review.create_review_job(lid, 1, lambda j: None)
    try:
        review.create_review_job(lid, 1, lambda j: None)
        check("在途任务存在时拒绝重复入队", False)
    except RuntimeError as e:
        check("在途任务存在时拒绝重复入队", "处理中" in str(e))


def test_fact_change_expires_old_conclusion():
    print("\n[3] 补录/改判推进版本：旧结论（含在途）全部过期，新复核不被冲掉")
    db = FakeDB()
    review = load_review_service(db)
    lid = db.add_lift(version=0)
    for code in ["A", "B", "C"]:
        db.add_item(lid, code)
    old_completed = review.create_review_job(lid, 1, lambda j: None)["job_id"]
    # 模拟 worker 完成该任务：写入预测与规则路径
    db.jobs[old_completed].update(status="completed", prediction="ready",
                                  path_rules_json=json.dumps({"rules": "tree path v0"}))
    # 模拟 worker 完成后，又补录一个检验项
    db.add_item(lid, "VISUAL_042", result="fail", score=70.0)
    new_version = review.mark_facts_changed(lid, "补录检验项 VISUAL_042，旧复核结论已过期")
    check("事实版本推进到 v1", new_version == 1 and db.lifts[lid]["data_version"] == 1)
    check("已完成的旧任务被标记 superseded", db.jobs[old_completed]["superseded"] == 1)
    check("过期原因留痕", "VISUAL_042" in db.jobs[old_completed]["superseded_reason"])
    check("旧任务的原始输入与规则路径仍保留",
          db.jobs[old_completed]["input_snapshot_json"] is not None
          and db.jobs[old_completed]["path_rules_json"] is not None)
    check("当前有效结论为空 → 工作台显示待重算", review.current_analysis(lid) is None)

    # 在途任务也要被作废
    lid2 = db.add_lift(version=0)
    for code in ["X", "Y", "Z"]:
        db.add_item(lid2, code)
    queued = review.create_review_job(lid2, 1, lambda j: None)["job_id"]
    review.mark_facts_changed(lid2, "复检改判")
    check("排队中的任务同样被作废", db.jobs[queued]["superseded"] == 1)

    # 重算后的新结论才是当前结论，归档里新旧都在
    new_job = review.create_review_job(lid, 1, lambda j: None)["job_id"]
    db.jobs[new_job]["status"] = "completed"
    current = review.current_analysis(lid)
    check("重算结论成为当前有效结论", current and current["id"] == new_job)
    history = review.latest_jobs(lid)
    check("归档保留全部复核（旧任务不被覆盖删除）", len(history) == 2)
    check("旧任务仍标记过期、新任务未过期",
          sorted(j["superseded"] for j in history) == [0, 1])


def test_certifiable_gate():
    print("\n[4] 签证采信闸门：只认版本匹配 + 未过期 + 已完成的任务")
    db = FakeDB()
    review = load_review_service(db)
    db_mod = sys.modules["src.db"]
    lid = db.add_lift(version=2)
    for code in ["A", "B", "C"]:
        db.add_item(lid, code)

    # 无任何任务
    with db_mod.transaction() as conn:
        lift, job = review.certifiable_analysis(conn, lid)
    check("没有复核任务时签证无结论可采信", job is None and lift["data_version"] == 2)

    # 旧版本完成任务
    stale = db.add_job(lid, status="completed", input_version=1, superseded=1)
    with db_mod.transaction() as conn:
        lift, job = review.certifiable_analysis(conn, lid)
    check("过期的旧版本结论不能用于签证", job is None)

    # 当前版本但仍在排队
    queued = db.add_job(lid, status="queued", input_version=2, superseded=0)
    with db_mod.transaction() as conn:
        lift, job = review.certifiable_analysis(conn, lid)
    check("在途任务不能用于签证（需等重算完成）", job is None)

    # 当前版本已完成
    db.jobs[queued]["status"] = "completed"
    with db_mod.transaction() as conn:
        lift, job = review.certifiable_analysis(conn, lid)
    check("版本匹配且完成的结论可采信", job and job["id"] == queued)

    # 采信期间事实又变更：版本推进后同一任务立即失效
    review.mark_facts_changed(lid, "签证前补录检验项")
    with db_mod.transaction() as conn:
        lift, job = review.certifiable_analysis(conn, lid)
    check("版本推进后原结论立即不可采信", job is None and lift["data_version"] == 3)
    check("原任务被标记过期", db.jobs[queued]["superseded"] == 1)
    check("旧任务记录未被删除，仍可审计", db.jobs[stale]["id"] in db.jobs)


def test_worker_stale_archive():
    print("\n[5] worker：迟到旧任务只归档，不回写电梯状态、不冲掉新复核")
    db = FakeDB()

    # 5a. 排队期间已被作废：直接跳过，不动任何状态
    lid = db.add_lift(status="under_review", version=1)
    stale_queued = db.add_job(lid, status="queued", input_version=0, superseded=1)
    worker = load_worker_module(db)
    worker.process(stale_queued)
    check("排队前已过期的任务不启动计算", db.jobs[stale_queued]["status"] == "queued")
    check("电梯状态不被回写", db.lifts[lid]["status"] == "under_review")

    # 5b. 正常任务完成并回写状态
    lid2 = db.add_lift(status="under_review", version=0)
    fresh = db.add_job(lid2, status="queued", input_version=0, superseded=0)
    worker.process(fresh)
    check("正常任务完成并写入分流与规则路径",
          db.jobs[fresh]["status"] == "completed" and db.jobs[fresh]["prediction"] == "ready"
          and "tree rules text" in db.jobs[fresh]["path_rules_json"])
    check("正常完成回写电梯状态", db.lifts[lid2]["status"] == "rectification")

    # 5c. 计算期间事实变更（running 之后被置 superseded）：结果归档但不回写状态
    lid3 = db.add_lift(status="under_review", version=1)
    racing = db.add_job(lid3, status="queued", input_version=0, superseded=0)
    orig = FakeCursor.execute

    def flips_superseded(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("UPDATE analysis_jobs SET status='running'"):
            orig(self, sql, params)
            db.jobs[racing]["superseded"] = 1  # 模拟并发事实变更
            return
        return orig(self, sql, params)

    FakeCursor.execute = flips_superseded
    try:
        worker.process(racing)
    finally:
        FakeCursor.execute = orig
    check("迟到结果仍落库归档（completed + 保留预测）",
          db.jobs[racing]["status"] == "completed" and db.jobs[racing]["prediction"] == "ready")
    check("归档任务保持 superseded=1", db.jobs[racing]["superseded"] == 1)
    check("迟到任务不回写电梯状态，新复核不被冲掉", db.lifts[lid3]["status"] == "under_review")
    check("归档原因自动补全", db.jobs[racing]["superseded_reason"] is not None)

    # 5d. 新复核任务不受迟到旧任务影响
    new_job = db.add_job(lid3, status="queued", input_version=1, superseded=0)
    worker.process(new_job)
    check("新复核正常完成且未被旧任务干扰",
          db.jobs[new_job]["status"] == "completed" and db.jobs[new_job]["superseded"] == 0)


def test_build_features_real():
    print("\n[6] 真实 build_features：特征计算口径不变")
    db = FakeDB()
    review = load_review_service(db)
    items = [
        {"result": "fail", "critical": True, "score": 42, "category": "safety"},
        {"result": "fail", "critical": False, "score": 74, "category": "mechanical"},
        {"result": "pass", "critical": False, "score": 90, "category": "safety"},
        {"result": "na", "critical": False, "score": 80, "category": "documentation"},
        {"result": "fail", "critical": False, "score": 66, "category": "documentation"},
    ]
    f = review.__dict__  # build_features 在桩模块里
    from src.algorithm import build_features
    feat = build_features(items)
    check("关键失败数=1", feat["critical_fail_count"] == 1, str(feat))
    check("失败总数=3", feat["failed_item_count"] == 3, str(feat))
    check("na 项不计入均分，均分=(42+74+90+66)/4=68.0", feat["average_score"] == 68.0, str(feat))
    check("资料类缺口=1", feat["documentation_gap"] == 1, str(feat))


if __name__ == "__main__":
    test_review_lifecycle()
    test_less_than_three_and_concurrent_guard()
    test_fact_change_expires_old_conclusion()
    test_certifiable_gate()
    test_worker_stale_archive()
    test_build_features_real()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
