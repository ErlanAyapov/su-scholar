from __future__ import annotations

import logging
from typing import Any, Mapping

from celery import Task

from core.models import CeleryTaskLog
from core.services.celery_task_logger import CeleryTaskLogger

logger = logging.getLogger(__name__)


class LoggedTask(Task):
    abstract = True

    def _normalize_result(self, value: Any) -> dict:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return dict(value)
        return {"value": value}

    def _extract_task_context(
        self,
        *,
        args: tuple[Any, ...] | None = None,
        kwargs: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict:
        context: dict[str, Any] = {}

        request = getattr(self, "request", None)
        if request is not None:
            parent_task_id = getattr(request, "parent_id", "") or ""
            if parent_task_id:
                context["parent_task_id"] = parent_task_id

            worker_hostname = getattr(request, "hostname", "") or ""
            if worker_hostname:
                context["worker_hostname"] = worker_hostname

            delivery_info = getattr(request, "delivery_info", None) or {}
            queue_name = ""
            if isinstance(delivery_info, Mapping):
                queue_name = str(delivery_info.get("routing_key") or delivery_info.get("queue") or "").strip()
            if queue_name:
                context["queue_name"] = queue_name

        source_kwargs = dict(kwargs or {})
        if source_kwargs:
            if source_kwargs.get("object_type"):
                context["object_type"] = str(source_kwargs.get("object_type"))
            if source_kwargs.get("object_id") is not None:
                context["object_id"] = str(source_kwargs.get("object_id"))
            if source_kwargs.get("username"):
                context["username"] = str(source_kwargs.get("username"))

            if "object_id" not in context and source_kwargs.get("user_id") is not None:
                context["object_type"] = "user"
                context["object_id"] = str(source_kwargs.get("user_id"))

        if extra:
            for key in ("object_type", "object_id", "username"):
                if extra.get(key) is not None:
                    context[key] = str(extra.get(key))

        return context

    def _safe_log(self, handler_name: str, **payload) -> None:
        handler = getattr(CeleryTaskLogger, handler_name)
        try:
            handler(**payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write celery task log via %s for task_id=%s error=%s", handler_name, payload.get("task_id"), exc)

    def before_start(self, task_id, args, kwargs):  # noqa: ANN001
        context = self._extract_task_context(args=args, kwargs=kwargs)
        self._safe_log(
            "mark_received",
            task_id=task_id,
            task_name=self.name,
            message="Task received by worker",
            **context,
        )
        self._safe_log(
            "mark_started",
            task_id=task_id,
            task_name=self.name,
            message="Task execution started",
            **context,
        )
        super().before_start(task_id, args, kwargs)

    def on_success(self, retval, task_id, args, kwargs):  # noqa: ANN001
        context = self._extract_task_context(args=args, kwargs=kwargs)
        normalized_result = self._normalize_result(retval)
        status_hint = str(normalized_result.get("status", "")).strip().lower()

        if status_hint in {"not_found", "skipped"}:
            reason = str(normalized_result.get("reason") or normalized_result.get("status") or "Task skipped")
            self._safe_log(
                "create_or_update_log",
                task_id=task_id,
                task_name=self.name,
                status=CeleryTaskLog.STATUS_SKIPPED,
                level=CeleryTaskLog.LEVEL_WARNING,
                message=reason,
                result=normalized_result,
                is_finished=True,
                is_success=False,
                **context,
            )
        else:
            self._safe_log(
                "mark_success",
                task_id=task_id,
                task_name=self.name,
                message="Task completed successfully",
                result=normalized_result,
                **context,
            )

        super().on_success(retval, task_id, args, kwargs)

    def on_failure(self, exc, task_id, args, kwargs, einfo):  # noqa: ANN001
        context = self._extract_task_context(args=args, kwargs=kwargs)
        message = str(exc) or "Task failed"
        traceback_text = ""
        if einfo is not None:
            traceback_text = str(getattr(einfo, "traceback", "") or getattr(einfo, "traceback_text", ""))

        self._safe_log(
            "mark_failure",
            task_id=task_id,
            task_name=self.name,
            message=message,
            traceback_text=traceback_text,
            result={"exception": exc.__class__.__name__, "error": message},
            **context,
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)

    def on_retry(self, exc, task_id, args, kwargs, einfo):  # noqa: ANN001
        context = self._extract_task_context(args=args, kwargs=kwargs)
        message = str(exc) if exc else "Task scheduled for retry"
        self._safe_log(
            "mark_retry",
            task_id=task_id,
            task_name=self.name,
            message=message,
            **context,
        )
        super().on_retry(exc, task_id, args, kwargs, einfo)

    def log_progress(
        self,
        *,
        message: str,
        current: int | None = None,
        total: int | None = None,
        meta: Mapping[str, Any] | None = None,
        **context,
    ) -> None:
        task_id = getattr(self.request, "id", None)
        if not task_id:
            return

        merged_context = self._extract_task_context(extra=context)
        self._safe_log(
            "mark_progress",
            task_id=task_id,
            task_name=self.name,
            message=message,
            current=current,
            total=total,
            meta=meta,
            **merged_context,
        )

    def log_info(self, message: str, *, meta: Mapping[str, Any] | None = None, **context) -> None:
        self._append_message(message=message, level=CeleryTaskLog.LEVEL_INFO, meta=meta, **context)

    def log_warning(self, message: str, *, meta: Mapping[str, Any] | None = None, **context) -> None:
        self._append_message(message=message, level=CeleryTaskLog.LEVEL_WARNING, meta=meta, **context)

    def log_error(self, message: str, *, meta: Mapping[str, Any] | None = None, **context) -> None:
        self._append_message(message=message, level=CeleryTaskLog.LEVEL_ERROR, meta=meta, **context)

    def _append_message(self, *, message: str, level: str, meta: Mapping[str, Any] | None = None, **context) -> None:
        task_id = getattr(self.request, "id", None)
        if not task_id:
            return

        merged_context = self._extract_task_context(extra=context)
        self._safe_log(
            "append_message",
            task_id=task_id,
            task_name=self.name,
            message=message,
            level=level,
            meta=meta,
            **merged_context,
        )
