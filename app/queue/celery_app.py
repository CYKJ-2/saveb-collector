# Celery 队列和 Beat 注册；分钟级检查到期时间不等于每分钟都采集订单。
from celery import Celery
from celery.schedules import crontab

from app.config.settings import get_settings

settings = get_settings()
celery_app = Celery(
    "saveb-collector",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.queue.tasks", "app.collectors.exchange_rates"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    # worker 完成后才确认消息；进程中断时允许重投，业务幂等不能依赖消息只送一次。
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_timeout=5,
    task_soft_time_limit=840,
    task_time_limit=900,
    result_expires=86400,
    task_default_queue="collect",
    broker_transport_options={"visibility_timeout": 1200},
    # 即时、历史、物流和维护任务分别消费，防止耗时查询占满即时采集 worker。
    task_routes={
        "collector.run_logistics": {"queue": "logistics"},
        "collector.logistics_schedule": {"queue": "maintenance"},
        "collector.run_history": {"queue": "history"},
        "collector.exchange_rates": {"queue": "maintenance"},
        "collector.dispatch": {"queue": "maintenance"},
        "collector.schedule": {"queue": "maintenance"},
        "collector.pending_schedule": {"queue": "maintenance"},
        "collector.reconcile": {"queue": "maintenance"},
    },
    # 订单默认 30 分钟由数据库调度表控制；Pending 和物流由各自设置控制。
    # 此处的 60 秒仅检查是否到期，outbox 则每 15 秒尝试投递。
    beat_schedule={
        "check-logistics-schedule": {"task": "collector.logistics_schedule", "schedule": 60.0},
        "discover-historical-pending": {"task": "collector.pending_schedule", "schedule": 60.0},
        "daily-reconciliation": {"task": "collector.reconcile", "schedule": crontab(hour=3, minute=10)},
        "check-collection-schedule": {
            "task": "collector.schedule",
            "schedule": 60.0,
        },
        "dispatch-durable-outbox": {"task": "collector.dispatch", "schedule": 15.0},
        "exchange-rates-hourly": {
            "task": "collector.exchange_rates",
            "schedule": crontab(minute=5),
        },
    },
)
