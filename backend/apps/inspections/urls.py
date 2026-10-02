"""
周期复核 URL 配置
"""
from django.urls import path

from .views import (
    ReleaseApprovalDecideView,
    ReleaseApprovalListCreateView,
    ResultListView,
    RuleDetailView,
    RuleListCreateView,
    RuleNewVersionView,
    RulePublishView,
    StockOutBlockersView,
    StockOutReleaseView,
    TaskDetailView,
    TaskGenerateView,
    TaskListView,
    TaskCompleteView,
)

urlpatterns = [
    # 复核规则（版本化）
    path('review-rules/', RuleListCreateView.as_view(), name='review-rule-list'),
    path('review-rules/<int:pk>/', RuleDetailView.as_view(), name='review-rule-detail'),
    path('review-rules/<int:pk>/publish/', RulePublishView.as_view(), name='review-rule-publish'),
    path('review-rules/<int:pk>/new-version/', RuleNewVersionView.as_view(), name='review-rule-new-version'),

    # 复核待办
    path('review-tasks/', TaskListView.as_view(), name='review-task-list'),
    path('review-tasks/generate/', TaskGenerateView.as_view(), name='review-task-generate'),
    path('review-tasks/<int:pk>/', TaskDetailView.as_view(), name='review-task-detail'),
    path('review-tasks/<int:pk>/complete/', TaskCompleteView.as_view(), name='review-task-complete'),

    # 历史结论
    path('review-results/', ResultListView.as_view(), name='review-result-list'),

    # 逾期放行
    path('stock-out/<int:stock_out_id>/review-blockers/',
         StockOutBlockersView.as_view(), name='stock-out-review-blockers'),
    path('stock-out/<int:stock_out_id>/release/',
         StockOutReleaseView.as_view(), name='stock-out-release'),
    path('release-approvals/',
         ReleaseApprovalListCreateView.as_view(), name='release-approval-list'),
    path('release-approvals/<int:pk>/decide/',
         ReleaseApprovalDecideView.as_view(), name='release-approval-decide'),
]
