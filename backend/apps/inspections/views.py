"""
周期复核视图：规则版本管理、待办生成与登记、逾期放行审批。
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.response import error_response, success_response
from apps.warehouse.models import StockOut

from .models import ReleaseApproval, ReviewResult, ReviewRule, ReviewTask
from .serializers import (
    CompleteReviewSerializer,
    ReleaseApprovalDecideSerializer,
    ReleaseApprovalSerializer,
    ResultSerializer,
    RuleSerializer,
    RuleWriteSerializer,
    TaskSerializer,
)
from .services import (
    ReviewError,
    complete_review,
    create_overdue_warnings,
    decide_release_approval,
    generate_tasks,
    release_blockers,
    release_stock_out,
    request_release_approval,
)

logger = logging.getLogger('apps')


def _paginate(request, queryset):
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = max(int(request.query_params.get('page_size', 10)), 1)
    total = queryset.count()
    start = (page - 1) * page_size
    return queryset[start:start + page_size], total, page, page_size


# ==================== 规则版本 ====================

class RuleListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = ReviewRule.objects.select_related('category', 'created_by')
        name = request.query_params.get('name')
        status = request.query_params.get('status')
        category_id = request.query_params.get('category')
        if name:
            queryset = queryset.filter(name=name)
        if status:
            queryset = queryset.filter(status=status)
        if category_id:
            queryset = queryset.filter(category_id=category_id)

        page = request.query_params.get('page')
        if page:
            rules, total, p, ps = _paginate(request, queryset)
            return success_response(data={
                'list': RuleSerializer(rules, many=True).data,
                'total': total, 'page': p, 'page_size': ps,
            })
        return success_response(data=RuleSerializer(queryset, many=True).data)

    def post(self, request):
        serializer = RuleWriteSerializer(data=request.data)
        if not serializer.is_valid():
            first = next(iter(serializers.errors.values()))[0]
            return error_response(message=str(first))
        data = serializer.validated_data
        rule = ReviewRule.objects.create(
            name=data['name'],
            version=1,
            category_id=data.get('category'),
            interval_days=data['interval_days'],
            initial_grace_days=data.get('initial_grace_days', 0),
            abnormal_interval_days=data.get('abnormal_interval_days'),
            check_items=data.get('check_items', []),
            remark=data.get('remark', ''),
            created_by=request.user,
            status=ReviewRule.STATUS_DRAFT,
        )
        logger.info("用户 %s 创建复核规则草稿 %s v%s", request.user.username, rule.name, rule.version)
        return success_response(data=RuleSerializer(rule).data, message='草稿已创建')


class RuleDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        rule = ReviewRule.objects.filter(pk=pk).first()
        if not rule:
            return error_response(message='规则不存在', code=404)
        return success_response(data=RuleSerializer(rule).data)

    def put(self, request, pk):
        rule = ReviewRule.objects.filter(pk=pk).first()
        if not rule:
            return error_response(message='规则不存在', code=404)
        if not rule.is_editable:
            return error_response(message='已发布版本不可修改，请发布新版本')
        serializer = RuleWriteSerializer(data=request.data)
        if not serializer.is_valid():
            first = next(iter(serializers.errors.values()))[0]
            return error_response(message=str(first))
        data = serializer.validated_data
        rule.name = data['name']
        rule.category_id = data.get('category')
        rule.interval_days = data['interval_days']
        rule.initial_grace_days = data.get('initial_grace_days', 0)
        rule.abnormal_interval_days = data.get('abnormal_interval_days')
        rule.check_items = data.get('check_items', [])
        rule.remark = data.get('remark', '')
        rule.save()
        return success_response(data=RuleSerializer(rule).data, message='草稿已更新')


class RulePublishView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        rule = ReviewRule.objects.filter(pk=pk).first()
        if not rule:
            return error_response(message='规则不存在', code=404)
        if rule.status != ReviewRule.STATUS_DRAFT:
            return error_response(message='仅草稿版本可以发布')
        rule.publish()
        logger.info("用户 %s 发布复核规则 %s v%s", request.user.username, rule.name, rule.version)
        return success_response(data=RuleSerializer(rule).data, message='规则已发布生效')


class RuleNewVersionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        """基于任一历史版本派生新草稿（改版不触碰已发布版本与历史结论）。"""
        source = ReviewRule.objects.filter(pk=pk).first()
        if not source:
            return error_response(message='规则不存在', code=404)
        # 沿用源版本内容，允许请求体覆盖；名称默认沿用以串起版本族
        payload = {
            'name': request.data.get('name', source.name),
            'category': request.data.get('category', source.category_id),
            'interval_days': request.data.get('interval_days', source.interval_days),
            'initial_grace_days': request.data.get(
                'initial_grace_days', source.initial_grace_days
            ),
            'abnormal_interval_days': request.data.get(
                'abnormal_interval_days', source.abnormal_interval_days
            ),
            'check_items': request.data.get('check_items', list(source.check_items or [])),
            'remark': request.data.get('remark', source.remark),
        }
        serializer = RuleWriteSerializer(data=payload)
        if not serializer.is_valid():
            first = next(iter(serializers.errors.values()))[0]
            return error_response(message=str(first))
        data = serializer.validated_data
        latest = ReviewRule.objects.filter(name=data['name']).order_by('-version').first()
        draft = ReviewRule.objects.create(
            name=data['name'],
            version=(latest.version + 1) if latest else 1,
            category_id=data.get('category'),
            interval_days=data['interval_days'],
            initial_grace_days=data.get('initial_grace_days', 0),
            abnormal_interval_days=data.get('abnormal_interval_days'),
            check_items=data.get('check_items', []),
            remark=data.get('remark', ''),
            created_by=request.user,
            status=ReviewRule.STATUS_DRAFT,
        )
        return success_response(data=RuleSerializer(draft).data, message='新版本草稿已创建')


# ==================== 复核待办 ====================

class TaskListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = ReviewTask.objects.select_related(
            'goods', 'goods__variety__category', 'rule', 'result'
        )
        status = request.query_params.get('status')
        goods_id = request.query_params.get('goods')
        overdue = request.query_params.get('overdue')
        if status:
            queryset = queryset.filter(status=status)
        if goods_id:
            queryset = queryset.filter(goods_id=goods_id)
        if overdue == 'true':
            from django.utils import timezone
            queryset = queryset.filter(
                status=ReviewTask.STATUS_PENDING, due_date__lt=timezone.localdate()
            )
        tasks, total, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': TaskSerializer(tasks, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })


class TaskGenerateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        within_days = request.data.get('within_days')
        try:
            within_days = int(within_days) if within_days is not None else None
        except (TypeError, ValueError):
            return error_response(message='within_days 必须为整数')
        stats = generate_tasks(within_days=within_days)
        create_overdue_warnings()
        return success_response(data=stats, message=f"新增 {stats['created']} 条待办")


class TaskDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        task = ReviewTask.objects.select_related(
            'goods', 'rule', 'result', 'result__inspector'
        ).filter(pk=pk).first()
        if not task:
            return error_response(message='待办不存在', code=404)
        return success_response(data=TaskSerializer(task).data)


class TaskCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        task = ReviewTask.objects.select_related('rule', 'goods').filter(pk=pk).first()
        if not task:
            return error_response(message='待办不存在', code=404)
        serializer = CompleteReviewSerializer(data=request.data)
        if not serializer.is_valid():
            first = next(iter(serializer.errors.values()))[0]
            return error_response(message=str(first))
        try:
            result = complete_review(
                task,
                inspector=request.user,
                conclusion=serializer.validated_data['conclusion'],
                abnormal_items=serializer.validated_data.get('abnormal_items', []),
                remark=serializer.validated_data.get('remark', ''),
            )
        except ReviewError as exc:
            return error_response(message=str(exc))
        return success_response(data=ResultSerializer(result).data, message='复核结果已登记')


# ==================== 历史结论 ====================

class ResultListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = ReviewResult.objects.select_related(
            'goods', 'rule', 'inspector', 'task'
        ).order_by('-checked_at')
        goods_id = request.query_params.get('goods')
        if goods_id:
            queryset = queryset.filter(goods_id=goods_id)
        results, total, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': ResultSerializer(results, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })


# ==================== 逾期放行 ====================

class StockOutBlockersView(APIView):
    """查询出库单放行前的逾期复核阻塞项。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, stock_out_id):
        stock_out = StockOut.objects.filter(pk=stock_out_id).first()
        if not stock_out:
            return error_response(message='出库记录不存在', code=404)
        blockers = release_blockers(stock_out)
        return success_response(data={
            'blocked': bool(blockers),
            'tasks': TaskSerializer(blockers, many=True).data,
        })


class StockOutReleaseView(APIView):
    """放行出库；存在逾期未复核且未审批的物资时拒绝。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, stock_out_id):
        stock_out = StockOut.objects.filter(pk=stock_out_id).first()
        if not stock_out:
            return error_response(message='出库记录不存在', code=404)
        try:
            release_stock_out(stock_out)
        except ReviewError as exc:
            logger.warning("出库 %s 放行被拦截: %s", stock_out_id, exc)
            return error_response(message=str(exc), code=403)
        return success_response(message='出库已放行')


class ReleaseApprovalListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = ReleaseApproval.objects.select_related(
            'stock_out', 'task', 'approver'
        )
        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        approvals, total, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': ReleaseApprovalSerializer(approvals, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        stock_out_id = request.data.get('stock_out')
        task_id = request.data.get('task')
        stock_out = StockOut.objects.filter(pk=stock_out_id).first()
        task = ReviewTask.objects.filter(pk=task_id).first()
        if not stock_out:
            return error_response(message='出库记录不存在')
        if not task:
            return error_response(message='复核待办不存在')
        try:
            approval = request_release_approval(stock_out, task)
        except ReviewError as exc:
            return error_response(message=str(exc))
        return success_response(data=ReleaseApprovalSerializer(approval).data, message='放行审批已提交')


class ReleaseApprovalDecideView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        approval = ReleaseApproval.objects.filter(pk=pk).first()
        if not approval:
            return error_response(message='审批不存在', code=404)
        serializer = ReleaseApprovalDecideSerializer(data=request.data)
        if not serializer.is_valid():
            first = next(iter(serializer.errors.values()))[0]
            return error_response(message=str(first))
        try:
            decide_release_approval(
                approval,
                approver=request.user,
                approved=serializer.validated_data['approved'],
                remark=serializer.validated_data.get('remark', ''),
            )
        except ReviewError as exc:
            return error_response(message=str(exc))
        return success_response(data=ReleaseApprovalSerializer(approval).data, message='审批已处理')
