"""ASGI websocket routing configuration."""

from main.routing import websocket_urlpatterns as main_websocket_urlpatterns

websocket_urlpatterns = [
    *main_websocket_urlpatterns,
]

