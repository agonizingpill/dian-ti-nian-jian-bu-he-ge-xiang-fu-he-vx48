# lift-inspection-review

电梯年检不合格项复核工作台，服务南京景诚特种设备检测团队。应用把电梯档案、逐项检验、整改通知、复检证据和合格证签发串成一条可审计流程。

## 业务范围

- 操作员录入设备与检验项，失败项生成整改通知并预约复检。
- 主管审核复检结果并签发合格证，安全回路与制动器关键项一票否决。
- 复检证据必须使用原检验项编码，照片错配会被后端拒绝。
- worker 使用 `DecisionTreeClassifier(random_state=1066, max_depth=3)` 计算分流结果并导出规则路径。

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
