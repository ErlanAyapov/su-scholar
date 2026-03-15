"""WebSocket URL routes for the main app."""

from django.urls import re_path

from main.consumers import LiveUpdatesConsumer, LlmChatConsumer

websocket_urlpatterns = [
    re_path(r"^ws/updates/$", LiveUpdatesConsumer.as_asgi()),
    re_path(r"^ws/llm/$", LlmChatConsumer.as_asgi()),
]
