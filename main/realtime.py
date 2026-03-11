"""Helpers to publish websocket events through Channels layer."""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def _push_group_payload(group_name: str, payload: dict) -> bool:
    layer = get_channel_layer()
    if layer is None:
        logger.warning("Channels layer is not configured; payload skipped group=%s", group_name)
        return False

    try:
        async_to_sync(layer.group_send)(
            group_name,
            {
                "type": "ws.message",
                "payload": payload,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to push websocket payload group=%s error=%s", group_name, exc)
        return False


def notify_user(user_id: int, message: str, level: str = "info", **extra) -> bool:
    payload = {
        "type": "notification",
        "level": level,
        "message": message,
        **extra,
    }
    return _push_group_payload(f"user_{user_id}", payload)


def notify_public(message: str, level: str = "info", **extra) -> bool:
    payload = {
        "type": "notification",
        "level": level,
        "message": message,
        **extra,
    }
    return _push_group_payload("public_updates", payload)
