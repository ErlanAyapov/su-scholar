from __future__ import annotations

import asyncio
import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .ws_confirm_gate import CONFIRM_TIMEOUT, ConfirmationGate


logger = logging.getLogger(__name__)
CONFIRM_TIMEOUT_SECONDS = int(CONFIRM_TIMEOUT)


class DocEditConsumer(AsyncJsonWebsocketConsumer):
    """
    Interactive websocket channel for document_operation flow.
    Group: doc_edit_{project_id}_{session_id}
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        self.project_id = self.scope["url_route"]["kwargs"]["project_id"]
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"doc_edit_{self.project_id}_{self.session_id}"

        self._confirm_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if self._confirm_queue.empty():
            await self._confirm_queue.put("reject")
        ConfirmationGate.push_decision(self.project_id, self.session_id, "reject")

    async def receive_json(self, content, **kwargs):
        action = str((content or {}).get("action") or "").strip().lower()
        if action not in {"confirm", "reject"}:
            return

        if self._confirm_queue.empty():
            await self._confirm_queue.put(action)

        ConfirmationGate.push_decision(self.project_id, self.session_id, action)

    async def doc_edit_event(self, event):
        await self.send_json((event or {}).get("data") or {})

    async def gate_resolve(self, event):
        action = str((event or {}).get("action") or "reject")
        if self._confirm_queue.empty():
            await self._confirm_queue.put(action)

    async def wait_for_confirmation(self) -> str:
        try:
            return await asyncio.wait_for(
                self._confirm_queue.get(),
                timeout=CONFIRM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return "timeout"
