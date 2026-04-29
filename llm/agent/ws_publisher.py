# agents/services/ws_publisher.py
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


class AgentProgressPublisher:
    def __init__(self, *, project_id: int, session_id: int):
        self.project_id = project_id
        self.session_id = session_id
        self.group_name = f"project_agent_{project_id}_{session_id}"
        self.channel_layer = get_channel_layer()

    def publish(self, event_type: str, **payload):
        async_to_sync(self.channel_layer.group_send)(
            self.group_name,
            {
                "type": "agent_event",
                "payload": {
                    "type": event_type,
                    "project_id": self.project_id,
                    "session_id": self.session_id,
                    **payload,
                },
            },
        )