"""封存物资周期复核：规则版本化、待办生成、结果登记、逾期放行审批。"""
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import (
    Approval, Category, Goods, ReviewRecord, ReviewRuleVersion, ReviewTask,
    StockIn, StockOut, Unit, Variety, Warning,
)
from apps.warehouse.review import (
    approve_overdue_release, assert_release_allowed, complete_review_task,
    generate_review_tasks, goods_review_overdue, publish_rule,
)
from django.test import TestCase


class ReviewFixture(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("review-admin", "testpass123", role="admin")
        self.user = User.objects.create_user("review-user", "testpass123", role="user")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.admin)}")

        self.unit = Unit.objects.create(name="件", created_by=self.admin)
        self.category = Category.objects.create(
            name="封存介质", unit=self.unit, created_by=self.admin
        )
        self.variety = Variety.objects.create(
            name="封存硬盘", category=self.category, created_by=self.admin
        )
        self.goods = Goods.objects.create(
            variety=self.variety,
            name="涉案硬盘A",
            code="EV-HDD-001",
            quantity=Decimal("10"),
            warning_threshold=Decimal("2"),
        )

    def stock_in_days_ago(self, days, quantity=Decimal("1")):
        """创建一条入库记录并把入库时间回溯到指定天数前。"""
        record = StockIn.objects.create(
            goods=self.goods, operator=self.admin, quantity=quantity
        )
        old_time = timezone.now() - timedelta(days=days)
        StockIn.objects.filter(pk=record.pk).update(stock_in_time=old_time)
        return record

    def publish_simple_rule(self, interval_days=30, category="default", check_items=None):
        return publish_rule(
            category=self.category if category == "default" else category,
            name="封存物资季度复核",
            interval_days=interval_days,
            check_items=check_items or ["外包装完好", "封存标签清晰", "保管温湿度正常"],
            overdue_grace_days=0,
            published_by=self.admin,
        )


class RuleVersioningTest(ReviewFixture):
    def test_publish_creates_incrementing_versions_and_deprecates_old(self):
        v1 = self.publish_simple_rule(interval_days=90)
        v2 = self.publish_simple_rule(interval_days=60)
        v1.refresh_from_db()
        self.assertEqual((v1.version, v2.version), (1, 2))
        self.assertEqual(v1.status, "deprecated")
        self.assertEqual(v2.status, "published")
        self.assertEqual(
            ReviewRuleVersion.active_for_category(self.category).id, v2.id
        )

    def test_published_rule_is_frozen(self):
        rule = self.publish_simple_rule(interval_days=90)
        rule.interval_days = 60
        with self.assertRaises(ValueError):
            rule.save()

    def test_category_rule_falls_back_to_global(self):
        # 未配置品类规则时使用通用规则
        global_rule = self.publish_simple_rule(interval_days=180, category=None)
        self.assertEqual(
            ReviewRuleVersion.active_for_category(self.category).id, global_rule.id
        )
        # 品类专用规则优先
        specific = self.publish_simple_rule(interval_days=30)
        self.assertEqual(
            ReviewRuleVersion.active_for_category(self.category).id, specific.id
        )

    def test_global_rule_version_constraint(self):
        publish_rule(
            category=None, name="通用规则", interval_days=180,
            check_items=["包装"], published_by=self.admin,
        )
        with self.assertRaises(IntegrityError):
            ReviewRuleVersion.objects.create(
                category=None, version=1, name="x", interval_days=10,
                check_items=[], status="published",
            )


class TaskGenerationTest(ReviewFixture):
    def test_due_date_based_on_stock_in_for_first_review(self):
        self.stock_in_days_ago(31)
        self.publish_simple_rule(interval_days=30)
        result = generate_review_tasks()
        self.assertEqual(result["created_count"], 1)
        task = ReviewTask.objects.get()
        baseline = self.goods.stock_ins.earliest("stock_in_time").stock_in_time.date()
        self.assertEqual(task.baseline_date, baseline)
        self.assertEqual(task.due_date, baseline + timedelta(days=30))
        self.assertTrue(task.is_overdue)

    def test_generation_is_idempotent(self):
        self.stock_in_days_ago(31)
        self.publish_simple_rule(interval_days=30)
        first = generate_review_tasks()
        second = generate_review_tasks()
        third = generate_review_tasks()
        self.assertEqual(first["created_count"], 1)
        self.assertEqual((second["created_count"], third["created_count"]), (0, 0))
        self.assertEqual(ReviewTask.objects.filter(status="pending").count(), 1)

    def test_no_task_without_rule_or_stock_in(self):
        result = generate_review_tasks()
        self.assertEqual(result["created_count"], 0)
        self.publish_simple_rule(interval_days=30)
        # 有规则但从未入库的物资不生成待办
        self.assertEqual(generate_review_tasks()["created_count"], 0)

    def test_overdue_generation_raises_warning(self):
        self.stock_in_days_ago(100)
        self.publish_simple_rule(interval_days=30)
        generate_review_tasks()
        warning = Warning.objects.get(goods=self.goods, type="review_overdue")
        self.assertFalse(warning.is_read)
        self.assertIn("已逾期", warning.message)

    def test_new_task_after_completion_uses_last_conclusion(self):
        self.stock_in_days_ago(40)
        self.publish_simple_rule(interval_days=30)
        generate_review_tasks()
        task = ReviewTask.objects.get(status="pending")
        review_day = date.today() - timedelta(days=10)
        complete_review_task(
            task=task, conclusion="qualified", reviewer=self.admin,
            review_date=review_day,
        )
        # 再次批量生成：新待办期限取上次结论的下次期限
        generate_review_tasks()
        new_task = ReviewTask.objects.get(status="pending")
        self.assertEqual(new_task.baseline_date, review_day)
        self.assertEqual(new_task.due_date, review_day + timedelta(days=30))
        self.assertEqual(ReviewTask.objects.count(), 2)


class ReviewRecordTest(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.stock_in_days_ago(40)
        self.rule = self.publish_simple_rule(interval_days=30)
        generate_review_tasks()
        self.task = ReviewTask.objects.get(status="pending")

    def test_record_snapshots_rule_abnormal_items_and_next_due(self):
        review_day = date.today()
        record = complete_review_task(
            task=self.task,
            conclusion="abnormal",
            reviewer=self.admin,
            abnormal_items=["外包装破损", "湿度记录缺失"],
            review_date=review_day,
            remark="已通知整改",
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "completed")
        self.assertEqual(record.rule_version_id, self.rule.id)
        self.assertEqual(record.rule_snapshot["version"], 1)
        self.assertEqual(record.rule_snapshot["interval_days"], 30)
        self.assertEqual(record.abnormal_items, ["外包装破损", "湿度记录缺失"])
        self.assertEqual(record.next_due_date, review_day + timedelta(days=30))

    def test_abnormal_requires_items_and_vice_versa(self):
        with self.assertRaises(ValueError):
            complete_review_task(task=self.task, conclusion="abnormal", abnormal_items=[])
        with self.assertRaises(ValueError):
            complete_review_task(
                task=self.task, conclusion="qualified", abnormal_items=["标签模糊"]
            )

    def test_completed_task_cannot_be_repeated(self):
        complete_review_task(task=self.task, conclusion="qualified")
        with self.assertRaises(ValueError):
            complete_review_task(task=self.task, conclusion="qualified")

    def test_rule_revision_does_not_change_history(self):
        review_day = date.today() - timedelta(days=20)
        record = complete_review_task(
            task=self.task, conclusion="qualified", reviewer=self.admin,
            review_date=review_day,
        )
        old_due = record.next_due_date
        old_snapshot = dict(record.rule_snapshot)

        # 规则改版：周期 30 -> 10
        self.publish_simple_rule(interval_days=10)
        record.refresh_from_db()
        self.assertEqual(record.rule_snapshot, old_snapshot)
        self.assertEqual(record.rule_snapshot["interval_days"], 30)
        self.assertEqual(record.next_due_date, old_due)
        # 历史引用的 v1 仍可查，状态为已作废而非删除
        self.assertEqual(record.rule_version.status, "deprecated")
        self.assertEqual(ReviewRecord.objects.get(pk=record.pk).next_due_date, old_due)

    def test_completing_review_closes_overdue_warning(self):
        # setUp 中入库已 40 天（周期 30 天），生成待办时已有一条逾期预警
        self.assertEqual(
            Warning.objects.filter(goods=self.goods, type="review_overdue").count(), 1
        )
        complete_review_task(task=self.task, conclusion="qualified")
        self.assertFalse(
            Warning.objects.filter(
                goods=self.goods, type="review_overdue", is_read=False
            ).exists()
        )


class OverdueReleaseTest(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.stock_in_days_ago(60)
        self.publish_simple_rule(interval_days=30)
        generate_review_tasks()
        task = ReviewTask.objects.get(status="pending")
        # 10 天前完成复核，下次期限已过 -> 当前逾期
        complete_review_task(
            task=task, conclusion="qualified", reviewer=self.admin,
            review_date=date.today() - timedelta(days=40),
        )
        self.stock_out = StockOut.objects.create(
            goods=self.goods, operator=self.user,
            receiver="办案民警", quantity=Decimal("1"), status="approved",
        )

    def test_overdue_detection(self):
        self.assertTrue(goods_review_overdue(self.goods))
        self.assertTrue(self.stock_out.requires_extra_approval)

    def test_release_blocked_without_extra_approval(self):
        with self.assertRaises(PermissionError):
            assert_release_allowed(self.stock_out)

    def test_extra_approval_then_release_allowed(self):
        approval = approve_overdue_release(
            stock_out=self.stock_out, approver=self.admin, remark="同意放行"
        )
        self.stock_out.refresh_from_db()
        self.assertEqual(approval.approval_type, "overdue_review")
        self.assertEqual(self.stock_out.extra_approval_id, approval.id)
        assert_release_allowed(self.stock_out)  # 不再抛异常

    def test_extra_approval_not_needed_when_not_overdue(self):
        fresh = Goods.objects.create(
            variety=self.variety, name="全新物资", code="EV-HDD-002",
            quantity=Decimal("5"), warning_threshold=Decimal("1"),
        )
        out = StockOut.objects.create(
            goods=fresh, operator=self.user, receiver="x",
            quantity=Decimal("1"), status="approved",
        )
        with self.assertRaises(ValueError):
            approve_overdue_release(stock_out=out, approver=self.admin)

    def test_grace_period(self):
        # 宽限 5 天：刚过期限 2 天不算逾期
        rule = self.publish_simple_rule(interval_days=30)
        ReviewRuleVersion.objects.filter(pk=rule.id).update(overdue_grace_days=5)
        goods2 = Goods.objects.create(
            variety=self.variety, name="宽限物资", code="EV-HDD-003",
            quantity=Decimal("5"), warning_threshold=Decimal("1"),
        )
        task2 = ReviewTask.objects.create(
            goods=goods2, rule_version=rule,
            baseline_date=date.today() - timedelta(days=32),
            due_date=date.today() - timedelta(days=2),
            status="completed",
        )
        record = ReviewRecord.objects.create(
            goods=goods2, task=task2, rule_version=rule,
            rule_snapshot=rule.snapshot(), conclusion="qualified",
            next_due_date=date.today() - timedelta(days=2),
            reviewer=self.admin, review_date=date.today() - timedelta(days=32),
        )
        self.assertFalse(goods_review_overdue(goods2))
        ReviewRecord.objects.filter(pk=record.pk).update(
            next_due_date=date.today() - timedelta(days=6)
        )
        self.assertTrue(goods_review_overdue(goods2))


class ReviewAPITest(ReviewFixture):
    def test_full_flow_api(self):
        self.stock_in_days_ago(31)
        # 发布规则
        resp = self.client.post("/api/review-rules/", {
            "name": "季度复核", "interval_days": 30,
            "check_items": ["包装", "标签"],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["version"], 1)

        # 普通用户不能发布规则
        user_client = APIClient()
        user_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        forbidden = user_client.post("/api/review-rules/", {
            "name": "x", "interval_days": 30, "check_items": ["a"],
        }, format="json")
        self.assertEqual(forbidden.status_code, 403)

        # 重复批量生成
        r1 = self.client.post("/api/review-tasks/generate/")
        r2 = self.client.post("/api/review-tasks/generate/")
        self.assertEqual(r1.json()["data"]["created_count"], 1)
        self.assertEqual(r2.json()["data"]["created_count"], 0)

        task_id = ReviewTask.objects.get().id

        # 异常结论不带异常项应被拒
        bad = self.client.post(f"/api/review-tasks/{task_id}/complete/", {
            "conclusion": "abnormal",
        }, format="json")
        self.assertEqual(bad.status_code, 400)

        # 正常登记
        ok = self.client.post(f"/api/review-tasks/{task_id}/complete/", {
            "conclusion": "abnormal",
            "abnormal_items": ["外包装破损"],
        }, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        data = ok.json()["data"]
        self.assertEqual(data["rule_snapshot"]["version"], 1)
        self.assertIn("next_due_date", data)

        # 历史结果可查
        records = self.client.get("/api/review-records/")
        self.assertEqual(records.json()["data"]["total"], 1)

    def test_overdue_release_api_gate(self):
        self.stock_in_days_ago(60)
        self.publish_simple_rule(interval_days=30)
        generate_review_tasks()
        task = ReviewTask.objects.get()
        complete_review_task(
            task=task, conclusion="qualified",
            review_date=date.today() - timedelta(days=40),
        )

        out = self.client.post("/api/stock-out/", {
            "goods": self.goods.id, "quantity": "1", "receiver": "民警",
        }, format="json")
        self.assertEqual(out.status_code, 200, out.content)
        self.assertTrue(out.json()["data"]["requires_extra_approval"])
        out_id = out.json()["data"]["id"]

        # 常规审批通过
        approved = self.client.post(f"/api/stock-out/{out_id}/approve/",
                                    {"action": "approve"}, format="json")
        self.assertEqual(approved.status_code, 200, approved.content)

        # 未额外审批直接放行被拒
        blocked = self.client.post(f"/api/stock-out/{out_id}/release/")
        self.assertEqual(blocked.status_code, 400)

        # 额外审批后放行成功，库存扣减
        extra = self.client.post(f"/api/stock-out/{out_id}/extra-approval/",
                                 {"remark": "情况紧急，同意"}, format="json")
        self.assertEqual(extra.status_code, 200, extra.content)

        released = self.client.post(f"/api/stock-out/{out_id}/release/")
        self.assertEqual(released.status_code, 200, released.content)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("9"))
        self.assertEqual(
            Approval.objects.filter(
                stock_out_id=out_id, approval_type="overdue_review", status="approved"
            ).count(),
            1,
        )

    def test_review_task_list_filters_overdue(self):
        self.stock_in_days_ago(100)
        self.publish_simple_rule(interval_days=30)
        generate_review_tasks()
        resp = self.client.get("/api/review-tasks/?status=pending&overdue=true")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 1)
        self.assertTrue(resp.json()["data"]["list"][0]["is_overdue"])
