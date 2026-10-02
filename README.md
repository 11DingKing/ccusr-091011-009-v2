# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

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

## 封存物资周期复核

部分封存物资必须按周期复查包装与保管条件，相关能力与库存阈值预警相互独立：

- **可版本化规则**：`POST /api/review-rules/` 按品类发布复核规则（周期天数、检查项、
  逾期宽限天数；`category` 不传时为通用兜底规则）。每次发布生成递增版本，旧版本自动
  作废且内容冻结，只能发布新版本，不能修改或删除历史版本。
- **待办批量生成**：`POST /api/review-tasks/generate/` 可重复执行（亦可由
  `apps.reports.cron.generate_review_tasks` 定时调用），按物资类型匹配规则，以首次入库
  日期或上次复核结论日期为基准生成待办；每个物资至多一条待复核待办（部分唯一索引
  兜底），重复执行不会产生重复数据。逾期未复核的物资会额外产生 `review_overdue` 预警。
- **结果登记**：`POST /api/review-tasks/<id>/complete/` 保存采用的规则版本外键及规则
  内容快照、异常项、复核结论和下一次期限；规则改版只影响之后的新结论，历史结果不变。
- **逾期放行审批**：逾期物资申请出库时标记 `requires_extra_approval`，常规审批后仍须
  调用 `POST /api/stock-out/<id>/extra-approval/` 完成额外审批，`release` 放行前会强制
  校验（`apps.warehouse.review.assert_release_allowed`），未通过额外审批的出库单无法放行。

