"""ASGI websocket routing configuration."""

from core.routing import websocket_urlpatterns as core_websocket_urlpatterns
from main.routing import websocket_urlpatterns as main_websocket_urlpatterns

websocket_urlpatterns = [
    *core_websocket_urlpatterns,
    *main_websocket_urlpatterns,
]
