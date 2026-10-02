"""
封存物资周期复核领域服务。

职责：
- 复核规则的版本化发布（发布后冻结、旧版本自动作废）；
- 按物资类型、入库日期和上次复核结论批量生成复核待办（幂等）；
- 登记复核结果（快照规则、异常项、下一次期限）；
- 逾期物资出库放行前的额外审批校验。
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    Approval, Goods, ReviewRecord, ReviewRuleVersion, ReviewTask, StockOut, Warning,
)

logger = logging.getLogger('apps')


# ==================== 规则版本 ====================

@transaction.atomic
def publish_rule(*, category, name, interval_days, check_items,
                 overdue_grace_days=0, published_by=None):
    """发布一个新版本的复核规则。

    同品类（或通用规则）在发布瞬间作废旧版本，已完成的历史结论
    仍引用旧版本记录，规则改版不会改写历史。
    """
    scope = ReviewRuleVersion.objects.filter(category=category)
    latest = scope.order_by('-version').first()
    next_version = (latest.version + 1) if latest else 1

    rule = ReviewRuleVersion.objects.create(
        category=category,
        version=next_version,
        name=name,
        interval_days=interval_days,
        check_items=list(check_items or []),
        overdue_grace_days=overdue_grace_days,
        status='published',
        published_at=timezone.now(),
        created_by=published_by,
    )

    # 旧版本立即作废，不删除（历史结论需要引用）
    scope.filter(status='published').exclude(pk=rule.pk).update(
        status='deprecated', deprecated_at=timezone.now()
    )

    logger.info(
        "发布复核规则: %s v%s（周期 %s 天）", rule, next_version, interval_days
    )
    return rule


# ==================== 待办生成 ====================

def _baseline_and_rule(goods):
    """返回物资的基准日期、依据规则版本、最近一次复核结论。

    基准日期：上次复核结论的复核日期；从未复核的取首次入库日期。
    规则：物资品类的专用规则优先，其次通用规则。
    """
    rule = ReviewRuleVersion.active_for_category(goods.variety.category)
    if rule is None:
        return None, None, None

    latest = goods.review_records.order_by('-review_date', '-id').first()
    if latest is not None:
        baseline_date = latest.review_date
    else:
        first_in = goods.stock_ins.order_by('stock_in_time').first()
        if first_in is None:
            return None, rule, None
        baseline_date = timezone.localdate(first_in.stock_in_time)

    return baseline_date, rule, latest


def _due_date(baseline_date, rule, latest_record):
    if latest_record is not None and latest_record.next_due_date:
        return latest_record.next_due_date
    return baseline_date + timedelta(days=rule.interval_days)


@transaction.atomic
def generate_review_tasks(now=None):
    """批量生成复核待办。

    重复执行是幂等的：每个物资至多存在一条 pending 待办
    （数据库部分唯一索引兜底），已完成/已取消的待办保留。
    规则改版只对之后生成的待办生效。
    """
    today = timezone.localdate(now) if now else timezone.localdate()
    created, skipped = [], 0

    goods_qs = (
        Goods.objects.filter(is_active=True)
        .select_related('variety__category')
    )

    for goods in goods_qs:
        baseline_date, rule, latest = _baseline_and_rule(goods)
        if rule is None or baseline_date is None:
            skipped += 1
            continue

        # 已有待复核待办：幂等跳过（不重复创建，也不改期限）
        if ReviewTask.objects.filter(goods=goods, status='pending').exists():
            skipped += 1
            continue

        due_date = _due_date(baseline_date, rule, latest)
        try:
            with transaction.atomic():
                task = ReviewTask.objects.create(
                    goods=goods,
                    rule_version=rule,
                    baseline_date=baseline_date,
                    due_date=due_date,
                )
        except IntegrityError:
            # 并发场景下部分唯一索引拦截，视为已存在
            skipped += 1
            continue
        created.append(task)

        # 逾期仍未复核：产生预警（现有库存阈值预警覆盖不到的缺口）
        grace_due = due_date + timedelta(days=rule.overdue_grace_days)
        if today > grace_due:
            _raise_overdue_warning(goods, due_date)

    logger.info(
        "复核待办批量生成完成: 新增 %s 条，跳过 %s 条", len(created), skipped
    )
    return {'created': created, 'created_count': len(created), 'skipped_count': skipped}


def _raise_overdue_warning(goods, due_date):
    exists = Warning.objects.filter(
        goods=goods, type='review_overdue', is_read=False
    ).exists()
    if not exists:
        Warning.objects.create(
            goods=goods,
            type='review_overdue',
            message=(
                f'货物 {goods.name}（{goods.code}）封存复核已逾期，'
                f'应于 {due_date} 前完成包装及保管条件复核'
            ),
        )


# ==================== 结果登记 ====================

@transaction.atomic
def complete_review_task(*, task, conclusion, reviewer=None,
                         abnormal_items=None, review_date=None, remark=''):
    """登记复核结果并关闭待办。

    结果中保存采用的规则版本外键和规则内容快照、异常项、
    下一次复核期限。规则改版不会改变这里的历史结论。
    """
    if task.status != 'pending':
        raise ValueError('该待办已处理，不能重复登记')

    review_date = review_date or timezone.localdate()
    rule = task.rule_version
    abnormal_items = list(abnormal_items or [])

    if conclusion == 'abnormal' and not abnormal_items:
        raise ValueError('复核结论为异常时必须填写异常项')
    if conclusion == 'qualified' and abnormal_items:
        raise ValueError('复核结论为合格时不能登记异常项')

    record = ReviewRecord.objects.create(
        goods=task.goods,
        task=task,
        rule_version=rule,
        rule_snapshot=rule.snapshot(),
        conclusion=conclusion,
        abnormal_items=abnormal_items,
        next_due_date=review_date + timedelta(days=rule.interval_days),
        reviewer=reviewer,
        review_date=review_date,
        remark=remark,
    )

    task.status = 'completed'
    task.save(update_fields=['status', 'updated_at'])

    # 该物资的逾期预警随之关闭
    Warning.objects.filter(
        goods=task.goods, type='review_overdue', is_read=False
    ).update(is_read=True)

    logger.info("复核结果登记: %s -> %s", task.goods.name, conclusion)
    return record


# ==================== 逾期放行审批 ====================

def goods_review_overdue(goods, on_date=None):
    """物资在指定日期是否复核逾期（超过最近结论的下次期限，含宽限期）"""
    on_date = on_date or timezone.localdate()
    latest = goods.review_records.order_by('-review_date', '-id').first()
    if latest is None:
        return False
    rule = latest.rule_version
    grace_due = latest.next_due_date + timedelta(days=rule.overdue_grace_days)
    return on_date > grace_due


@transaction.atomic
def approve_overdue_release(*, stock_out, approver, remark=''):
    """逾期物资放行前的额外审批，审批通过后才可放行出库。"""
    if not goods_review_overdue(stock_out.goods):
        raise ValueError('该物资不存在复核逾期，无需额外审批')

    approval = Approval.objects.create(
        stock_out=stock_out,
        approver=approver,
        approval_type='overdue_review',
        status='approved',
        remark=remark or '封存复核逾期，同意额外审批放行',
    )
    stock_out.extra_approval = approval
    stock_out.save(update_fields=['extra_approval'])

    logger.info(
        "逾期放行额外审批通过: 出库单 %s（%s）", stock_out.id, stock_out.goods.name
    )
    return approval


def assert_release_allowed(stock_out):
    """放行（完成出库）前的闸门：逾期物资必须先通过额外审批。"""
    if goods_review_overdue(stock_out.goods) and stock_out.extra_approval_id is None:
        raise PermissionError('该物资封存复核已逾期，放行前须完成额外审批')
