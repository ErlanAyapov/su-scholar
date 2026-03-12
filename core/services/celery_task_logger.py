from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from core.models import CeleryTaskLog
from core.services.celery_task_channels import broadcast_task_log_updated

logger = logging.getLogger(__name__)

FINAL_STATUSES = {
    CeleryTaskLog.STATUS_SUCCESS,
    CeleryTaskLog.STATUS_FAILURE,
    CeleryTaskLog.STATUS_REVOKED,
    CeleryTaskLog.STATUS_SKIPPED,
}


def _merge_json_dicts(existing: Any, incoming: Any) -> Any:
    if incoming is None:
        return existing
    if isinstance(existing, Mapping) and isinstance(incoming, Mapping):
        merged = dict(existing)
        for key, value in incoming.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged
    return incoming


def _to_decimal_percent(current: int | None, total: int | None) -> Decimal | None:
    if current is None or total in (None, 0):
        return None
    if total < 0:
        return None

    value = (Decimal(current) * Decimal("100")) / Decimal(total)
    if value < 0:
        value = Decimal("0")
    if value > Decimal("100"):
        value = Decimal("100")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _safe_message_history_item(message: str, level: str) -> dict:
    return {
        "message": (message or "")[:500],
        "level": level,
        "timestamp": timezone.now().isoformat(),
    }


class CeleryTaskLogger:
    @classmethod
    def create_or_update_log(
        cls,
        *,
        task_id: str,
        task_name: str | None = None,
        parent_task_id: str | None = None,
        queue_name: str | None = None,
        status: str | None = None,
        level: str | None = None,
        message: str | None = None,
        result: Any | None = None,
        meta: Mapping[str, Any] | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
        progress_percent: Decimal | float | None = None,
        object_type: str | None = None,
        object_id: str | int | None = None,
        username: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        worker_hostname: str | None = None,
        traceback_text: str | None = None,
        is_finished: bool | None = None,
        is_success: bool | None = None,
    ) -> CeleryTaskLog:
        if not task_id:
            raise ValueError("task_id is required")

        now = timezone.now()
        log, _ = CeleryTaskLog.objects.get_or_create(
            task_id=task_id,
            defaults={
                "task_name": task_name or "",
                "status": status or CeleryTaskLog.STATUS_PENDING,
                "level": level or CeleryTaskLog.LEVEL_INFO,
                "message": message or "",
            },
        )

        if task_name is not None:
            log.task_name = str(task_name)
        if parent_task_id is not None:
            log.parent_task_id = str(parent_task_id)
        if queue_name is not None:
            log.queue_name = str(queue_name)
        if status is not None:
            log.status = status
        if level is not None:
            log.level = level
        if message is not None:
            log.message = str(message)[:500]

        if meta is not None:
            log.meta = _merge_json_dicts(log.meta or {}, meta)

        if result is not None:
            normalized_result = result
            if not isinstance(result, Mapping):
                normalized_result = {"value": result}
            log.result = _merge_json_dicts(log.result or {}, normalized_result)

        if progress_current is not None:
            log.progress_current = progress_current
        if progress_total is not None:
            log.progress_total = progress_total
        if progress_percent is not None:
            log.progress_percent = progress_percent
        elif progress_current is not None or progress_total is not None:
            log.progress_percent = _to_decimal_percent(log.progress_current, log.progress_total)

        if object_type is not None:
            log.object_type = str(object_type)
        if object_id is not None:
            log.object_id = str(object_id)
        if username is not None:
            log.username = str(username)
        if worker_hostname is not None:
            log.worker_hostname = str(worker_hostname)
        if traceback_text is not None:
            log.traceback_text = str(traceback_text)

        if started_at is not None:
            log.started_at = started_at
        elif status in {CeleryTaskLog.STATUS_STARTED, CeleryTaskLog.STATUS_PROGRESS} and not log.started_at:
            log.started_at = now

        if finished_at is not None:
            log.finished_at = finished_at
        elif status in FINAL_STATUSES and not log.finished_at:
            log.finished_at = now

        if is_finished is not None:
            log.is_finished = is_finished
        elif status in FINAL_STATUSES:
            log.is_finished = True

        if is_success is not None:
            log.is_success = is_success
        elif status == CeleryTaskLog.STATUS_SUCCESS:
            log.is_success = True
        elif status in FINAL_STATUSES:
            log.is_success = False

        if log.started_at and log.finished_at:
            delta_ms = (log.finished_at - log.started_at).total_seconds() * 1000
            log.duration_ms = max(int(delta_ms), 0)

        log.save()
        transaction.on_commit(lambda: broadcast_task_log_updated(log))
        return log

    @classmethod
    def append_message(
        cls,
        *,
        task_id: str,
        task_name: str | None = None,
        message: str,
        level: str = CeleryTaskLog.LEVEL_INFO,
        status: str | None = None,
        meta: Mapping[str, Any] | None = None,
        **kwargs,
    ) -> CeleryTaskLog:
        existing = CeleryTaskLog.objects.filter(task_id=task_id).values_list("meta", flat=True).first() or {}
        history = []
        if isinstance(existing, Mapping):
            maybe_history = existing.get("messages")
            if isinstance(maybe_history, list):
                history = maybe_history[-49:]
        history.append(_safe_message_history_item(message=message, level=level))

        merged_meta = {"messages": history}
        if meta:
            merged_meta = _merge_json_dicts(merged_meta, meta)

        return cls.create_or_update_log(
            task_id=task_id,
            task_name=task_name,
            status=status,
            level=level,
            message=message,
            meta=merged_meta,
            **kwargs,
        )

    @classmethod
    def mark_received(cls, *, task_id: str, task_name: str, message: str = "Task received", **kwargs) -> CeleryTaskLog:
        return cls.create_or_update_log(
            task_id=task_id,
            task_name=task_name,
            status=CeleryTaskLog.STATUS_RECEIVED,
            level=CeleryTaskLog.LEVEL_INFO,
            message=message,
            **kwargs,
        )

    @classmethod
    def mark_started(cls, *, task_id: str, task_name: str, message: str = "Task started", **kwargs) -> CeleryTaskLog:
        return cls.create_or_update_log(
            task_id=task_id,
            task_name=task_name,
            status=CeleryTaskLog.STATUS_STARTED,
            level=CeleryTaskLog.LEVEL_INFO,
            message=message,
            started_at=timezone.now(),
            **kwargs,
        )

    @classmethod
    def mark_progress(
        cls,
        *,
        task_id: str,
        task_name: str,
        message: str,
        current: int | None = None,
        total: int | None = None,
        meta: Mapping[str, Any] | None = None,
        **kwargs,
    ) -> CeleryTaskLog:
        return cls.create_or_update_log(
            task_id=task_id,
            task_name=task_name,
            status=CeleryTaskLog.STATUS_PROGRESS,
            level=CeleryTaskLog.LEVEL_INFO,
            message=message,
            progress_current=current,
            progress_total=total,
            meta=meta,
            **kwargs,
        )

    @classmethod
    def mark_success(
        cls,
        *,
        task_id: str,
        task_name: str,
        message: str = "Task completed successfully",
        result: Any | None = None,
        **kwargs,
    ) -> CeleryTaskLog:
        return cls.create_or_update_log(
            task_id=task_id,
            task_name=task_name,
            status=CeleryTaskLog.STATUS_SUCCESS,
            level=CeleryTaskLog.LEVEL_INFO,
            message=message,
            result=result,
            finished_at=timezone.now(),
            is_finished=True,
            is_success=True,
            **kwargs,
        )

    @classmethod
    def mark_failure(
        cls,
        *,
        task_id: str,
        task_name: str,
        message: str,
        traceback_text: str = "",
        result: Any | None = None,
        **kwargs,
    ) -> CeleryTaskLog:
        return cls.create_or_update_log(
            task_id=task_id,
            task_name=task_name,
            status=CeleryTaskLog.STATUS_FAILURE,
            level=CeleryTaskLog.LEVEL_ERROR,
            message=message,
            traceback_text=traceback_text,
            result=result,
            finished_at=timezone.now(),
            is_finished=True,
            is_success=False,
            **kwargs,
        )

    @classmethod
    def mark_retry(
        cls,
        *,
        task_id: str,
        task_name: str,
        message: str = "Task retry requested",
        **kwargs,
    ) -> CeleryTaskLog:
        return cls.create_or_update_log(
            task_id=task_id,
            task_name=task_name,
            status=CeleryTaskLog.STATUS_RETRY,
            level=CeleryTaskLog.LEVEL_WARNING,
            message=message,
            **kwargs,
        )
