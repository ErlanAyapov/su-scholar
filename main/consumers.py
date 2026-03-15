"""Channels consumers for realtime websocket updates."""

import logging
from datetime import datetime

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


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


class LlmChatConsumer(AsyncJsonWebsocketConsumer):
    """WebSocket consumer for LLM streaming chat responses."""

    @staticmethod
    def _prepare_messages(raw_messages, prompt: str) -> list[dict[str, str]]:
        allowed_roles = {"user", "assistant", "system"}
        prepared: list[dict[str, str]] = []

        if isinstance(raw_messages, list):
            for item in raw_messages[-20:]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().lower()
                content = str(item.get("content") or "").strip()
                if role not in allowed_roles or not content:
                    continue
                prepared.append({"role": role, "content": content[:4000]})

        if prompt:
            if not prepared or prepared[-1]["role"] != "user" or prepared[-1]["content"] != prompt:
                prepared.append({"role": "user", "content": prompt[:4000]})

        return prepared

    async def connect(self):
        self.is_busy = False
        await self.accept()
        await self.send_json(
            {
                "type": "llm_ready",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        )

    async def receive_json(self, content, **kwargs):
        payload = content or {}
        action = (payload.get("action") or "").strip().lower()

        if action == "ping":
            await self.send_json(
                {
                    "type": "pong",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            )
            return

        if action != "ask":
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Unsupported action",
                }
            )
            return

        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Prompt is empty",
                }
            )
            return

        if self.is_busy:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Generation is already in progress",
                }
            )
            return

        model = (payload.get("model") or getattr(settings, "LLM_MODEL", "gpt-oss:20b")).strip() or "gpt-oss:20b"
        messages = self._prepare_messages(payload.get("messages"), prompt)
        base_url = (getattr(settings, "LLM_API", "") or "").strip()
        api_key = (getattr(settings, "LLM_API_KEY", "") or "").strip() or "ollama"

        if not base_url:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "LLM_API is not configured",
                }
            )
            return

        self.is_busy = True
        await self.send_json(
            {
                "type": "llm_started",
                "model": model,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        )

        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        collected_chunks: list[str] = []

        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
            )
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None)
                if not text:
                    continue
                collected_chunks.append(text)
                await self.send_json(
                    {
                        "type": "llm_delta",
                        "delta": text,
                    }
                )

            await self.send_json(
                {
                    "type": "llm_done",
                    "model": model,
                    "text": "".join(collected_chunks),
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            )
        except Exception as exc:
            logger.exception("LLM websocket stream failed: %s", exc)
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": str(exc),
                }
            )
        finally:
            self.is_busy = False
            await client.close()
