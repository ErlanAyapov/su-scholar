"""Channels consumers for realtime websocket updates."""

from datetime import datetime

from channels.generic.websocket import AsyncJsonWebsocketConsumer


class LiveUpdatesConsumer(AsyncJsonWebsocketConsumer):
    """WebSocket consumer for realtime app notifications."""

    async def connect(self):
        user = self.scope.get("user")
        self.groups_to_join = {"public_updates"}
        if user and user.is_authenticated:
            self.groups_to_join.add(f"user_{user.id}")

        for group_name in self.groups_to_join:
            await self.channel_layer.group_add(group_name, self.channel_name)

        await self.accept()
        await self.send_json(
            {
                "type": "welcome",
                "message": "WebSocket connected",
                "authenticated": bool(user and user.is_authenticated),
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        )

    async def disconnect(self, code):
        for group_name in getattr(self, "groups_to_join", set()):
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = (content or {}).get("action")
        if action == "ping":
            await self.send_json(
                {
                    "type": "pong",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            )

    async def ws_message(self, event):
        payload = event.get("payload", {})
        await self.send_json(payload)
