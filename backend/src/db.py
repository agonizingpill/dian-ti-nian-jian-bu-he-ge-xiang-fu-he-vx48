import os
import time
from contextlib import contextmanager
import pymysql

DB = {
    "host": os.getenv("MYSQL_HOST", "db"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "lift_user"),
    "password": os.getenv("MYSQL_PASSWORD", "lift_pass"),
    "database": os.getenv("MYSQL_DATABASE", "lift_review"),
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": True,
}

SCHEMA = [
"""CREATE TABLE IF NOT EXISTS users (id INT AUTO_INCREMENT PRIMARY KEY, email VARCHAR(120) UNIQUE NOT NULL, name VARCHAR(80) NOT NULL, password_hash CHAR(64) NOT NULL, role ENUM('operator','supervisor','viewer') NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
"""CREATE TABLE IF NOT EXISTS lifts (id INT AUTO_INCREMENT PRIMARY KEY, asset_code VARCHAR(40) UNIQUE NOT NULL, building VARCHAR(120) NOT NULL, address VARCHAR(255) NOT NULL, manufacturer VARCHAR(80) NOT NULL, model VARCHAR(80) NOT NULL, rated_load INT NOT NULL, installed_year INT NOT NULL, inspection_due DATE NOT NULL, status ENUM('active','under_review','rectification','recheck','certified','suspended') NOT NULL DEFAULT 'active', data_version INT NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
"""CREATE TABLE IF NOT EXISTS inspection_items (id INT AUTO_INCREMENT PRIMARY KEY, lift_id INT NOT NULL, code VARCHAR(40) NOT NULL, title VARCHAR(160) NOT NULL, category ENUM('safety','mechanical','electrical','documentation') NOT NULL, critical BOOLEAN NOT NULL DEFAULT FALSE, score DECIMAL(5,2) NOT NULL, result ENUM('pass','fail','na') NOT NULL, notes TEXT, photo_ref VARCHAR(160), inspected_at DATETIME NOT NULL, status ENUM('open','rectified','verified') NOT NULL DEFAULT 'open', UNIQUE KEY uq_lift_code (lift_id, code), FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE)""",
"""CREATE TABLE IF NOT EXISTS rectification_notices (id INT AUTO_INCREMENT PRIMARY KEY, lift_id INT NOT NULL, inspection_item_id INT NOT NULL, issue_summary VARCHAR(255) NOT NULL, severity ENUM('critical','major','minor') NOT NULL, deadline DATE NOT NULL, status ENUM('open','submitted','verified','rejected','closed') NOT NULL DEFAULT 'open', evidence_required VARCHAR(160) NOT NULL, created_by INT NOT NULL, closed_at DATETIME NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE, FOREIGN KEY (inspection_item_id) REFERENCES inspection_items(id), FOREIGN KEY (created_by) REFERENCES users(id))""",
"""CREATE TABLE IF NOT EXISTS rechecks (id INT AUTO_INCREMENT PRIMARY KEY, notice_id INT NOT NULL, scheduled_at DATETIME NOT NULL, inspector VARCHAR(100) NOT NULL, status ENUM('scheduled','evidence_rejected','passed','failed') NOT NULL DEFAULT 'scheduled', photo_ref VARCHAR(160), photo_item_code VARCHAR(40), result ENUM('pass','fail') NULL, remarks TEXT, created_by INT NOT NULL, reviewed_by INT NULL, reviewed_at DATETIME NULL, FOREIGN KEY (notice_id) REFERENCES rectification_notices(id) ON DELETE CASCADE, FOREIGN KEY (created_by) REFERENCES users(id), FOREIGN KEY (reviewed_by) REFERENCES users(id))""",
"""CREATE TABLE IF NOT EXISTS certificates (id INT AUTO_INCREMENT PRIMARY KEY, lift_id INT NOT NULL, certificate_no VARCHAR(60) UNIQUE NOT NULL, issued_at DATETIME NOT NULL, valid_until DATE NOT NULL, status ENUM('issued','revoked') NOT NULL DEFAULT 'issued', decision_reason TEXT NOT NULL, issued_by INT NOT NULL, analysis_job_id INT NULL, FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE, FOREIGN KEY (issued_by) REFERENCES users(id))""",
"""CREATE TABLE IF NOT EXISTS analysis_jobs (id INT AUTO_INCREMENT PRIMARY KEY, lift_id INT NOT NULL, status ENUM('queued','running','completed','failed') NOT NULL DEFAULT 'queued', features_json JSON NOT NULL, prediction VARCHAR(40), probability DECIMAL(6,4), path_rules_json JSON, error TEXT, input_version INT NOT NULL DEFAULT 0, input_snapshot_json JSON NULL, superseded TINYINT(1) NOT NULL DEFAULT 0, superseded_reason VARCHAR(160) NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME NULL, KEY idx_jobs_lift_version (lift_id, superseded, id), FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE)""",
"""CREATE TABLE IF NOT EXISTS audit_logs (id INT AUTO_INCREMENT PRIMARY KEY, actor_id INT NULL, action VARCHAR(100) NOT NULL, entity_type VARCHAR(60) NOT NULL, entity_id INT NULL, detail TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (actor_id) REFERENCES users(id) ON DELETE SET NULL)""",
]

# 增量迁移：老库通过 init_schema 幂等补齐新列，避免依赖一次性 SQL 迁移文件。
ALTER_COLUMNS = [
    ("lifts", "data_version", "ALTER TABLE lifts ADD COLUMN data_version INT NOT NULL DEFAULT 0"),
    ("analysis_jobs", "input_version", "ALTER TABLE analysis_jobs ADD COLUMN input_version INT NOT NULL DEFAULT 0"),
    ("analysis_jobs", "input_snapshot_json", "ALTER TABLE analysis_jobs ADD COLUMN input_snapshot_json JSON NULL"),
    ("analysis_jobs", "superseded", "ALTER TABLE analysis_jobs ADD COLUMN superseded TINYINT(1) NOT NULL DEFAULT 0"),
    ("analysis_jobs", "superseded_reason", "ALTER TABLE analysis_jobs ADD COLUMN superseded_reason VARCHAR(160) NULL"),
    ("certificates", "analysis_job_id", "ALTER TABLE certificates ADD COLUMN analysis_job_id INT NULL"),
]


@contextmanager
def connection(retries=30):
    last = None
    for _ in range(retries):
        try:
            conn = pymysql.connect(**DB)
            try:
                yield conn
            finally:
                conn.close()
            return
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise last


@contextmanager
def transaction():
    """显式事务：行锁与多表状态落定必须在同一连接内提交。"""
    with connection() as conn:
        conn.autocommit(False)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit(True)


def _column_exists(cur, table, column):
    cur.execute(
        "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table, column),
    )
    return cur.fetchone() is not None


def init_schema():
    with connection() as conn:
        with conn.cursor() as cur:
            for statement in SCHEMA:
                cur.execute(statement)
            for table, column, ddl in ALTER_COLUMNS:
                if not _column_exists(cur, table, column):
                    cur.execute(ddl)
            cur.execute(
                "SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='analysis_jobs' AND INDEX_NAME='idx_jobs_lift_version'"
            )
            if not cur.fetchone():
                cur.execute("CREATE INDEX idx_jobs_lift_version ON analysis_jobs(lift_id, superseded, id)")
