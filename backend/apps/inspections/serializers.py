"""
周期复核序列化器
"""
from rest_framework import serializers

from .models import ReleaseApproval, ReviewResult, ReviewRule, ReviewTask


class RuleSerializer(serializers.ModelSerializer):
    """规则版本序列化器"""
    category_name = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    is_editable = serializers.BooleanField(read_only=True)

    class Meta:
        model = ReviewRule
        fields = [
            'id', 'name', 'version', 'category', 'category_name',
            'interval_days', 'initial_grace_days', 'abnormal_interval_days',
            'check_items', 'remark', 'status', 'status_display', 'is_editable',
            'published_at', 'created_by', 'created_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'version', 'status', 'published_at', 'created_at', 'updated_at']


class RuleWriteSerializer(serializers.Serializer):
    """草稿规则创建/修改"""
    name = serializers.CharField(max_length=100)
    category = serializers.IntegerField(required=False, allow_null=True)
    interval_days = serializers.IntegerField(min_value=1)
    initial_grace_days = serializers.IntegerField(min_value=0, required=False)
    abnormal_interval_days = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    check_items = serializers.ListField(
        child=serializers.CharField(max_length=100), required=False, allow_empty=True
    )
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_category(self, value):
        if value in (None, ''):
            return None
        from apps.warehouse.models import Category
        if not Category.objects.filter(pk=value).exists():
            raise serializers.ValidationError('品类不存在')
        return value

    def validate(self, data):
        abnormal = data.get('abnormal_interval_days')
        if abnormal and abnormal > data['interval_days']:
            raise serializers.ValidationError(
                {'abnormal_interval_days': '异常后加严周期不应长于常规周期'}
            )
        return data


class ResultSerializer(serializers.ModelSerializer):
    conclusion_display = serializers.CharField(source='get_conclusion_display', read_only=True)
    inspector_name = serializers.CharField(source='inspector.username', read_only=True)
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    rule_name = serializers.CharField(source='rule.name', read_only=True)
    rule_version = serializers.IntegerField(source='rule.version', read_only=True)

    class Meta:
        model = ReviewResult
        fields = [
            'id', 'task', 'goods', 'goods_name', 'goods_code',
            'rule', 'rule_name', 'rule_version', 'rule_snapshot',
            'inspector', 'inspector_name',
            'conclusion', 'conclusion_display', 'abnormal_items',
            'remark', 'next_due_date', 'checked_at',
        ]


class TaskSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    category_name = serializers.CharField(source='goods.variety.category.name', read_only=True)
    rule_name = serializers.CharField(source='rule.name', read_only=True)
    rule_version = serializers.IntegerField(source='rule.version', read_only=True)
    result = ResultSerializer(read_only=True)

    class Meta:
        model = ReviewTask
        fields = [
            'id', 'goods', 'goods_name', 'goods_code', 'category_name',
            'sequence', 'rule', 'rule_name', 'rule_version', 'rule_snapshot',
            'due_date', 'status', 'status_display', 'is_overdue', 'result',
            'created_at', 'completed_at',
        ]


class CompleteReviewSerializer(serializers.Serializer):
    """登记复核结论"""
    conclusion = serializers.ChoiceField(choices=[
        ReviewResult.CONCLUSION_NORMAL, ReviewResult.CONCLUSION_ABNORMAL
    ])
    abnormal_items = serializers.ListField(
        child=serializers.JSONField(), required=False, allow_empty=True
    )
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_abnormal_items(self, value):
        for item in value or []:
            if not isinstance(item, dict) or not item.get('item'):
                raise serializers.ValidationError('每个异常项须包含 item 字段')
        return value


class ReleaseApprovalSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    approver_name = serializers.CharField(source='approver.username', read_only=True)
    goods_name = serializers.CharField(source='task.goods.name', read_only=True)
    due_date = serializers.DateField(source='task.due_date', read_only=True)

    class Meta:
        model = ReleaseApproval
        fields = [
            'id', 'stock_out', 'task', 'goods_name', 'due_date',
            'approver', 'approver_name', 'status', 'status_display',
            'remark', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'stock_out', 'task', 'approver', 'status', 'created_at', 'updated_at']


class ReleaseApprovalDecideSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    remark = serializers.CharField(required=False, allow_blank=True, default='')
