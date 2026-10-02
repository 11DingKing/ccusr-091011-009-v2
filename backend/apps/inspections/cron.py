"""
周期复核定时任务：可由管理命令或进程内调度器调用。
"""
import logging

from .services import create_overdue_warnings, generate_tasks

logger = logging.getLogger('apps')


def scan_reviews(within_days=7):
    """
    周期复核扫描 - 建议每日执行。
    批量生成未来 within_days 天内到期的复核待办（幂等），并为逾期物资生成预警。
    """
    logger.info("开始执行周期复核扫描...")
    stats = generate_tasks(within_days=within_days)
    warning_count = create_overdue_warnings()
    logger.info(
        "周期复核扫描完成: 新增待办 %s，新增逾期预警 %s",
        stats['created'], warning_count,
    )
    return {'tasks': stats, 'overdue_warnings': warning_count}
