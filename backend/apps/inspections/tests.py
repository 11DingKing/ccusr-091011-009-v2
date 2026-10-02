"""
周期复核领域测试：
- 可版本化规则（已发布冻结、改版产生新版本）
- 按物资类型/入库日期/上次结论生成待办
- 批量生成幂等
- 结果固化规则快照、异常项、下次期限
- 规则改版不改变历史结论
- 逾期放行需额外审批
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import (
    Category, Goods, StockIn, StockOut, Unit, Variety, Warning,
)

from .models import ReleaseApproval, ReviewResult, ReviewRule, ReviewTask
from .services import (
    ReviewError, can_release, complete_review, create_overdue_warnings,
    decide_release_approval, generate_tasks, release_blockers, release_stock_out,
    request_release_approval,
)


class InspectionFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("insp-user", "testpass123", role="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.unit = Unit.objects.create(name="件", created_by=self.user)
        self.category = Category.objects.create(
            name="封存介质", unit=self.unit, created_by=self.user
        )
        self.variety = Variety.objects.create(
            name="封存硬盘", category=self.category, created_by=self.user
        )
        # 入库日期固定为 40 天前
        self.stock_in_date = timezone.now() - timedelta(days=40)
        self.goods = Goods.objects.create(
            variety=self.variety, name="涉案硬盘A", code="EV-001",
            quantity=Decimal("1"), warning_threshold=Decimal("5"),
        )
        StockIn.objects.create(
            goods=self.goods, operator=self.user, quantity=Decimal("1"),
        )
        # stock_in_time 为 auto_now_add，需通过 update 固定入库日期
        StockIn.objects.filter(goods=self.goods).update(
            stock_in_time=self.stock_in_date
        )

    def make_rule(self, **kw):
        defaults = dict(
            name="封存介质复核", category=self.category, interval_days=30,
            initial_grace_days=30, abnormal_interval_days=15,
            check_items=["包装完好性", "封条完整性"],
            created_by=self.user,
        )
        defaults.update(kw)
        return ReviewRule.objects.create(**defaults)


class RuleVersioningTest(InspectionFixture):
    def test_draft_editable_published_frozen(self):
        rule = self.make_rule()
        self.assertTrue(rule.is_editable)
        rule.interval_days = 20
        rule.save()
        rule.publish()
        rule.refresh_from_db()
        self.assertEqual(rule.status, ReviewRule.STATUS_PUBLISHED)
        self.assertFalse(rule.is_editable)

    def test_publish_deprecates_previous_version(self):
        v1 = self.make_rule(version=1)
        v1.publish()
        v2 = self.make_rule(version=2, interval_days=10)
        v2.publish()
        v1.refresh_from_db()
        self.assertEqual(v1.status, ReviewRule.STATUS_DEPRECATED)
        # 生效规则取新版本
        self.assertEqual(ReviewRule.active_for_category(self.category.id).id, v2.id)

    def test_new_version_increments_and_keeps_history(self):
        v1 = self.make_rule(version=1)
        v1.publish()
        draft = v1.new_version(interval_days=45, created_by=self.user)
        self.assertEqual(draft.version, 2)
        self.assertEqual(draft.interval_days, 45)
        self.assertEqual(draft.status, ReviewRule.STATUS_DRAFT)
        # v1 内容与状态保持不变
        v1.refresh_from_db()
        self.assertEqual(v1.interval_days, 30)
        self.assertEqual(v1.status, ReviewRule.STATUS_PUBLISHED)

    def test_generic_rule_fallback(self):
        generic = self.make_rule(name="通用复核", category=None)
        generic.publish()
        # 无品类专用规则时回退通用规则
        self.assertEqual(ReviewRule.active_for_category(self.category.id).id, generic.id)
        specific = self.make_rule(version=2)
        specific.publish()
        self.assertEqual(ReviewRule.active_for_category(self.category.id).id, specific.id)


class TaskGenerationTest(InspectionFixture):
    def test_first_task_due_uses_inbound_date_and_grace(self):
        self.make_rule().publish()
        stats = generate_tasks()
        self.assertEqual(stats['created'], 1)
        task = ReviewTask.objects.get(goods=self.goods)
        expected = timezone.localtime(self.stock_in_date).date() + timedelta(days=30)
        self.assertEqual(task.due_date, expected)
        self.assertEqual(task.sequence, 1)
        # 生成时固化规则快照
        self.assertEqual(task.rule_snapshot['interval_days'], 30)
        self.assertEqual(task.rule_snapshot['check_items'], ["包装完好性", "封条完整性"])

    def test_generation_is_idempotent(self):
        self.make_rule().publish()
        first = generate_tasks()
        second = generate_tasks()
        third = generate_tasks(within_days=3650)
        self.assertEqual(first['created'], 1)
        self.assertEqual(second['created'], 0)
        self.assertEqual(third['created'], 0)
        self.assertEqual(ReviewTask.objects.filter(goods=self.goods).count(), 1)

    def test_next_task_uses_last_conclusion_abnormal_interval(self):
        rule = self.make_rule()
        rule.publish()
        generate_tasks()
        task = ReviewTask.objects.get(goods=self.goods, sequence=1)
        result = complete_review(
            task, inspector=self.user,
            conclusion=ReviewResult.CONCLUSION_ABNORMAL,
            abnormal_items=[{"item": "封条完整性", "detail": "封条松动"}],
        )
        # 异常 -> 下次期限按加严周期 15 天
        self.assertEqual(
            result.next_due_date,
            timezone.localdate() + timedelta(days=15),
        )
        # 完成后再次生成：序号 2，期限沿用上次结论给出的下次期限
        generate_tasks()
        task2 = ReviewTask.objects.get(goods=self.goods, sequence=2)
        self.assertEqual(task2.due_date, result.next_due_date)

    def test_normal_conclusion_uses_regular_interval(self):
        self.make_rule().publish()
        generate_tasks()
        task = ReviewTask.objects.get(sequence=1)
        result = complete_review(
            task, inspector=self.user, conclusion=ReviewResult.CONCLUSION_NORMAL
        )
        self.assertEqual(
            result.next_due_date, timezone.localdate() + timedelta(days=30)
        )

    def test_horizon_skips_far_future_tasks(self):
        # 入库在今天，首检 30 天后；只生成 7 天内到期的待办时应跳过
        Goods.objects.filter(pk=self.goods.pk).update(created_at=timezone.now())
        StockIn.objects.filter(goods=self.goods).update(stock_in_time=timezone.now())
        self.make_rule().publish()
        stats = generate_tasks(within_days=7)
        self.assertEqual(stats['created'], 0)
        stats = generate_tasks(within_days=3650)
        self.assertEqual(stats['created'], 1)

    def test_no_rule_skips_goods(self):
        stats = generate_tasks()
        self.assertEqual(stats['created'], 0)
        self.assertEqual(ReviewTask.objects.count(), 0)


class ResultImmutabilityTest(InspectionFixture):
    def test_result_snapshots_rule_and_rule_change_keeps_history(self):
        v1 = self.make_rule(version=1, interval_days=30)
        v1.publish()
        generate_tasks()
        task = ReviewTask.objects.get(sequence=1)
        result = complete_review(
            task, inspector=self.user,
            conclusion=ReviewResult.CONCLUSION_ABNORMAL,
            abnormal_items=[{"item": "包装完好性", "detail": "外箱破损"}],
        )
        snapshot_v1 = result.rule_snapshot
        self.assertEqual(snapshot_v1['version'], 1)
        self.assertEqual(snapshot_v1['interval_days'], 30)

        # 规则改版：发布 v2，周期 10 天
        v2 = self.make_rule(version=2, interval_days=10, abnormal_interval_days=5)
        v2.publish()

        # 历史结论不变：外键指向 v1，快照仍是 v1 内容
        result.refresh_from_db()
        self.assertEqual(result.rule_id, v1.id)
        self.assertEqual(result.rule_snapshot, snapshot_v1)
        task.refresh_from_db()
        self.assertEqual(task.rule_id, v1.id)
        self.assertEqual(task.status, ReviewTask.STATUS_COMPLETED)

        # 已完成待办不能重复登记
        with self.assertRaises(ReviewError):
            complete_review(
                task, inspector=self.user, conclusion=ReviewResult.CONCLUSION_NORMAL
            )

    def test_abnormal_requires_items(self):
        self.make_rule().publish()
        generate_tasks()
        task = ReviewTask.objects.get(sequence=1)
        with self.assertRaises(ReviewError):
            complete_review(
                task, inspector=self.user,
                conclusion=ReviewResult.CONCLUSION_ABNORMAL,
            )


class OverdueReleaseTest(InspectionFixture):
    def _overdue_task(self):
        self.make_rule().publish()
        generate_tasks()
        task = ReviewTask.objects.get(sequence=1)
        # 将期限改为过去，制造逾期
        ReviewTask.objects.filter(pk=task.pk).update(
            due_date=timezone.localdate() - timedelta(days=1)
        )
        task.refresh_from_db()
        self.assertTrue(task.is_overdue)
        return task

    def _stock_out(self):
        return StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="领用部门",
            quantity=Decimal("1"),
        )

    def test_overdue_blocks_release(self):
        self._overdue_task()
        stock_out = self._stock_out()
        self.assertFalse(can_release(stock_out))
        self.assertEqual(len(release_blockers(stock_out)), 1)
        with self.assertRaises(ReviewError):
            release_stock_out(stock_out)
        stock_out.refresh_from_db()
        self.assertNotEqual(stock_out.status, 'completed')

    def test_approved_extra_approval_allows_release(self):
        task = self._overdue_task()
        stock_out = self._stock_out()
        approval = request_release_approval(stock_out, task)
        self.assertEqual(approval.status, ReleaseApproval.STATUS_PENDING)
        decide_release_approval(approval, approver=self.user, approved=True, remark="特批")
        self.assertTrue(can_release(stock_out))
        release_stock_out(stock_out)
        stock_out.refresh_from_db()
        self.assertEqual(stock_out.status, 'completed')

    def test_rejected_approval_still_blocks(self):
        task = self._overdue_task()
        stock_out = self._stock_out()
        approval = request_release_approval(stock_out, task)
        decide_release_approval(approval, approver=self.user, approved=False)
        self.assertFalse(can_release(stock_out))

    def test_release_request_idempotent(self):
        task = self._overdue_task()
        stock_out = self._stock_out()
        a1 = request_release_approval(stock_out, task)
        a2 = request_release_approval(stock_out, task)
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(ReleaseApproval.objects.count(), 1)

    def test_cannot_request_for_non_overdue(self):
        self.make_rule().publish()
        generate_tasks()
        fresh_task = ReviewTask.objects.get(sequence=1)
        ReviewTask.objects.filter(pk=fresh_task.pk).update(
            due_date=timezone.localdate() + timedelta(days=10)
        )
        fresh_task.refresh_from_db()
        stock_out = self._stock_out()
        with self.assertRaises(ReviewError):
            request_release_approval(stock_out, fresh_task)

    def test_completed_review_does_not_block(self):
        task = self._overdue_task()
        stock_out = self._stock_out()
        complete_review(
            task, inspector=self.user, conclusion=ReviewResult.CONCLUSION_NORMAL
        )
        self.assertTrue(can_release(stock_out))
        release_stock_out(stock_out)
        self.assertEqual(stock_out.status, 'completed')

    def test_overdue_warning_generated_once(self):
        self._overdue_task()
        self.assertEqual(create_overdue_warnings(), 1)
        self.assertEqual(create_overdue_warnings(), 0)
        self.assertTrue(
            Warning.objects.filter(goods=self.goods, type='review_overdue').exists()
        )


class InspectionAPITest(InspectionFixture):
    def test_rule_lifecycle_and_task_flow_api(self):
        # 创建草稿
        r = self.client.post('/api/review-rules/', {
            'name': '封存介质复核', 'category': self.category.id,
            'interval_days': 30, 'initial_grace_days': 30,
            'abnormal_interval_days': 15,
            'check_items': ['包装完好性', '封条完整性'],
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        rule_id = r.json()['data']['id']
        self.assertEqual(r.json()['data']['version'], 1)

        # 已发布不可改
        self.client.post(f'/api/review-rules/{rule_id}/publish/')
        blocked = self.client.put(f'/api/review-rules/{rule_id}/', {
            'name': '封存介质复核', 'interval_days': 10,
        }, format='json')
        self.assertEqual(blocked.status_code, 400)

        # 生成待办（执行两次验证幂等）
        g1 = self.client.post('/api/review-tasks/generate/', {}, format='json')
        g2 = self.client.post('/api/review-tasks/generate/', {}, format='json')
        self.assertEqual(g1.json()['data']['created'], 1)
        self.assertEqual(g2.json()['data']['created'], 0)

        task_id = ReviewTask.objects.get().id

        # 逾期后放行被拦截
        ReviewTask.objects.filter(pk=task_id).update(
            due_date=timezone.localdate() - timedelta(days=1)
        )
        stock_out = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="x", quantity=Decimal("1"),
        )
        blocked_release = self.client.post(f'/api/stock-out/{stock_out.id}/release/')
        self.assertEqual(blocked_release.status_code, 403)

        # 查询阻塞项
        blockers = self.client.get(f'/api/stock-out/{stock_out.id}/review-blockers/')
        self.assertTrue(blockers.json()['data']['blocked'])

        # 提交并通过额外审批
        ar = self.client.post('/api/release-approvals/', {
            'stock_out': stock_out.id, 'task': task_id,
        }, format='json')
        self.assertEqual(ar.status_code, 200, ar.content)
        approval_id = ar.json()['data']['id']
        dr = self.client.post(f'/api/release-approvals/{approval_id}/decide/', {
            'approved': True, 'remark': '同意放行',
        }, format='json')
        self.assertEqual(dr.status_code, 200)

        released = self.client.post(f'/api/stock-out/{stock_out.id}/release/')
        self.assertEqual(released.status_code, 200)

    def test_complete_review_api_snapshots_rule(self):
        rule = self.make_rule()
        rule.publish()
        generate_tasks()
        task = ReviewTask.objects.get()
        r = self.client.post(f'/api/review-tasks/{task.id}/complete/', {
            'conclusion': 'abnormal',
            'abnormal_items': [{'item': '封条完整性', 'detail': '松动'}],
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()['data']
        self.assertEqual(data['rule_snapshot']['version'], 1)
        self.assertEqual(len(data['abnormal_items']), 1)
        self.assertIsNotNone(data['next_due_date'])

        # 再次完成应被拒绝
        again = self.client.post(f'/api/review-tasks/{task.id}/complete/', {
            'conclusion': 'normal',
        }, format='json')
        self.assertEqual(again.status_code, 400)
