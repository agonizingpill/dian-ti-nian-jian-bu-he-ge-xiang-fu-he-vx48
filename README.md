# lift-inspection-review

电梯年检不合格项复核工作台，服务南京景诚特种设备检测团队。应用把电梯档案、逐项检验、整改通知、复检证据和合格证签发串成一条可审计流程。

## 业务范围

- 操作员录入设备与检验项，失败项生成整改通知并预约复检。
- 主管审核复检结果并签发合格证，安全回路与制动器关键项一票否决。
- 复检证据必须使用原检验项编码，照片错配会被后端拒绝。
- worker 使用 `DecisionTreeClassifier(random_state=1066, max_depth=3)` 计算分流结果并导出规则路径。

## 复核版本与签证裁决

- 每台电梯维护 `data_version` 事实版本号：补录检验项、复检改判（即使分值未变）都会在同一事务内推进版本，并把该电梯所有在途/已完成复核任务标记 `superseded`。
- 复核任务入队时快照当时的检验项原始输入（`input_snapshot_json`）、特征与 `input_version`；过期任务不删除、不覆盖，规则路径与原始输入全部保留可审计。
- 签证在单事务内 `SELECT ... FOR UPDATE` 锁定电梯行，只采信 `superseded=0`、`input_version` 等于当前版本且已完成的任务；证书落库写入 `analysis_job_id`，事后可通过 `/api/certificates/{id}/basis` 查到当时采信的复核。
- worker 对迟到的旧任务照常计算并归档为 `superseded`，不回写电梯状态，冲不掉新复核或已放行签证。
- 工作台明确区分「当前版本 · 可用于签证」与「结论已过期 · 待重算」，归档列表见 `/api/lifts/{id}/analysis-history`。

## 演示账号

| 账号 | 密码 | 权限 |
|---|---|---|
| operator@jingcheng.com | Demo@1066 | 设备、检验项、整改通知、复检预约 |
| supervisor@jingcheng.com | Demo@1066 | 操作员权限 + 复检审核、合格证签发 |
| viewer@jingcheng.com | Demo@1066 | 只读与审计轨迹 |

## 启动

```bash
docker compose up -d --build
docker compose exec -T backend python scripts/seed_demo.py
```

访问 `http://localhost:18265`。后端健康检查在 `http://localhost:19265/api/health`。
