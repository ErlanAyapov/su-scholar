from django.db import models


class CeleryTaskLog(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_RECEIVED = "RECEIVED"
    STATUS_STARTED = "STARTED"
    STATUS_PROGRESS = "PROGRESS"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILURE = "FAILURE"
    STATUS_RETRY = "RETRY"
    STATUS_REVOKED = "REVOKED"
    STATUS_SKIPPED = "SKIPPED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RECEIVED, "Received"),
        (STATUS_STARTED, "Started"),
        (STATUS_PROGRESS, "Progress"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILURE, "Failure"),
        (STATUS_RETRY, "Retry"),
        (STATUS_REVOKED, "Revoked"),
        (STATUS_SKIPPED, "Skipped"),
    ]

    LEVEL_DEBUG = "DEBUG"
    LEVEL_INFO = "INFO"
    LEVEL_WARNING = "WARNING"
    LEVEL_ERROR = "ERROR"

    LEVEL_CHOICES = [
        (LEVEL_DEBUG, "Debug"),
        (LEVEL_INFO, "Info"),
        (LEVEL_WARNING, "Warning"),
        (LEVEL_ERROR, "Error"),
    ]

    task_id = models.CharField(max_length=64, unique=True, db_index=True)
    parent_task_id = models.CharField(max_length=64, blank=True, db_index=True)
    task_name = models.CharField(max_length=255, blank=True, db_index=True)
    queue_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default=LEVEL_INFO, db_index=True)
    message = models.CharField(max_length=500, blank=True)

    result = models.JSONField(default=dict, blank=True)
    meta = models.JSONField(default=dict, blank=True)

    progress_current = models.PositiveIntegerField(null=True, blank=True)
    progress_total = models.PositiveIntegerField(null=True, blank=True)
    progress_percent = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    object_id = models.CharField(max_length=120, blank=True, db_index=True)
    object_type = models.CharField(max_length=80, blank=True, db_index=True)
    username = models.CharField(max_length=150, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    worker_hostname = models.CharField(max_length=255, blank=True)
    traceback_text = models.TextField(blank=True)

    is_finished = models.BooleanField(default=False, db_index=True)
    is_success = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        indexes = [
            models.Index(fields=["task_name"], name="core_ctl_task_name_idx"),
            models.Index(fields=["status"], name="core_ctl_status_idx"),
            models.Index(fields=["created_at"], name="core_ctl_created_at_idx"),
            models.Index(fields=["object_type", "object_id"], name="core_ctl_object_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.task_name or 'unknown'} [{self.status}] {self.task_id}"
