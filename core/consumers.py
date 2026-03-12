from __future__ import annotations

from datetime import datetime

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from core.services.celery_task_channels import GROUP_ALL, object_group_name, task_group_name_from_slug


class CeleryTaskLogConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated or not user.is_staff:
            await self.close(code=4403)
            return
        if not (user.is_superuser or user.has_perm("core.view_celerytasklog")):
            await self.close(code=4403)
            return

        route_kwargs = self.scope.get("url_route", {}).get("kwargs", {})
        task_slug = (route_kwargs.get("task_slug") or "").strip()
        object_type = (route_kwargs.get("object_type") or "").strip()
        object_id = (route_kwargs.get("object_id") or "").strip()

        self.groups_to_join = set()
        if object_type and object_id:
            self.groups_to_join.add(object_group_name(object_type, object_id))
        elif task_slug:
            self.groups_to_join.add(task_group_name_from_slug(task_slug))
        else:
            self.groups_to_join.add(GROUP_ALL)

        for group_name in self.groups_to_join:
            await self.channel_layer.group_add(group_name, self.channel_name)

        await self.accept()
        await self.send_json(
            {
                "type": "welcome",
                "message": "Celery task log stream connected",
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
