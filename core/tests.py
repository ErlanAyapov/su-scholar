from django.test import TestCase

from core.models import CeleryTaskLog
from core.services.celery_task_logger import CeleryTaskLogger


class CeleryTaskLoggerTests(TestCase):
    def test_create_progress_and_success_flow(self):
        task_id = "task-123"

        CeleryTaskLogger.mark_received(task_id=task_id, task_name="account.tasks.example")
        CeleryTaskLogger.mark_started(task_id=task_id, task_name="account.tasks.example")
        CeleryTaskLogger.mark_progress(
            task_id=task_id,
            task_name="account.tasks.example",
            message="Step 1",
            current=1,
            total=4,
            meta={"step": 1},
        )
        CeleryTaskLogger.mark_success(
            task_id=task_id,
            task_name="account.tasks.example",
            message="Done",
            result={"status": "ok"},
        )

        log = CeleryTaskLog.objects.get(task_id=task_id)
        self.assertEqual(log.status, CeleryTaskLog.STATUS_SUCCESS)
        self.assertEqual(log.meta.get("step"), 1)
        self.assertEqual(log.progress_current, 1)
        self.assertEqual(log.progress_total, 4)
        self.assertTrue(log.is_finished)
        self.assertTrue(log.is_success)
        self.assertIsNotNone(log.duration_ms)
