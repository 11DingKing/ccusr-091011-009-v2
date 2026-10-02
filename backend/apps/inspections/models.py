"""
封存物资周期复核模型

设计要点：
- ReviewRule 是可版本化的规则：同一名称可有多版，已发布的版本不可修改，
  改版即发布新版本；已完成的复核结论快照保存当版规则，规则改版不影响历史结论。
- ReviewTask 是待办，按 (物资, 复核序号) 幂等，批量生成重复执行不会产生重复待办。
- ReviewResult 保存检查结果：采用的规则快照、异常项与下一次期限。
- 逾期物资放行（出库完成）前必须存在针对当前逾期任务的额外审批。
"""
from django.conf import settings
from django.db import models, transaction

from apps.warehouse.models import Goods


class ReviewRule(models.Model):
    """周期复核规则（可版本化）"""

    STATUS_DRAFT = 'draft'
    STATUS_PUBLISHED = 'published'
    STATUS_DEPRECATED = 'deprecated'
    STATUS_CHOICES = [
        (STATUS_DRAFT, '草稿'),
        (STATUS_PUBLISHED, '生效中'),
        (STATUS_DEPRECATED, '已停用'),
    ]

    name = models.CharField('规则名称', max_length=100)
    version = models.PositiveIntegerField('版本号', default=1)
    # 适用物资类型：品类(wh_category)主键，None 表示兜底通用规则
    category = models.ForeignKey(
        'warehouse.Category', on_delete=models.PROTECT,
        null=True, blank=True, related_name='review_rules', verbose_name='适用品类'
    )
    # 复核周期（天）
    interval_days = models.PositiveIntegerField('复核周期(天)')
    # 入库后宽限天数：首检期限 = 入库日期 + initial_grace_days
    initial_grace_days = models.PositiveIntegerField('首次复核宽限(天)', default=0)
    # 上次复核存在异常时的加严周期（天），为空则沿用常规周期
    abnormal_interval_days = models.PositiveIntegerField('异常后复核周期(天)', null=True, blank=True)
    # 检查项目清单，如 ["包装完好性", "温湿度条件", "封条完整性"]
    check_items = models.JSONField('检查项目', default=list)
    remark = models.TextField('说明', blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    published_at = models.DateTimeField('发布时间', null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='created_review_rules', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'insp_review_rule'
        verbose_name = '复核规则'
        verbose_name_plural = verbose_name
        # 同一名称下版本号唯一
        unique_together = [('name', 'version')]
        ordering = ['category_id', '-version', '-id']

    def __str__(self):
        return f"{self.name} v{self.version}（{self.get_status_display()}）"

    @property
    def is_editable(self):
        """仅草稿可改；已发布版本冻结，改版须新建版本。"""
        return self.status == self.STATUS_DRAFT

    @property
    def category_name(self):
        return self.category.name if self.category_id else '通用'

    def publish(self):
        """发布版本：同一名称的其他已发布版本自动停用。"""
        from django.utils import timezone
        with transaction.atomic():
            ReviewRule.objects.filter(
                name=self.name, status=self.STATUS_PUBLISHED
            ).exclude(pk=self.pk).update(status=self.STATUS_DEPRECATED)
            self.status = self.STATUS_PUBLISHED
            self.published_at = timezone.now()
            self.save(update_fields=['status', 'published_at', 'updated_at'])

    def new_version(self, **overrides):
        """基于当前版本创建一份新草稿，版本号取同名称最大值 + 1。"""
        latest = ReviewRule.objects.filter(name=self.name).order_by('-version').first()
        next_version = (latest.version if latest else self.version) + 1
        return ReviewRule.objects.create(
            name=self.name,
            version=next_version,
            category=self.category,
            interval_days=overrides.get('interval_days', self.interval_days),
            initial_grace_days=overrides.get('initial_grace_days', self.initial_grace_days),
            abnormal_interval_days=overrides.get(
                'abnormal_interval_days', self.abnormal_interval_days
            ),
            check_items=overrides.get('check_items', list(self.check_items)),
            remark=overrides.get('remark', self.remark),
            created_by=overrides.get('created_by'),
            status=self.STATUS_DRAFT,
        )

    @staticmethod
    def active_for_category(category_id):
        """返回某品类当前生效规则：优先品类专用，其次通用兜底。"""
        return (
            ReviewRule.objects.filter(status=ReviewRule.STATUS_PUBLISHED, category_id=category_id)
            .order_by('-published_at')
            .first()
            or ReviewRule.objects.filter(status=ReviewRule.STATUS_PUBLISHED, category__isnull=True)
            .order_by('-published_at')
            .first()
        )

    def snapshot(self):
        """规则内容快照，随复核结论一并固化。"""
        return {
            'rule_id': self.id,
            'name': self.name,
            'version': self.version,
            'category_id': self.category_id,
            'interval_days': self.interval_days,
            'initial_grace_days': self.initial_grace_days,
            'abnormal_interval_days': self.abnormal_interval_days,
            'check_items': list(self.check_items or []),
        }


class ReviewTask(models.Model):
    """复核待办"""

    STATUS_PENDING = 'pending'
    STATUS_COMPLETED = 'completed'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待复核'),
        (STATUS_COMPLETED, '已完成'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='review_tasks', verbose_name='物资'
    )
    sequence = models.PositiveIntegerField('复核序号', default=1)
    rule = models.ForeignKey(
        ReviewRule, on_delete=models.PROTECT,
        related_name='tasks', verbose_name='适用规则版本'
    )
    # 生成时即固化所采用规则版本，后续规则改版不影响本待办
    rule_snapshot = models.JSONField('规则快照', default=dict)
    due_date = models.DateField('应复核期限')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    completed_at = models.DateTimeField('完成时间', null=True, blank=True)

    class Meta:
        db_table = 'insp_review_task'
        verbose_name = '复核待办'
        verbose_name_plural = verbose_name
        # 幂等键：同一物资的同一序号只能有一条待办
        unique_together = [('goods', 'sequence')]
        ordering = ['due_date', '-id']

    def __str__(self):
        return f"{self.goods.name} 第{self.sequence}次复核（{self.due_date}）"

    @property
    def is_overdue(self):
        from django.utils import timezone
        return self.status == self.STATUS_PENDING and self.due_date < timezone.localdate()

    @property
    def latest_result(self):
        return getattr(self, 'result', None)


class ReviewResult(models.Model):
    """复核检查结果（历史结论不可变）"""

    CONCLUSION_NORMAL = 'normal'
    CONCLUSION_ABNORMAL = 'abnormal'
    CONCLUSION_CHOICES = [
        (CONCLUSION_NORMAL, '正常'),
        (CONCLUSION_ABNORMAL, '异常'),
    ]

    task = models.OneToOneField(
        ReviewTask, on_delete=models.PROTECT,
        related_name='result', verbose_name='复核待办'
    )
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='review_results', verbose_name='物资'
    )
    # 采用的规则及其完整内容快照；规则改版后历史结论仍保持原样
    rule = models.ForeignKey(
        ReviewRule, on_delete=models.PROTECT,
        related_name='results', verbose_name='采用规则版本'
    )
    rule_snapshot = models.JSONField('规则快照', default=dict)
    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='inspection_results', verbose_name='检查人'
    )
    conclusion = models.CharField(
        '复核结论', max_length=20, choices=CONCLUSION_CHOICES, default=CONCLUSION_NORMAL
    )
    # 异常项，如 [{"item": "包装完好性", "detail": "外箱破损"}]；正常为空列表
    abnormal_items = models.JSONField('异常项', default=list)
    remark = models.TextField('备注', blank=True)
    next_due_date = models.DateField('下次复核期限', null=True, blank=True)
    checked_at = models.DateTimeField('检查时间', auto_now_add=True)

    class Meta:
        db_table = 'insp_review_result'
        verbose_name = '复核结果'
        verbose_name_plural = verbose_name
        ordering = ['-checked_at']

    def __str__(self):
        return f"{self.goods.name} - {self.get_conclusion_display()}（{self.checked_at:%Y-%m-%d}）"


class ReleaseApproval(models.Model):
    """逾期物资放行额外审批"""

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待审批'),
        (STATUS_APPROVED, '已通过'),
        (STATUS_REJECTED, '已拒绝'),
    ]

    stock_out = models.ForeignKey(
        'warehouse.StockOut', on_delete=models.CASCADE,
        related_name='release_approvals', verbose_name='出库记录'
    )
    task = models.ForeignKey(
        ReviewTask, on_delete=models.PROTECT,
        related_name='release_approvals', verbose_name='逾期复核待办'
    )
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='release_approvals', verbose_name='审批人'
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    remark = models.TextField('审批意见', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'insp_release_approval'
        verbose_name = '逾期放行审批'
        verbose_name_plural = verbose_name
        # 同一出库单对同一逾期任务只需一次审批
        unique_together = [('stock_out', 'task')]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.stock_out_id} 逾期放行 - {self.get_status_display()}"
