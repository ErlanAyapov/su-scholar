from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.management import call_command
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from account.models import Department, Role, University, User
from account.tasks import enqueue_satbayev_enrichment, enrich_user_profile_from_satbayev
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

    def bulk_operations_view(self, request):
        info = self.model._meta.app_label, self.model._meta.model_name
        changelist_url = reverse("admin:%s_%s_changelist" % info)

        if request.method == "POST":
            operation = (request.POST.get("operation") or "").strip()
            limit = _parse_positive_int(request.POST.get("limit"), default=200)
            force = request.POST.get("force") == "1"

            if operation == "seed_inactive_users":
                call_command("seed_inactive_users")
                self.message_user(
                    request,
                    "Команда seed_inactive_users выполнена успешно.",
                    level=messages.SUCCESS,
                )
            elif operation == "enqueue_satbayev_enrichment":
                task = enqueue_satbayev_enrichment.delay(limit=limit, force=force)
                self.message_user(
                    request,
                    f"Satbayev enrichment поставлен в очередь. Task ID: {task.id}",
                    level=messages.SUCCESS,
                )
            elif operation == "import_publications_all":
                task = import_publications_for_all_users_task.delay(limit=limit, force=force)
                self.message_user(
                    request,
                    f"Импорт ORCID/OpenAlex поставлен в очередь. Task ID: {task.id}",
                    level=messages.SUCCESS,
                )
            elif operation == "import_scholar_all":
                task = import_publications_from_google_scholar_for_all_users_task.delay()
                self.message_user(
                    request,
                    f"Импорт Google Scholar поставлен в очередь. Task ID: {task.id}",
                    level=messages.SUCCESS,
                )
            else:
                self.message_user(request, "Неизвестная операция.", level=messages.ERROR)

            return HttpResponseRedirect(request.path)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Массовые операции пользователей",
            "changelist_url": changelist_url,
            "default_limit": 200,
        }
        return TemplateResponse(request, "admin/account/user/bulk_operations.html", context)


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
