"""WebSocket URL routes for the main app."""

from django.urls import re_path

from llm.agent.ws_doc_edit_consumer import DocEditConsumer
from llm.consumers import LlmChatConsumer, ProjectAgentConsumer
from main.consumers import LiveUpdatesConsumer

websocket_urlpatterns = [
    re_path(r"^ws/updates/$", LiveUpdatesConsumer.as_asgi()),
    re_path(r"^ws/llm/$", LlmChatConsumer.as_asgi()),
    re_path(r"^ws/project_agent/(?P<project_id>\d+)/(?P<session_id>\d+)/$", ProjectAgentConsumer.as_asgi()),
    re_path(r"^ws/doc-edit/(?P<project_id>\d+)/(?P<session_id>\d+)/$", DocEditConsumer.as_asgi()),
]
