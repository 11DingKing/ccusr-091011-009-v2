# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 封存物资周期复核

部分封存物资需按周期复查包装与保管条件，`apps.inspections` 提供独立于库存阈值预警的复核闭环：

- **可版本化规则** `ReviewRule`：规则按名称维护多个版本（草稿/生效中/已停用）。已发布版本冻结不可改，改版须派生新版本；发布新版本自动停用同名旧版本。规则可按品类配置，未配置品类时回退通用规则，字段含常规周期、首次宽限、异常后加严周期与检查项目。
- **待办生成**：`generate_tasks()`（可由管理命令调用，或 `POST /api/review-tasks/generate/`）按物资类型（品类）、入库日期（首检期限）与上次复核结论（异常走加严周期）生成待办。待办在生成时固化规则快照，`(物资, 序号)` 唯一约束 + 待办存在性检查保证批量任务重复执行不产生重复待办。
- **检查结果** `ReviewResult`：登记时固化采用的规则及内容快照、异常项与下一次期限。已完成待办不可重复登记，规则改版不会改变任何历史结论。
- **逾期与放行**：逾期未完成的待办进入逾期预警（`review_overdue`）；对应物资的出库单在放行（`POST /api/stock-out/<id>/release/`）前会被拦截，必须先经 `ReleaseApproval` 额外审批通过。

定时入口：`apps.inspections.cron.scan_reviews(within_days=7)`，建议每日执行（生成临期待办并产出逾期预警）。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
