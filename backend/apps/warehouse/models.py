"""
库房管理模型
"""
from datetime import timedelta

from django.db import models
from django.db.models import JSONField, Q
from django.utils import timezone
from apps.authentication.models import User


class Unit(models.Model):
    """单位模型"""
    name = models.CharField('单位名称', max_length=5, unique=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_units', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_unit'
        verbose_name = '单位'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品类"""
        return self.categories.exists()


class Category(models.Model):
    """品类模型"""
    name = models.CharField('品类名称', max_length=10, unique=True)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT,
        related_name='categories', verbose_name='单位'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_categories', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_category'
        verbose_name = '品类'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品种"""
        return self.varieties.exists()


class Variety(models.Model):
    """品种模型"""
    name = models.CharField('品种名称', max_length=20)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='varieties', verbose_name='所属品类'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_varieties', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_variety'
        verbose_name = '品种'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['category', 'name']
    
    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
    @property
    def is_in_stock(self):
        """是否已入库"""
        return self.goods.exists()
    
    @property
    def unit_name(self):
        """获取单位名称"""
        return self.category.unit.name if self.category and self.category.unit else ''


class Goods(models.Model):
    """货物模型"""
    variety = models.ForeignKey(
        Variety, on_delete=models.CASCADE,
        related_name='goods', verbose_name='所属品种'
    )
    name = models.CharField('货物名称', max_length=200)
    code = models.CharField('货物编码', max_length=50, unique=True)
    specification = models.CharField('规格型号', max_length=200, blank=True)
    quantity = models.DecimalField('库存数量', max_digits=12, decimal_places=2, default=0)
    warning_threshold = models.DecimalField('预警阈值', max_digits=12, decimal_places=2, default=10)
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_goods'
        verbose_name = '货物'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_warning(self):
        """是否预警"""
        return self.quantity <= self.warning_threshold


class StockIn(models.Model):
    """入库记录模型"""
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_ins', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_in_operations', verbose_name='操作人'
    )
    quantity = models.DecimalField('入库数量', max_digits=12, decimal_places=2)
    batch_no = models.CharField('批次号', max_length=50, blank=True)
    supplier = models.CharField('供应商', max_length=200, blank=True)
    stock_in_time = models.DateTimeField('入库时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)
    
    class Meta:
        db_table = 'wh_stock_in'
        verbose_name = '入库记录'
        verbose_name_plural = verbose_name
        ordering = ['-stock_in_time']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class StockOut(models.Model):
    """出库记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
        ('completed', '已完成'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_outs', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_out_operations', verbose_name='操作人'
    )
    receiver = models.CharField('领用人', max_length=100)
    receiver_dept = models.CharField('领用部门', max_length=100, blank=True)
    quantity = models.DecimalField('出库数量', max_digits=12, decimal_places=2)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    stock_out_time = models.DateTimeField('出库时间', null=True, blank=True)
    extra_approval = models.ForeignKey(
        'Approval', on_delete=models.PROTECT, null=True, blank=True,
        related_name='released_stock_outs', verbose_name='逾期复核额外审批'
    )
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_stock_out'
        verbose_name = '出库记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"

    @property
    def requires_extra_approval(self):
        """是否因复核逾期而需要额外审批（按出库申请日期判定）"""
        from .review import goods_review_overdue
        return goods_review_overdue(
            self.goods, on_date=timezone.localdate(self.created_at)
        )


class Warning(models.Model):
    """预警记录模型"""
    TYPE_CHOICES = [
        ('low_stock', '库存不足'),
        ('expiring', '即将过期'),
        ('expired', '已过期'),
        ('review_overdue', '复核逾期'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='warnings', verbose_name='货物'
    )
    type = models.CharField('预警类型', max_length=20, choices=TYPE_CHOICES)
    message = models.TextField('预警信息')
    is_read = models.BooleanField('是否已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_warning'
        verbose_name = '预警记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.get_type_display()}"


class Approval(models.Model):
    """审批记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
    ]
    APPROVAL_TYPE_CHOICES = [
        ('stock_out', '出库审批'),
        ('overdue_review', '复核逾期额外审批'),
    ]

    stock_out = models.ForeignKey(
        StockOut, on_delete=models.CASCADE,
        related_name='approvals', verbose_name='出库记录'
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='approvals', verbose_name='审批人'
    )
    approval_type = models.CharField(
        '审批类型', max_length=30, choices=APPROVAL_TYPE_CHOICES, default='stock_out'
    )
    status = models.CharField('审批状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    remark = models.TextField('审批意见', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_approval'
        verbose_name = '审批记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.stock_out} - {self.get_status_display()}"


class ReviewRuleVersion(models.Model):
    """复核规则版本

    规则按品类（物资类型）配置，category 为空表示通用兜底规则。
    每次改版产生一条新记录、版本号递增；已发布版本内容冻结，
    新版本发布后同品类旧版本自动作废，历史结论引用的版本不受影响。
    """
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
        ('deprecated', '已作废'),
    ]

    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, null=True, blank=True,
        related_name='review_rules', verbose_name='适用品类'
    )
    version = models.PositiveIntegerField('版本号')
    name = models.CharField('规则名称', max_length=100)
    interval_days = models.PositiveIntegerField('复核周期（天）')
    check_items = JSONField('检查项目', default=list)
    overdue_grace_days = models.PositiveIntegerField('逾期宽限天数', default=0)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_review_rules', verbose_name='创建人'
    )
    published_at = models.DateTimeField('发布时间', null=True, blank=True)
    deprecated_at = models.DateTimeField('作废时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    # 规则发布后除“状态/作废时间”外一律冻结
    _frozen_fields = ('category_id', 'version', 'name', 'interval_days',
                      'check_items', 'overdue_grace_days')

    class Meta:
        db_table = 'wh_review_rule_version'
        verbose_name = '复核规则版本'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['category', 'version'],
                condition=Q(category__isnull=False),
                name='uniq_review_rule_category_version'
            ),
            models.UniqueConstraint(
                fields=['version'],
                condition=Q(category__isnull=True),
                name='uniq_review_rule_global_version'
            ),
        ]

    def __str__(self):
        scope = self.category.name if self.category else '通用'
        return f"{scope}复核规则 v{self.version}"

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if old.status != 'draft':
                for field in self._frozen_fields:
                    if getattr(old, field) != getattr(self, field):
                        raise ValueError('已发布的复核规则不可修改，只能发布新版本')
                if old.status == 'deprecated' and self.status != 'deprecated':
                    raise ValueError('已作废的复核规则不可恢复')
        super().save(*args, **kwargs)

    @property
    def is_published(self):
        return self.status == 'published'

    def snapshot(self):
        """生成写入复核结果的规则快照"""
        return {
            'rule_id': self.id,
            'name': self.name,
            'version': self.version,
            'category_id': self.category_id,
            'category_name': self.category.name if self.category else None,
            'interval_days': self.interval_days,
            'check_items': list(self.check_items or []),
            'overdue_grace_days': self.overdue_grace_days,
        }

    @classmethod
    def active_for_category(cls, category):
        """获取某品类当前生效规则：优先品类专用规则，其次通用规则"""
        rule = cls.objects.filter(status='published', category=category).order_by('-version').first()
        if rule is None:
            rule = cls.objects.filter(status='published', category__isnull=True).order_by('-version').first()
        return rule


class ReviewTask(models.Model):
    """复核待办

    每个物资同一时间只允许存在一条待复核待办（部分唯一索引），
    批量生成重复执行不会产生重复待办。
    """
    STATUS_CHOICES = [
        ('pending', '待复核'),
        ('completed', '已完成'),
        ('cancelled', '已取消'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='review_tasks', verbose_name='物资'
    )
    rule_version = models.ForeignKey(
        ReviewRuleVersion, on_delete=models.PROTECT,
        related_name='review_tasks', verbose_name='依据规则版本'
    )
    baseline_date = models.DateField('基准日期（入库日期或上次结论日期）')
    due_date = models.DateField('复核期限')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_review_task'
        verbose_name = '复核待办'
        verbose_name_plural = verbose_name
        ordering = ['due_date', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['goods'],
                condition=Q(status='pending'),
                name='uniq_pending_review_task_per_goods'
            ),
        ]

    def __str__(self):
        return f"{self.goods.name} - 复核待办（{self.due_date} 前）"

    @property
    def is_overdue(self):
        grace = timedelta(days=self.rule_version.overdue_grace_days)
        return timezone.localdate() > self.due_date + grace


class ReviewRecord(models.Model):
    """复核结果

    保存采用的规则（外键 + 内容快照）、异常项和下一次期限。
    规则改版只影响之后的新结果，历史结论保持不变。
    """
    CONCLUSION_CHOICES = [
        ('qualified', '合格'),
        ('abnormal', '异常'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='review_records', verbose_name='物资'
    )
    task = models.ForeignKey(
        ReviewTask, on_delete=models.PROTECT,
        related_name='records', verbose_name='来源待办'
    )
    rule_version = models.ForeignKey(
        ReviewRuleVersion, on_delete=models.PROTECT,
        related_name='review_records', verbose_name='采用的规则版本'
    )
    rule_snapshot = JSONField('采用规则快照')
    conclusion = models.CharField('复核结论', max_length=20, choices=CONCLUSION_CHOICES)
    abnormal_items = JSONField('异常项', default=list)
    next_due_date = models.DateField('下一次复核期限')
    reviewer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='review_records', verbose_name='复核人'
    )
    review_date = models.DateField('复核日期')
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_review_record'
        verbose_name = '复核结果'
        verbose_name_plural = verbose_name
        ordering = ['-review_date', '-id']

    def __str__(self):
        return f"{self.goods.name} - {self.get_conclusion_display()}（{self.review_date}）"
