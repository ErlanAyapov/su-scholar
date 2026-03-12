from django.urls import re_path

from core.consumers import CeleryTaskLogConsumer

websocket_urlpatterns = [
    re_path(r"^ws/celery-task-logs/$", CeleryTaskLogConsumer.as_asgi()),
    re_path(r"^ws/celery-task-logs/task/(?P<task_slug>[-\w.]+)/$", CeleryTaskLogConsumer.as_asgi()),
    re_path(
        r"^ws/celery-task-logs/object/(?P<object_type>[-\w]+)/(?P<object_id>[-\w.:@]+)/$",
        CeleryTaskLogConsumer.as_asgi(),
    ),
]
