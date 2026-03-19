from django.contrib import admin

from llm.models import ChatSession, ChatShareImport, ChatShareLink, Message


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "user", "created", "updated")
    list_filter = ("created", "updated")
    search_fields = ("title", "user__username", "user__first_name", "user__last_name")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "chat", "sended_from", "created")
    list_filter = ("sended_from", "created")
    search_fields = ("body", "chat__title", "chat__user__username")


@admin.register(ChatShareLink)
class ChatShareLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "created_by", "is_active", "created_at", "expires_at", "last_opened_at")
    list_filter = ("is_active", "created_at", "expires_at")
    search_fields = ("token", "session__title", "session__user__username", "created_by__username")


@admin.register(ChatShareImport)
class ChatShareImportAdmin(admin.ModelAdmin):
    list_display = ("id", "share_link", "user", "session", "created_at")
    list_filter = ("created_at",)
    search_fields = ("share_link__token", "user__username", "session__title")
