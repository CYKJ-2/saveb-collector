"""队列模块入口：暴露 celery_app 实例供 CLI 使用。"""

from app.queue.celery_app import celery_app

__all__ = ["celery_app"]
