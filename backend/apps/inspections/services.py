"""
周期复核领域服务：待办生成、结论登记、逾期放行校验。
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.warehouse.models import StockIn, Warning

from .models import ReleaseApproval, ReviewResult, ReviewRule, ReviewTask

logger = logging.getLogger('apps')


class ReviewError(Exception):
    """复核业务错误"""


def inbound_date(goods):
    """物资的入库日期：取最早一条入库记录，缺省回退到建档日期。"""
    first = (
        StockIn.objects.filter(goods=goods)
        .order_by('stock_in_time')
        .values_list('stock_in_time', flat=True)
        .first()
    )
    moment = first or goods.created_at
    return timezone.localtime(moment).date()


def _first_due_date(rule, base_date):
    """首检期限 = 入库日期 + 首次宽限（未配置则用常规周期）。"""
    grace = rule.initial_grace_days or rule.interval_days
    return base_date + timedelta(days=grace)


def _next_due_date(rule, checked_date, abnormal):
    """根据上次结论决定下一周期：异常走加严周期，否则常规周期。"""
    if abnormal and rule.abnormal_interval_days:
        days = rule.abnormal_interval_days
    else:
        days = rule.interval_days
    return checked_date + timedelta(days=days)


@transaction.atomic
def generate_tasks(within_days=None):
    """
    批量生成复核待办。

    依据：物资类型（品类匹配生效规则）、入库日期（首检期限）、上次结论（加严周期）。
    幂等：每种物资至多保留一条待办待复核，(物资, 序号) 唯一约束兜底，
    重复执行不会产生重复待办。返回 {'created': n, 'skipped': n}。
    """
    today = timezone.localdate()
    horizon = today + timedelta(days=within_days) if within_days is not None else None

    created = 0
    skipped = 0

    from apps.warehouse.models import Goods
    goods_qs = Goods.objects.filter(is_active=True).select_related('variety__category')

    # 预取每种物资的待办与最近结论，避免 N+1
    pending_map = {}
    result_map = {}
    for task in (
        ReviewTask.objects.filter(status=ReviewTask.STATUS_PENDING)
        .select_related('result')
    ):
        pending_map.setdefault(task.goods_id, []).append(task)
    for result in ReviewResult.objects.select_related('task'):
        prev = result_map.get(result.goods_id)
        if prev is None or result.task.sequence > prev.task.sequence:
            result_map[result.goods_id] = result

    for goods in goods_qs:
        rule = ReviewRule.active_for_category(goods.variety.category_id)
        if rule is None:
            skipped += 1
            continue

        # 已有待复核待办：不重复生成
        if pending_map.get(goods.id):
            skipped += 1
            continue

        last_result = result_map.get(goods.id)
        if last_result is None:
            sequence = 1
            due_date = _first_due_date(rule, inbound_date(goods))
        else:
            sequence = last_result.task.sequence + 1
            due_date = last_result.next_due_date or _next_due_date(
                rule,
                timezone.localdate(last_result.checked_at),
                last_result.conclusion == ReviewResult.CONCLUSION_ABNORMAL,
            )

        if horizon is not None and due_date > horizon:
            skipped += 1
            continue

        _, was_created = ReviewTask.objects.get_or_create(
            goods=goods,
            sequence=sequence,
            defaults={
                'rule': rule,
                'rule_snapshot': rule.snapshot(),
                'due_date': due_date,
                'status': ReviewTask.STATUS_PENDING,
            },
        )
        if was_created:
            created += 1
            logger.info("生成复核待办: %s 第%s次, 期限 %s", goods.code, sequence, due_date)
        else:
            skipped += 1

    logger.info("复核待办批量生成完成: 新增 %s, 跳过 %s", created, skipped)
    return {'created': created, 'skipped': skipped}


@transaction.atomic
def complete_review(task, *, inspector, conclusion, abnormal_items=None, remark=''):
    """
    登记复核结果。结果固化规则快照、异常项与下次期限；已完成任务不可重复登记，
    从而保证规则改版或重复提交都不能改变历史结论。
    """
    if task.status == ReviewTask.STATUS_COMPLETED:
        raise ReviewError('该待办已完成复核，历史结论不可修改')

    abnormal_items = abnormal_items or []
    if conclusion == ReviewResult.CONCLUSION_ABNORMAL and not abnormal_items:
        raise ReviewError('复核结论为异常时，必须填写异常项')

    rule = task.rule
    checked_at = timezone.now()
    next_due = _next_due_date(
        rule, timezone.localdate(checked_at),
        conclusion == ReviewResult.CONCLUSION_ABNORMAL,
    )

    result = ReviewResult.objects.create(
        task=task,
        goods=task.goods,
        rule=rule,
        rule_snapshot=task.rule_snapshot or rule.snapshot(),
        inspector=inspector,
        conclusion=conclusion,
        abnormal_items=abnormal_items,
        remark=remark,
        next_due_date=next_due,
        checked_at=checked_at,
    )
    task.status = ReviewTask.STATUS_COMPLETED
    task.completed_at = checked_at
    task.save(update_fields=['status', 'completed_at'])

    logger.info(
        "复核完成: %s 第%s次 结论=%s 下次期限=%s",
        task.goods.code, task.sequence, conclusion, next_due,
    )
    return result


def overdue_tasks_for_goods(goods):
    """该物资当前逾期未完成的复核待办。"""
    today = timezone.localdate()
    return list(
        ReviewTask.objects.filter(
            goods=goods, status=ReviewTask.STATUS_PENDING, due_date__lt=today
        )
    )


def release_blockers(stock_out):
    """
    出库放行前的逾期复核阻塞项：逾期且没有“已通过”额外审批的待办。
    """
    blockers = []
    for task in overdue_tasks_for_goods(stock_out.goods):
        approved = ReleaseApproval.objects.filter(
            stock_out=stock_out, task=task, status=ReleaseApproval.STATUS_APPROVED
        ).exists()
        if not approved:
            blockers.append(task)
    return blockers


def can_release(stock_out):
    return not release_blockers(stock_out)


@transaction.atomic
def request_release_approval(stock_out, task):
    """为逾期物资的出库单创建额外审批（幂等）。"""
    if task.goods_id != stock_out.goods_id:
        raise ReviewError('审批待办与出库单物资不一致')
    if task.status != ReviewTask.STATUS_PENDING or not task.is_overdue:
        raise ReviewError('只能对逾期未完成的复核申请放行审批')
    approval, _ = ReleaseApproval.objects.get_or_create(
        stock_out=stock_out, task=task,
        defaults={'status': ReleaseApproval.STATUS_PENDING},
    )
    return approval


@transaction.atomic
def decide_release_approval(approval, *, approver, approved, remark=''):
    """审批逾期放行申请。"""
    if approval.status != ReleaseApproval.STATUS_PENDING:
        raise ReviewError('该审批已处理，不能重复审批')
    approval.approver = approver
    approval.status = (
        ReleaseApproval.STATUS_APPROVED if approved else ReleaseApproval.STATUS_REJECTED
    )
    approval.remark = remark
    approval.save(update_fields=['approver', 'status', 'remark', 'updated_at'])
    return approval


@transaction.atomic
def release_stock_out(stock_out):
    """
    放行（完成出库）。存在逾期未复核且未额外审批通过的物资时拒绝放行。
    """
    blockers = release_blockers(stock_out)
    if blockers:
        raise ReviewError(
            '物资存在逾期未完成的周期复核，须先完成额外审批后方可放行：'
            + '、'.join(str(t.due_date) for t in blockers)
        )
    stock_out.status = 'completed'
    stock_out.stock_out_time = timezone.now()
    stock_out.save(update_fields=['status', 'stock_out_time'])
    return stock_out


def create_overdue_warnings():
    """
    为逾期未复核的物资生成预警（弥补现有预警只看库存阈值的缺口）。
    同一物资存在未读逾期预警时不重复生成。
    """
    today = timezone.localdate()
    created = 0
    overdue = (
        ReviewTask.objects.filter(
            status=ReviewTask.STATUS_PENDING, due_date__lt=today
        )
        .select_related('goods')
        .order_by('due_date')
    )
    seen_goods = set()
    for task in overdue:
        if task.goods_id in seen_goods:
            continue
        seen_goods.add(task.goods_id)
        exists = Warning.objects.filter(
            goods=task.goods, type='review_overdue', is_read=False
        ).exists()
        if not exists:
            Warning.objects.create(
                goods=task.goods,
                type='review_overdue',
                message=(
                    f'物资 {task.goods.name}（{task.goods.code}）周期复核已逾期，'
                    f'应于 {task.due_date} 完成第{task.sequence}次复核，放行前需额外审批'
                ),
            )
            created += 1
    logger.info("逾期复核预警生成完成: 新增 %s 条", created)
    return created
