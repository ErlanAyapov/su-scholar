from __future__ import annotations

import json

from django.contrib import admin
from django.db import DatabaseError
from django.http import JsonResponse
from django.urls import path
from django.urls import reverse
from django.utils.html import format_html

from core.models import CeleryTaskLog
from core.services.celery_task_channels import serialize_task_log


@admin.register(CeleryTaskLog)
class CeleryTaskLogAdmin(admin.ModelAdmin):
    change_list_template = "admin/core/celerytasklog/change_list.html"
    list_display = (
        "id",
        "task_name",
        "task_id",
        "status",
        "level",
        "object_type",
        "object_id",
        "progress_percent",
        "started_at",
        "finished_at",
        "duration_ms",
    )
    list_filter = ("status", "level", "task_name", "object_type", "created_at")
    search_fields = ("task_id", "task_name", "message", "object_id")
    ordering = ("-updated_at", "-id")
    readonly_fields = (
        "task_id",
        "parent_task_id",
        "task_name",
        "queue_name",
        "status",
        "level",
        "message",
        "formatted_result",
        "formatted_meta",
        "progress_current",
        "progress_total",
        "progress_percent",
        "object_type",
        "object_id",
        "username",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "duration_ms",
        "worker_hostname",
        "formatted_traceback_text",
        "is_finished",
        "is_success",
    )
    fieldsets = (
        (
            "Task",
            {
                "fields": (
                    "task_id",
                    "parent_task_id",
                    "task_name",
                    "queue_name",
                    "status",
                    "level",
                    "message",
                    "worker_hostname",
                )
            },
        ),
        (
            "Object",
            {
                "fields": (
                    "object_type",
                    "object_id",
                    "username",
                )
            },
        ),
        (
            "Progress",
            {
                "fields": (
                    "progress_current",
                    "progress_total",
                    "progress_percent",
                )
            },
        ),
        (
            "Timing",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "started_at",
                    "finished_at",
                    "duration_ms",
                    "is_finished",
                    "is_success",
                )
            },
        ),
        ("Payload", {"fields": ("formatted_result", "formatted_meta", "formatted_traceback_text")}),
    )

    @admin.display(description="Result")
    def formatted_result(self, obj: CeleryTaskLog):
        return format_html("<pre style='white-space:pre-wrap;max-width:1200px'>{}</pre>", json.dumps(obj.result or {}, ensure_ascii=False, indent=2))

    @admin.display(description="Meta")
    def formatted_meta(self, obj: CeleryTaskLog):
        return format_html("<pre style='white-space:pre-wrap;max-width:1200px'>{}</pre>", json.dumps(obj.meta or {}, ensure_ascii=False, indent=2))

    @admin.display(description="Traceback")
    def formatted_traceback_text(self, obj: CeleryTaskLog):
        text = obj.traceback_text or ""
        if not text:
            return "-"
        return format_html("<pre style='white-space:pre-wrap;max-width:1200px'>{}</pre>", text)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        latest_logs = CeleryTaskLog.objects.order_by("-updated_at", "-id")[:50]
        extra_context["live_logs"] = [serialize_task_log(item) for item in latest_logs]
        extra_context["change_url_template"] = reverse("admin:core_celerytasklog_change", args=["__id__"])
        extra_context["live_logs_url"] = reverse("admin:core_celerytasklog_live_feed")
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "live-feed/",
                self.admin_site.admin_view(self.live_feed_view),
                name="core_celerytasklog_live_feed",
            ),
        ]
        return custom_urls + urls

    def live_feed_view(self, request):
        if request.method != "GET":
            return JsonResponse({"detail": "Method not allowed"}, status=405)

        raw_since_id = request.GET.get("since_id")
        try:
            since_id = max(int(raw_since_id or 0), 0)
        except (TypeError, ValueError):
            since_id = 0

        try:
            queryset = CeleryTaskLog.objects.order_by("id")
            if since_id:
                queryset = queryset.filter(id__gt=since_id)
            logs = list(queryset[:120])
        except DatabaseError:
            logs = []

        return JsonResponse(
            {
                "ok": True,
                "logs": [serialize_task_log(item) for item in logs],
                "last_id": logs[-1].id if logs else since_id,
            }
        )
