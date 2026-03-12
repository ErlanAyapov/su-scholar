from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.management import call_command
from django.db import DatabaseError
from django.http import HttpResponseRedirect, JsonResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse

from account.models import Department, Role, University, User
from account.tasks import enqueue_satbayev_enrichment, enrich_user_profile_from_satbayev
from core.models import CeleryTaskLog
from core.services.celery_task_channels import serialize_task_log
from main.tasks import (
    import_publications_for_all_users_task,
    import_publications_from_google_scholar_for_all_users_task,
    import_publications_from_google_scholar_task,
    import_user_publications_task,
)


def _parse_positive_int(raw_value, default: int) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return value


def _message_level_name(level: int) -> str:
    mapping = {
        messages.DEBUG: "info",
        messages.INFO: "info",
        messages.SUCCESS: "success",
        messages.WARNING: "warning",
        messages.ERROR: "error",
    }
    return mapping.get(level, "info")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    change_list_template = "admin/account/user/change_list.html"
    list_display = (
        "id",
        "username",
        "last_name",
        "first_name",
        "is_user",
        "is_active",
        "department",
    )
    list_filter = ("is_user", "is_active", "gender", "department")
    search_fields = ("username", "last_name", "first_name", "email", "orc_id", "scopus_id")
    ordering = ("id",)
    actions = (
        "action_import_publications_selected",
        "action_import_scholar_selected",
        "action_enrich_satbayev_selected",
    )

    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "Academic",
            {
                "fields": (
                    "father_name",
                    "phone_number",
                    "inn",
                    "photo",
                    "department",
                    "universities",
                    "roles",
                    "is_user",
                    "banned",
                    "quiet_mode",
                    "gender",
                    "orc_id",
                    "scopus_id",
                    "wos_id",
                    "researchgate",
                    "google_scholar",
                    "satbayev_profile_url",
                    "journal_links",
                    "journal_ids",
                ),
            },
        ),
    )
    filter_horizontal = ("groups", "user_permissions", "universities", "roles")

    def get_urls(self):
        urls = super().get_urls()
        info = self.model._meta.app_label, self.model._meta.model_name
        custom_urls = [
            path(
                "bulk-operations/",
                self.admin_site.admin_view(self.bulk_operations_view),
                name="%s_%s_bulk_operations" % info,
            ),
            path(
                "bulk-operations/logs/",
                self.admin_site.admin_view(self.bulk_operations_logs_view),
                name="%s_%s_bulk_operations_logs" % info,
            ),
        ]
        return custom_urls + urls

    @admin.action(description="Импорт публикаций (ORCID/OpenAlex) для выбранных")
    def action_import_publications_selected(self, request, queryset):
        user_ids = list(queryset.values_list("id", flat=True))
        for user_id in user_ids:
            import_user_publications_task.delay(user_id=user_id, force=False)
        self.message_user(
            request,
            f"Поставлено в очередь: {len(user_ids)} задач ORCID/OpenAlex импорта.",
            level=messages.SUCCESS,
        )

    @admin.action(description="Импорт публикаций (Google Scholar) для выбранных")
    def action_import_scholar_selected(self, request, queryset):
        user_ids = list(queryset.values_list("id", flat=True))
        for user_id in user_ids:
            import_publications_from_google_scholar_task.delay(user_id=user_id, force=False)
        self.message_user(
            request,
            f"Поставлено в очередь: {len(user_ids)} задач Google Scholar импорта.",
            level=messages.SUCCESS,
        )

    @admin.action(description="Обогатить профили Satbayev для выбранных")
    def action_enrich_satbayev_selected(self, request, queryset):
        user_ids = list(queryset.values_list("id", flat=True))
        for user_id in user_ids:
            enrich_user_profile_from_satbayev.delay(user_id=user_id, force=False)
        self.message_user(
            request,
            f"Поставлено в очередь: {len(user_ids)} задач Satbayev enrichment.",
            level=messages.SUCCESS,
        )

    def _run_bulk_operation(self, operation: str, limit: int, force: bool) -> dict:
        if operation == "seed_inactive_users":
            call_command("seed_inactive_users")
            return {
                "level": messages.SUCCESS,
                "message": "Команда seed_inactive_users выполнена успешно.",
            }

        if operation == "enqueue_satbayev_enrichment":
            task = enqueue_satbayev_enrichment.delay(limit=limit, force=force)
            return {
                "level": messages.SUCCESS,
                "message": f"Satbayev enrichment поставлен в очередь. Task ID: {task.id}",
                "task_id": task.id,
            }

        if operation == "import_publications_all":
            task = import_publications_for_all_users_task.delay(limit=limit, force=force)
            return {
                "level": messages.SUCCESS,
                "message": f"Импорт ORCID/OpenAlex поставлен в очередь. Task ID: {task.id}",
                "task_id": task.id,
            }

        if operation == "import_scholar_all":
            task = import_publications_from_google_scholar_for_all_users_task.delay(force=force)
            return {
                "level": messages.SUCCESS,
                "message": f"Импорт Google Scholar поставлен в очередь. Task ID: {task.id}",
                "task_id": task.id,
            }

        return {
            "level": messages.ERROR,
            "message": "Неизвестная операция.",
        }

    def bulk_operations_view(self, request):
        info = self.model._meta.app_label, self.model._meta.model_name
        changelist_url = reverse("admin:%s_%s_changelist" % info)
        bulk_logs_url = reverse("admin:%s_%s_bulk_operations_logs" % info)

        if request.method == "POST":
            operation = (request.POST.get("operation") or "").strip()
            limit = _parse_positive_int(request.POST.get("limit"), default=200)
            force = request.POST.get("force") == "1"

            result = self._run_bulk_operation(operation=operation, limit=limit, force=force)
            self.message_user(request, result["message"], level=result["level"])

            is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
            if is_ajax:
                payload = {
                    "ok": result["level"] != messages.ERROR,
                    "message": result["message"],
                    "level": _message_level_name(result["level"]),
                    "operation": operation,
                    "task_id": result.get("task_id", ""),
                }
                return JsonResponse(payload, status=200 if payload["ok"] else 400)

            return HttpResponseRedirect(request.path)

        try:
            recent_logs = list(CeleryTaskLog.objects.order_by("-updated_at", "-id")[:60])
        except DatabaseError:
            recent_logs = []
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Массовые операции пользователей",
            "changelist_url": changelist_url,
            "default_limit": 200,
            "bulk_logs_url": bulk_logs_url,
            "recent_task_logs": [serialize_task_log(item) for item in recent_logs],
        }
        return TemplateResponse(request, "admin/account/user/bulk_operations.html", context)

    def bulk_operations_logs_view(self, request):
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


@admin.register(University)
class UniversityAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "country")
    search_fields = ("name", "country")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "university")
    list_filter = ("university",)
    search_fields = ("name", "university__name")


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "level")
    search_fields = ("name",)
    ordering = ("level",)
