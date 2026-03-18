from django.contrib import admin

from llm.models import ChatSession, Message


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
