CREATE DATABASE IF NOT EXISTS lift_review;
USE lift_review;

CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  email VARCHAR(120) UNIQUE NOT NULL,
  name VARCHAR(80) NOT NULL,
  password_hash CHAR(64) NOT NULL,
  role ENUM('operator','supervisor','viewer') NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS lifts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  asset_code VARCHAR(40) UNIQUE NOT NULL,
  building VARCHAR(120) NOT NULL,
  address VARCHAR(255) NOT NULL,
  manufacturer VARCHAR(80) NOT NULL,
  model VARCHAR(80) NOT NULL,
  rated_load INT NOT NULL,
  installed_year INT NOT NULL,
  inspection_due DATE NOT NULL,
  status ENUM('active','under_review','rectification','recheck','certified','suspended') NOT NULL DEFAULT 'active',
  data_version INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inspection_items (
  id INT AUTO_INCREMENT PRIMARY KEY,
  lift_id INT NOT NULL,
  code VARCHAR(40) NOT NULL,
  title VARCHAR(160) NOT NULL,
  category ENUM('safety','mechanical','electrical','documentation') NOT NULL,
  critical BOOLEAN NOT NULL DEFAULT FALSE,
  score DECIMAL(5,2) NOT NULL,
  result ENUM('pass','fail','na') NOT NULL,
  notes TEXT,
  photo_ref VARCHAR(160),
  inspected_at DATETIME NOT NULL,
  status ENUM('open','rectified','verified') NOT NULL DEFAULT 'open',
  UNIQUE KEY uq_lift_code (lift_id, code),
  FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rectification_notices (
  id INT AUTO_INCREMENT PRIMARY KEY,
  lift_id INT NOT NULL,
  inspection_item_id INT NOT NULL,
  issue_summary VARCHAR(255) NOT NULL,
  severity ENUM('critical','major','minor') NOT NULL,
  deadline DATE NOT NULL,
  status ENUM('open','submitted','verified','rejected','closed') NOT NULL DEFAULT 'open',
  evidence_required VARCHAR(160) NOT NULL,
  created_by INT NOT NULL,
  closed_at DATETIME NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE,
  FOREIGN KEY (inspection_item_id) REFERENCES inspection_items(id),
  FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS rechecks (
  id INT AUTO_INCREMENT PRIMARY KEY,
  notice_id INT NOT NULL,
  scheduled_at DATETIME NOT NULL,
  inspector VARCHAR(100) NOT NULL,
  status ENUM('scheduled','evidence_rejected','passed','failed') NOT NULL DEFAULT 'scheduled',
  photo_ref VARCHAR(160),
  photo_item_code VARCHAR(40),
  result ENUM('pass','fail') NULL,
  remarks TEXT,
  created_by INT NOT NULL,
  reviewed_by INT NULL,
  reviewed_at DATETIME NULL,
  FOREIGN KEY (notice_id) REFERENCES rectification_notices(id) ON DELETE CASCADE,
  FOREIGN KEY (created_by) REFERENCES users(id),
  FOREIGN KEY (reviewed_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS certificates (
  id INT AUTO_INCREMENT PRIMARY KEY,
  lift_id INT NOT NULL,
  certificate_no VARCHAR(60) UNIQUE NOT NULL,
  issued_at DATETIME NOT NULL,
  valid_until DATE NOT NULL,
  status ENUM('issued','revoked') NOT NULL DEFAULT 'issued',
  decision_reason TEXT NOT NULL,
  issued_by INT NOT NULL,
  analysis_job_id INT NULL,
  FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE,
  FOREIGN KEY (issued_by) REFERENCES users(id)
);

-- 复核任务带事实版本与原始输入快照；superseded=1 表示结论已过期，仅归档不得用于签证。
CREATE TABLE IF NOT EXISTS analysis_jobs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  lift_id INT NOT NULL,
  status ENUM('queued','running','completed','failed') NOT NULL DEFAULT 'queued',
  features_json JSON NOT NULL,
  prediction VARCHAR(40),
  probability DECIMAL(6,4),
  path_rules_json JSON,
  error TEXT,
  input_version INT NOT NULL DEFAULT 0,
  input_snapshot_json JSON NULL,
  superseded TINYINT(1) NOT NULL DEFAULT 0,
  superseded_reason VARCHAR(160) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  completed_at DATETIME NULL,
  KEY idx_jobs_lift_version (lift_id, superseded, id),
  FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  actor_id INT NULL,
  action VARCHAR(100) NOT NULL,
  entity_type VARCHAR(60) NOT NULL,
  entity_id INT NULL,
  detail TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (actor_id) REFERENCES users(id) ON DELETE SET NULL
);
