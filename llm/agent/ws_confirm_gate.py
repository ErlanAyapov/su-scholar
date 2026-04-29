from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

import redis
from django.conf import settings


logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 120  # seconds
_POLL_INTERVAL = 0.2


class ConfirmationGate:
    """
    Bridge sync orchestrator and async websocket confirmations.
    Works in-process and cross-process (via Redis pubsub).
    """

    _registry_lock = threading.Lock()
    _registry: dict[str, set["ConfirmationGate"]] = defaultdict(set)

    def __init__(self, project_id, session_id):
        self.project_id = int(project_id or 0)
        self.session_id = int(session_id or 0)
        self.token = f"{self.project_id}_{self.session_id}"
        self.channel = f"confirm_gate:{self.token}"

        self._event = threading.Event()
        self._result = "timeout"

    @staticmethod
    def _normalize_action(action: str) -> str:
        value = str(action or "").strip().lower()
        if value == "confirm":
            return "confirm"
        if value == "reject":
            return "reject"
        return "timeout"

    @classmethod
    def _channel_layer_redis_url(cls) -> str:
        try:
            channels_cfg = (getattr(settings, "CHANNEL_LAYERS", {}) or {}).get("default", {})
            hosts = ((channels_cfg.get("CONFIG") or {}).get("hosts") or [])
            if not hosts:
                return ""
            first_host = hosts[0]
            if isinstance(first_host, str):
                return first_host
            if isinstance(first_host, (list, tuple)) and first_host:
                return str(first_host[0] or "")
            if isinstance(first_host, dict):
                return str(first_host.get("address") or first_host.get("url") or "")
        except Exception:  # noqa: BLE001
            return ""
        return ""

    @classmethod
    def _redis_client(cls):
        redis_url = cls._channel_layer_redis_url()
        if not redis_url:
            return None
        try:
            client = redis.from_url(redis_url)
            client.ping()
            return client
        except Exception:  # noqa: BLE001
            logger.debug("ConfirmationGate redis is unavailable", exc_info=True)
            return None

    @classmethod
    def push_decision(cls, project_id, session_id, action: str):
        normalized = cls._normalize_action(action)
        token = f"{int(project_id or 0)}_{int(session_id or 0)}"

        with cls._registry_lock:
            targets = list(cls._registry.get(token) or [])
        for gate in targets:
            gate.resolve(normalized)

        client = cls._redis_client()
        if client:
            try:
                client.publish(f"confirm_gate:{token}", normalized)
            except Exception:  # noqa: BLE001
                logger.debug("ConfirmationGate redis publish failed", exc_info=True)

    def resolve(self, action: str):
        self._result = self._normalize_action(action)
        self._event.set()

    def _register(self):
        with self._registry_lock:
            self._registry[self.token].add(self)

    def _unregister(self):
        with self._registry_lock:
            bucket = self._registry.get(self.token)
            if not bucket:
                return
            bucket.discard(self)
            if not bucket:
                self._registry.pop(self.token, None)

    def wait(self, timeout: float = CONFIRM_TIMEOUT) -> str:
        timeout_value = float(timeout or CONFIRM_TIMEOUT)
        if timeout_value <= 0:
            return "timeout"

        self._register()
        deadline = time.monotonic() + timeout_value

        client = self._redis_client()
        pubsub = None
        if client:
            try:
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self.channel)
            except Exception:  # noqa: BLE001
                pubsub = None

        try:
            while True:
                if self._event.is_set():
                    return self._result

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout"

                if pubsub:
                    try:
                        message = pubsub.get_message(timeout=min(1.0, remaining))
                    except Exception:  # noqa: BLE001
                        message = None
                    if message and message.get("type") == "message":
                        payload = message.get("data")
                        if isinstance(payload, bytes):
                            payload = payload.decode("utf-8", errors="ignore")
                        self.resolve(str(payload or ""))
                        return self._result

                self._event.wait(timeout=min(_POLL_INTERVAL, remaining))
        finally:
            if pubsub:
                try:
                    pubsub.unsubscribe(self.channel)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    pubsub.close()
                except Exception:  # noqa: BLE001
                    pass
            self._unregister()
