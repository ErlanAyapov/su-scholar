from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from core.models import CeleryTaskLog

logger = logging.getLogger(__name__)

GROUP_ALL = "celery_logs_all"


def _clean_group_part(value: Any, max_len: int) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", text)
    cleaned = cleaned.strip("-._")
    if not cleaned:
        return "unknown"
    return cleaned[:max_len]


def task_group_name(task_name: str) -> str:
    return f"celery_logs_task_{_clean_group_part(task_name, max_len=70)}"


def task_group_name_from_slug(task_slug: str) -> str:
    return f"celery_logs_task_{_clean_group_part(task_slug, max_len=70)}"


def object_group_name(object_type: str, object_id: str) -> str:
    return (
        f"celery_logs_object_{_clean_group_part(object_type, max_len=30)}_"
        f"{_clean_group_part(object_id, max_len=50)}"
    )


def _serialize_datetime(value):
    if value is None:
        return None
    return value.isoformat()


def _serialize_decimal(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def serialize_task_log(log: CeleryTaskLog) -> dict:
    return {
        "id": log.id,
        "task_id": log.task_id,
        "parent_task_id": log.parent_task_id,
        "task_name": log.task_name,
        "queue_name": log.queue_name,
        "status": log.status,
        "level": log.level,
        "message": log.message,
        "result": log.result or {},
        "meta": log.meta or {},
        "progress_current": log.progress_current,
        "progress_total": log.progress_total,
        "progress_percent": _serialize_decimal(log.progress_percent),
        "object_type": log.object_type,
        "object_id": log.object_id,
        "username": log.username,
        "started_at": _serialize_datetime(log.started_at),
        "finished_at": _serialize_datetime(log.finished_at),
        "duration_ms": log.duration_ms,
        "worker_hostname": log.worker_hostname,
        "traceback_text": log.traceback_text,
        "is_finished": log.is_finished,
        "is_success": log.is_success,
        "created_at": _serialize_datetime(log.created_at),
        "updated_at": _serialize_datetime(log.updated_at),
    }


def _publish_to_group(group_name: str, payload: dict) -> bool:
    layer = get_channel_layer()
    if layer is None:
        return False

    try:
        async_to_sync(layer.group_send)(
            group_name,
            {
                "type": "ws.message",
                "payload": payload,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to push celery task log payload group=%s error=%s", group_name, exc)
        return False


def broadcast_task_log_updated(log: CeleryTaskLog) -> None:
    payload = {
        "type": "task_log.updated",
        "data": serialize_task_log(log),
    }

    groups = {GROUP_ALL}
    if log.task_name:
        groups.add(task_group_name(log.task_name))
    if log.object_type and log.object_id:
        groups.add(object_group_name(log.object_type, log.object_id))

    for group_name in groups:
        _publish_to_group(group_name, payload)
