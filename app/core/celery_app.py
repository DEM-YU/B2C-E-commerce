# pyrefly: ignore [missing-import]
from celery import Celery

celery_app = Celery(
    "b2c_worker",
    broker="redis://localhost:6380/0",
    backend="redis://localhost:6380/0",
    include=["app.transaction.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
