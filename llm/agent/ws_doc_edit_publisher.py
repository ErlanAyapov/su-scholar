from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


logger = logging.getLogger(__name__)


class DocEditPublisher:
    """
    Publish document-edit interaction events for /ws/doc-edit/{project_id}/{session_id}/.
    """

    def __init__(self, project_id, session_id):
        self.project_id = int(project_id or 0)
        self.session_id = int(session_id or 0)
        self.group_name = f"doc_edit_{self.project_id}_{self.session_id}"
        self._channel_layer = get_channel_layer()

    def publish(self, stage: str, **payload):
        if not self._channel_layer:
            return
        data = {"stage": str(stage or "").strip(), **payload}
        try:
            async_to_sync(self._channel_layer.group_send)(
                self.group_name,
                {
                    "type": "doc_edit_event",
                    "data": data,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("DocEditPublisher.publish failed: %s", exc)

    def plan_ready(self, plan: dict, operations_count: int):
        self.publish(
            "plan_ready",
            plan=plan or {},
            operations_count=int(operations_count or 0),
            message=f"LLM предлагает {int(operations_count or 0)} изменений. Применить?",
        )

    def applying(self, message: str = "Применяю изменения..."):
        self.publish("applying", message=message)

    def done(self, message: str, applied: int, reload_editor: bool = False):
        self.publish(
            "done",
            message=message,
            applied=int(applied or 0),
            reload_editor=bool(reload_editor),
        )

    def rejected(self):
        self.publish("rejected", message="Изменения отклонены.")

    def timeout(self):
        self.publish("timeout", message="Время ожидания подтверждения истекло. Изменения не применены.")

    def error(self, message: str):
        self.publish("error", message=message)
