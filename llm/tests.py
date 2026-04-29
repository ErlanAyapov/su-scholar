import httpx
import asyncio
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from openai import APITimeoutError

from document.models import Document
from llm.consumers import LlmChatConsumer
from llm.models import ChatSession, ChatShareImport, ChatShareLink, Message
from main.models import Language, Project, Publication, PublicationFile, PublicationProject, PublicationReference, PublicationType, Venue

User = get_user_model()


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _FakeCompletions:
    last_messages = None

    async def create(self, **kwargs):
        _FakeCompletions.last_messages = kwargs.get("messages")
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello"))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=" world"))]),
        ]
        return _FakeStream(chunks)


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.chat = SimpleNamespace(completions=_FakeCompletions())

    async def close(self):
        return None


class _SlowFakeStream:
    def __init__(self, chunks, delay=0.05):
        self._chunks = chunks
        self._index = 0
        self._delay = delay

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        await asyncio.sleep(self._delay)
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _SlowStopCompletions:
    async def create(self, **kwargs):
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Chunk"))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=" one"))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=" two"))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=" three"))]),
        ]
        return _SlowFakeStream(chunks)


class _SlowStopClient:
    def __init__(self, *args, **kwargs):
        self.chat = SimpleNamespace(completions=_SlowStopCompletions())

    async def close(self):
        return None


class _TimeoutCompletions:
    async def create(self, **kwargs):
        raise APITimeoutError(
            request=httpx.Request("POST", "http://192.168.1.2:11434/v1/chat/completions")
        )


class _TimeoutClient:
    def __init__(self, *args, **kwargs):
        self.chat = SimpleNamespace(completions=_TimeoutCompletions())

    async def close(self):
        return None


class LlmSessionConsumerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="llm-user",
            password="testpass123",
        )
        self.language = Language.objects.create(code="en", name="English")
        self.publication_type = PublicationType.objects.create(name="Journal Article")
        self.venue = Venue.objects.create(name="Test Venue", kind="journal", character="scientific_journal")

    def _create_chat_with_messages(self, user, *, title="Shared chat sample") -> ChatSession:
        session = ChatSession.objects.create(
            user=user,
            title=title,
        )
        Message.objects.create(chat=session, body="User question", sended_from=Message.Sender.USER)
        Message.objects.create(chat=session, body="Assistant answer", sended_from=Message.Sender.BOT)
        return session

    def test_session_create_and_open(self):
        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "session_create", "title": "Research plan"})
            created = await communicator.receive_json_from()

            self.assertEqual(created.get("type"), "llm_session_created")
            session_id = created["session"]["id"]

            await communicator.send_json_to({"action": "session_open", "session_id": session_id})
            opened = await communicator.receive_json_from()

            self.assertEqual(opened.get("type"), "llm_session_opened")
            self.assertEqual(opened["session"]["id"], session_id)
            self.assertEqual(opened.get("messages"), [])

            await communicator.disconnect()

        async_to_sync(scenario)()

    def test_session_actions_require_authenticated_user(self):
        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "session_create"})
            payload = await communicator.receive_json_from()

            self.assertEqual(payload.get("type"), "llm_error")
            self.assertEqual(payload.get("message"), "Authentication required")

            await communicator.disconnect()

        async_to_sync(scenario)()

    @patch("llm.consumers.AsyncOpenAI", side_effect=lambda *args, **kwargs: _FakeClient())
    def test_ask_persists_user_and_assistant_messages(self, _mock_openai):
        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "session_create", "title": "AI chat"})
            created = await communicator.receive_json_from()
            session_id = created["session"]["id"]

            await communicator.send_json_to(
                {
                    "action": "ask",
                    "session_id": session_id,
                    "prompt": "What is the latest AI publication?",
                }
            )

            started = await communicator.receive_json_from()
            self.assertEqual(started.get("type"), "llm_started")

            deltas = []
            done_payload = None
            for _ in range(6):
                payload = await communicator.receive_json_from()
                if payload.get("type") == "llm_delta":
                    deltas.append(payload.get("delta"))
                if payload.get("type") == "llm_done":
                    done_payload = payload
                    break

            self.assertIsNotNone(done_payload)
            self.assertEqual(done_payload.get("text"), "Hello world")
            self.assertEqual("".join(deltas), "Hello world")

            await communicator.disconnect()

        async_to_sync(scenario)()

        session = ChatSession.objects.get(user=self.user)
        self.assertTrue(session.title)

        messages = list(session.messages.order_by("created", "id"))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].sended_from, Message.Sender.USER)
        self.assertEqual(messages[1].sended_from, Message.Sender.BOT)
        self.assertIn("latest AI publication", messages[0].body)
        self.assertEqual(messages[1].body, "Hello world")

    @patch("llm.consumers.AsyncOpenAI", side_effect=lambda *args, **kwargs: _FakeClient())
    def test_ask_includes_core_and_agent_context_without_preset_blob(self, _mock_openai):
        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "session_create", "title": "Context chat"})
            created = await communicator.receive_json_from()
            session_id = created["session"]["id"]

            await communicator.send_json_to(
                {
                    "action": "ask",
                    "session_id": session_id,
                    "prompt": "Test preset context usage",
                }
            )

            await communicator.receive_json_from()  # llm_started
            while True:
                payload = await communicator.receive_json_from()
                if payload.get("type") == "llm_done":
                    break

            await communicator.disconnect()

        async_to_sync(scenario)()

        sent_messages = _FakeCompletions.last_messages
        self.assertIsInstance(sent_messages, list)
        self.assertGreaterEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[0].get("role"), "system")
        self.assertIn("SU_SCIENCE_CORE_SYSTEM", sent_messages[0].get("content", ""))
        self.assertFalse(any("SU_SCIENCE_PRESET_CONTEXT_JSON" in str(item.get("content", "")) for item in sent_messages))
        self.assertTrue(any("SU_SCIENCE_AGENT_CONTEXT_JSON" in str(item.get("content", "")) for item in sent_messages))

    @patch("llm.consumers.AsyncOpenAI", side_effect=lambda *args, **kwargs: _FakeClient())
    def test_ask_includes_custom_agent_context(self, _mock_openai):
        agent_payload = {
            "agent": "su_science_query_agent",
            "schema_version": "1.0",
            "query": "iot",
            "terms": ["iot"],
            "scripts_executed": ["dataset_stats", "search_projects", "request_emulator"],
            "result_counts": {"search_projects": 1},
            "results": {
                "dataset_stats": {"publications_total": 10},
                "search_projects": [{"id": 1, "name": "IoT Research Program"}],
                "request_emulator": {"requests": []},
            },
        }

        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "session_create", "title": "Agent chat"})
            created = await communicator.receive_json_from()
            session_id = created["session"]["id"]

            await communicator.send_json_to(
                {
                    "action": "ask",
                    "session_id": session_id,
                    "prompt": "Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р Р†Р вЂљРЎвЂєР РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В°Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’Вµ Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р Р†Р вЂљРЎСљР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р РЋРІвЂћСћР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р вЂ Р Р†Р вЂљРЎвЂєР Р†Р вЂљРІР‚Сљ Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р’В Р Р†Р вЂљР’В° Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В¦Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В° Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В РІР‚в„ўР вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎв„ў IoT?",
                }
            )

            await communicator.receive_json_from()  # llm_started
            while True:
                payload = await communicator.receive_json_from()
                if payload.get("type") == "llm_done":
                    break

            await communicator.disconnect()

        with patch(
            "llm.consumers.LlmChatConsumer._build_agent_context",
            new=AsyncMock(return_value=agent_payload),
        ):
            async_to_sync(scenario)()

        sent_messages = _FakeCompletions.last_messages
        self.assertTrue(any("SU_SCIENCE_AGENT_CONTEXT_JSON" in str(item.get("content", "")) for item in sent_messages))

    def test_agent_run_returns_context(self):
        agent_payload = {
            "agent": "su_science_query_agent",
            "schema_version": "1.0",
            "query": "iot",
            "terms": ["iot"],
            "scripts_executed": ["dataset_stats", "search_publications", "request_emulator"],
            "result_counts": {"search_publications": 0},
            "results": {
                "dataset_stats": {"publications_total": 0, "researchers_total": 0, "projects_total": 0},
                "search_publications": [],
                "request_emulator": {"requests": []},
            },
        }

        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to(
                {
                    "action": "agent_run",
                    "prompt": "IoT",
                }
            )
            payload = await communicator.receive_json_from()
            self.assertEqual(payload.get("type"), "llm_agent_result")
            self.assertEqual(payload.get("context", {}).get("agent"), "su_science_query_agent")

            await communicator.disconnect()

        with patch(
            "llm.consumers.LlmChatConsumer._build_agent_context",
            new=AsyncMock(return_value=agent_payload),
        ):
            async_to_sync(scenario)()

    @patch("llm.consumers.AsyncOpenAI", side_effect=lambda *args, **kwargs: _SlowStopClient())
    def test_generation_stop_interrupts_stream(self, _mock_openai):
        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "session_create", "title": "Stop test"})
            created = await communicator.receive_json_from()
            session_id = created["session"]["id"]

            await communicator.send_json_to(
                {
                    "action": "ask",
                    "session_id": session_id,
                    "prompt": "Generate long answer",
                }
            )

            started = await communicator.receive_json_from()
            self.assertEqual(started.get("type"), "llm_started")

            while True:
                payload = await communicator.receive_json_from()
                if payload.get("type") == "llm_delta":
                    break

            await communicator.send_json_to({"action": "generation_stop"})
            done_payload = None
            for _ in range(20):
                payload = await communicator.receive_json_from()
                if payload.get("type") == "llm_done":
                    done_payload = payload
                    break

            self.assertIsNotNone(done_payload)
            self.assertTrue(done_payload.get("stopped"))
            self.assertIn("Generation stopped", done_payload.get("stop_reason", ""))

            await communicator.disconnect()

        async_to_sync(scenario)()

    @override_settings(
        LLM_CONNECT_TIMEOUT=11.5,
        LLM_READ_TIMEOUT=90.0,
        LLM_WRITE_TIMEOUT=25.0,
        LLM_POOL_TIMEOUT=7.0,
        LLM_MAX_RETRIES=4,
    )
    @patch("llm.consumers.AsyncOpenAI")
    def test_ask_uses_configured_openai_timeout_and_retries(self, mock_openai):
        mock_openai.return_value = _FakeClient()

        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "session_create", "title": "Timeout config"})
            created = await communicator.receive_json_from()
            session_id = created["session"]["id"]

            await communicator.send_json_to(
                {
                    "action": "ask",
                    "session_id": session_id,
                    "prompt": "Check timeout settings",
                }
            )

            await communicator.receive_json_from()  # llm_started
            while True:
                payload = await communicator.receive_json_from()
                if payload.get("type") == "llm_done":
                    break

            await communicator.disconnect()

        async_to_sync(scenario)()

        kwargs = mock_openai.call_args.kwargs
        timeout = kwargs["timeout"]
        self.assertAlmostEqual(timeout.connect, 11.5)
        self.assertAlmostEqual(timeout.read, 90.0)
        self.assertAlmostEqual(timeout.write, 25.0)
        self.assertAlmostEqual(timeout.pool, 7.0)
        self.assertEqual(kwargs["max_retries"], 4)

    @patch("llm.consumers.AsyncOpenAI", side_effect=lambda *args, **kwargs: _TimeoutClient())
    def test_ask_reports_llm_timeout_and_resets_state(self, _mock_openai):
        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "session_create", "title": "Timeout test"})
            created = await communicator.receive_json_from()
            session_id = created["session"]["id"]

            await communicator.send_json_to(
                {
                    "action": "ask",
                    "session_id": session_id,
                    "prompt": "Trigger timeout",
                }
            )

            started = await communicator.receive_json_from()
            self.assertEqual(started.get("type"), "llm_started")

            payload = await communicator.receive_json_from()
            self.assertEqual(payload.get("type"), "llm_error")
            self.assertIn("timed out", payload.get("message", "").lower())

            await communicator.send_json_to(
                {
                    "action": "ask",
                    "session_id": session_id,
                    "prompt": "Trigger timeout again",
                }
            )

            started_again = await communicator.receive_json_from()
            self.assertEqual(started_again.get("type"), "llm_started")

            payload_again = await communicator.receive_json_from()
            self.assertEqual(payload_again.get("type"), "llm_error")
            self.assertIn("timed out", payload_again.get("message", "").lower())

            await communicator.disconnect()

        async_to_sync(scenario)()

    def test_generation_stop_without_active_request_returns_error(self):
        async def scenario():
            communicator = WebsocketCommunicator(LlmChatConsumer.as_asgi(), "/ws/llm/")
            communicator.scope["user"] = self.user
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "generation_stop"})
            payload = await communicator.receive_json_from()

            self.assertEqual(payload.get("type"), "llm_error")
            self.assertEqual(payload.get("message"), "No active generation")

            await communicator.disconnect()

        async_to_sync(scenario)()

    def test_select_agent_scripts_by_prompt(self):
        scripts = LlmChatConsumer._select_agent_scripts("Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р Р†Р вЂљРЎвЂєР РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В°Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’Вµ Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р Р†Р вЂљРЎСљР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р РЋРІвЂћСћР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р вЂ Р Р†Р вЂљРЎвЂєР Р†Р вЂљРІР‚Сљ Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р’В Р Р†Р вЂљР’В° Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В¦Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В° Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В РІР‚в„ўР вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎв„ў IoT")

        self.assertEqual(
            scripts,
            ["dataset_stats", "search_publications", "search_projects", "request_emulator"],
        )

    def test_select_agent_scripts_for_identity_prompt(self):
        scripts = LlmChatConsumer._select_agent_scripts("Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р Р‹Р Р†Р вЂљРЎвЂќР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р вЂ Р Р†Р вЂљРЎвЂєР Р†Р вЂљРІР‚Сљ Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂє?")

        self.assertEqual(scripts, ["dataset_stats", "request_emulator"])

    def test_select_agent_scripts_for_general_prompt_is_publication_first(self):
        scripts = LlmChatConsumer._select_agent_scripts("hello")

        self.assertEqual(scripts, ["dataset_stats", "search_publications", "request_emulator"])

    def test_route_for_projects_enables_publication_fallback(self):
        route = LlmChatConsumer._route_agent_prompt("Give projects about IoT")

        self.assertEqual(route["intent"], "projects")
        self.assertIn("search_projects", route["scripts"])
        self.assertIn("search_publications", route["scripts"])
        self.assertTrue(route["fallback_publications_on_empty_projects"])

    def test_resolve_agent_prompt_uses_previous_user_query_for_short_follow_up(self):
        messages = [
            {"role": "user", "content": "Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р Р†Р вЂљРЎвЂєР РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В°Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’Вµ Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В»Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р РЋРЎвЂєР В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В°Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В»Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р’В Р Р†Р вЂљР’В° Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В  Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В±Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В»Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В°Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В iot?"},
            {"role": "assistant", "content": "Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р вЂ Р Р†Р вЂљРЎвЂєР РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС› Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р Р†Р вЂљРЎСљР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™..."},
            {"role": "user", "content": "2"},
        ]

        resolved = LlmChatConsumer._resolve_agent_prompt("2", messages)

        self.assertIn("iot", resolved.casefold())
        self.assertIn("Follow-up: 2", resolved)

    def test_resolve_agent_prompt_keeps_regular_prompt_unchanged(self):
        prompt = "Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р Р‹Р РЋРЎСџР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂєР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В°Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В¶Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р Р†Р вЂљРЎСљР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎв„ўР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В±Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В»Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В°Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В РІР‚в„ўР вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р Р†Р вЂљРЎСљР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎвЂє blockchain Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В·Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В° 2025"
        messages = [
            {"role": "user", "content": "Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р Р‹Р РЋРЎСџР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р РЋРІвЂћСћР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’ВµР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›"},
            {"role": "assistant", "content": "Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р вЂ Р В РІР‚С™Р РЋРЎС™Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р РЋРЎвЂєР В Р вЂ Р В РІР‚С™Р вЂ™Р’ВР В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р РЋРІвЂћСћР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’В°Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р вЂ™Р’В Р В Р Р‹Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р вЂ Р В РІР‚С™Р вЂ™Р’В Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В Р вЂ№Р В Р вЂ Р В РІР‚С™Р РЋРЎв„ўР В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р В РІР‚В Р В Р вЂ Р В РІР‚С™Р РЋРІР‚С”Р В Р вЂ Р В РІР‚С™Р Р†Р вЂљРЎС™Р В Р’В Р вЂ™Р’В Р В Р’В Р В РІР‚в„–Р В Р’В Р В РІР‚В Р В Р’В Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†РІР‚С›РЎС›Р В Р’В Р вЂ™Р’В Р В РІР‚в„ўР вЂ™Р’В Р В Р’В Р Р†Р вЂљРІвЂћСћР В РІР‚в„ўР вЂ™Р’Вµ"},
            {"role": "user", "content": prompt},
        ]

        resolved = LlmChatConsumer._resolve_agent_prompt(prompt, messages)

        self.assertEqual(resolved, prompt)

    def test_core_system_message_enforces_response_template(self):
        message = LlmChatConsumer._build_core_system_message()
        content = message.get("content", "")

        self.assertIn("Response format is mandatory.", content)
        self.assertIn("Always return exactly 4 short sections", content)
        self.assertIn("Do not use literal headings like", content)
        self.assertIn("Publication records first", content)

    def test_script_search_publications_returns_latest_on_no_match(self):
        pub_type = PublicationType.objects.create(name="Journal article")
        lang = Language.objects.create(code="en", name="English")
        venue = Venue.objects.create(name="Test Journal")
        older = Publication.objects.create(
            record_id="pub-fallback-1",
            pub_type=pub_type,
            title_original="Applied geochemistry baseline",
            language=lang,
            year=2022,
            venue=venue,
            abstract="Baseline study for geochemistry.",
        )
        newer = Publication.objects.create(
            record_id="pub-fallback-2",
            pub_type=pub_type,
            title_original="AI-assisted ore body prediction",
            language=lang,
            year=2025,
            venue=venue,
            abstract="Abstract with ML approach for ore body prediction.",
        )

        consumer = LlmChatConsumer()
        result = async_to_sync(consumer._script_search_publications)(["term-without-match"], 5)

        self.assertGreaterEqual(len(result), 2)
        self.assertEqual(result[0]["id"], newer.id)
        self.assertEqual(result[1]["id"], older.id)
        self.assertIn("abstract", result[0])

    def test_compact_preset_context_limits_payload(self):
        preset = {
            "meta": {"project": "SU Scholar"},
            "system_role": {"name": "SU Scholar Assistant"},
            "instructions": ["Use internal data"],
            "publications": [
                {
                    "id": idx,
                    "title": f"Publication {idx}" * 20,
                    "authors": "A, B, C",
                    "doi": "10.0000/example",
                    "first_author": "Author Example",
                    "publication_page": f"/publications/{idx}/",
                }
                for idx in range(300)
            ],
            "users": [
                {
                    "id": idx,
                    "name": f"User {idx}" * 20,
                    "profile_page": f"/employees/{idx}/",
                }
                for idx in range(300)
            ],
        }

        compact = LlmChatConsumer._compact_preset_context(preset)

        self.assertEqual(compact["stats"]["publications_total"], 300)
        self.assertEqual(compact["stats"]["users_total"], 300)
        self.assertLessEqual(
            compact["stats"]["publications_in_prompt"],
            LlmChatConsumer.PRESET_PUBLICATIONS_LIMIT,
        )
        self.assertLessEqual(
            compact["stats"]["users_in_prompt"],
            LlmChatConsumer.PRESET_USERS_LIMIT,
        )

    def test_fit_context_to_budget_produces_valid_and_bounded_json(self):
        compact = {
            "meta": {"project": "SU Scholar"},
            "system_role": {"name": "SU Scholar Assistant"},
            "instructions": ["Use internal data"],
            "stats": {
                "publications_total": 200,
                "users_total": 200,
                "publications_in_prompt": 200,
                "users_in_prompt": 200,
            },
            "publications": [
                {"id": i, "title": "T" * 120, "authors": "A" * 120, "doi": "D" * 40}
                for i in range(200)
            ],
            "users": [
                {"id": i, "name": "U" * 120, "profile_page": f"/employees/{i}/"}
                for i in range(200)
            ],
        }

        fitted = LlmChatConsumer._fit_context_to_budget(compact, 2500)
        raw_json = __import__("json").dumps(fitted, ensure_ascii=False, separators=(",", ":"))

        self.assertLessEqual(len(raw_json), 2500)
        self.assertLessEqual(fitted["stats"]["publications_in_prompt"], 200)
        self.assertLessEqual(fitted["stats"]["users_in_prompt"], 200)

    def test_project_create_html_redirects_to_detail(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("project_create"),
            data={
                "name": "AI Grant 2026",
                "description": "Drafting publications and milestones",
            },
        )

        self.assertEqual(response.status_code, 302)
        created = Project.objects.get(name="AI Grant 2026", owner=self.user)
        self.assertEqual(created.description, "Drafting publications and milestones")
        self.assertEqual(
            response.headers.get("Location"),
            reverse("project_detail_page", kwargs={"project_id": created.id}),
        )

    def test_project_create_json_returns_created_payload(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("project_create"),
            data={"name": "NLP Pilot"},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["name"], "NLP Pilot")
        self.assertEqual(payload["owner"], self.user.id)
        self.assertTrue(Project.objects.filter(id=payload["id"], owner=self.user).exists())

    def test_project_create_blocks_empty_name_for_json(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("project_create"),
            data={"name": "   "},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json().get("detail"), "Project name is required")

    def test_project_detail_hides_other_projects_when_active_selected(self):
        self.client.force_login(self.user)
        active = Project.objects.create(name="Active Project", owner=self.user)
        Project.objects.create(name="Other Project", owner=self.user)

        response = self.client.get(reverse("project_detail_page", kwargs={"project_id": active.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Project")
        self.assertNotContains(response, "Other Project")

    def test_project_detail_shows_project_publications_and_publication_files(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Research Project", owner=self.user)
        publication_type = PublicationType.objects.create(name="Article")
        language = Language.objects.create(code="en", name="English")
        venue = Venue.objects.create(name="Test Journal")
        publication = Publication.objects.create(
            pub_type=publication_type,
            title_original="Linked Publication",
            language=language,
            year=2025,
            venue=venue,
        )
        PublicationProject.objects.create(project=project, publication=publication)

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            publication_file = PublicationFile.objects.create(
                publication=publication,
                kind="pdf",
                description="Main PDF",
                file=SimpleUploadedFile("linked-publication.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
            )

            response = self.client.get(
                reverse("project_detail_page", kwargs={"project_id": project.id}),
                data={"publication_file": publication_file.id},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Публикации проекта")
        self.assertContains(response, "Linked Publication")
        self.assertContains(response, "Main PDF")
        self.assertContains(
            response,
            reverse(
                "project_publication_file_stream",
                kwargs={"project_id": project.id, "file_id": publication_file.id},
            ),
        )
        self.assertContains(response, "<iframe", html=False)

    def test_project_publication_file_config_and_stream_work_for_owner(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Publication Files Project", owner=self.user)
        publication_type = PublicationType.objects.create(name="Conference paper")
        language = Language.objects.create(code="ru", name="Russian")
        venue = Venue.objects.create(name="Conference Venue")
        publication = Publication.objects.create(
            pub_type=publication_type,
            title_original="Conference Publication",
            language=language,
            year=2024,
            venue=venue,
            created_by=self.user,
        )
        PublicationProject.objects.create(project=project, publication=publication)

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            publication_file = PublicationFile.objects.create(
                publication=publication,
                kind="other",
                description="Conference Draft",
                file=SimpleUploadedFile(
                    "conference-paper.docx",
                    b"PK\x03\x04 conference docx",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            )

            config_response = self.client.get(
                reverse(
                    "project_publication_file_onlyoffice_config",
                    kwargs={"project_id": project.id, "file_id": publication_file.id},
                )
            )
            self.assertEqual(config_response.status_code, 200)
            payload = config_response.json()
            self.assertEqual(payload["editorConfig"]["mode"], "edit")
            self.assertEqual(payload["document"]["fileType"], "docx")

            stream_response = self.client.get(
                reverse(
                    "project_publication_file_stream",
                    kwargs={"project_id": project.id, "file_id": publication_file.id},
                )
            )
            streamed_bytes = b"".join(stream_response.streaming_content)

        self.assertEqual(stream_response.status_code, 200)
        self.assertEqual(streamed_bytes, b"PK\x03\x04 conference docx")

    def test_project_publication_create_adds_publication_to_project(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="New Publication Project", owner=self.user)
        publication_type = PublicationType.objects.create(name="Journal article")
        language = Language.objects.create(code="kk", name="Kazakh")

        response = self.client.post(
            reverse("project_publication_create", kwargs={"project_id": project.id}),
            data={
                "title_original": "Project Publication Draft",
                "venue": "Automation Letters",
                "year": "2026",
                "pub_type_id": str(publication_type.id),
                "language_id": str(language.id),
            },
        )

        self.assertEqual(response.status_code, 302)
        publication = Publication.objects.get(title_original="Project Publication Draft")
        self.assertEqual(publication.venue.name, "Automation Letters")
        self.assertEqual(publication.year, 2026)
        self.assertEqual(publication.created_by, self.user)
        self.assertFalse(publication.private)
        self.assertTrue(PublicationProject.objects.filter(project=project, publication=publication).exists())
        self.assertEqual(
            response.headers.get("Location"),
            f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}",
        )

    def test_publication_model_generates_record_id_when_missing(self):
        publication_type = PublicationType.objects.create(name="Generated Record ID type")
        language = Language.objects.create(code="pl", name="Polish")
        venue = Venue.objects.create(name="Generated Record ID venue")

        publication = Publication.objects.create(
            pub_type=publication_type,
            title_original="Generated Record ID publication",
            language=language,
            year=2026,
            venue=venue,
        )

        self.assertTrue(publication.record_id)
        self.assertTrue(publication.record_id.startswith("pub-"))

    def test_project_publication_create_handles_integrity_error(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Integrity Publication Project", owner=self.user)
        publication_type = PublicationType.objects.create(name="Integrity article")
        language = Language.objects.create(code="pt", name="Portuguese")

        with patch("llm.views.Publication.objects.create", side_effect=IntegrityError("duplicate key value violates unique constraint")):
            response = self.client.post(
                reverse("project_publication_create", kwargs={"project_id": project.id}),
                data={
                    "title_original": "Integrity test publication",
                    "venue": "Integrity Venue",
                    "year": "2026",
                    "pub_type_id": str(publication_type.id),
                    "language_id": str(language.id),
                },
                HTTP_ACCEPT="application/json",
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Unable to create publication", response.json().get("detail", ""))

    def test_project_publication_link_attaches_existing_publication(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Link Publication Project", owner=self.user)
        publication_type = PublicationType.objects.create(name="Book chapter")
        language = Language.objects.create(code="de", name="German")
        venue = Venue.objects.create(name="Linked Venue")
        publication = Publication.objects.create(
            pub_type=publication_type,
            title_original="Existing Publication",
            language=language,
            year=2023,
            venue=venue,
        )

        response = self.client.post(
            reverse("project_publication_link", kwargs={"project_id": project.id}),
            data={"publication_id": str(publication.id)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(PublicationProject.objects.filter(project=project, publication=publication).exists())
        self.assertEqual(
            response.headers.get("Location"),
            f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}",
        )

    def test_project_detail_hides_private_publications_and_link_rejects_them(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Private Publication Project", owner=self.user)
        publication_type = PublicationType.objects.create(name="Private article")
        language = Language.objects.create(code="fr", name="French")
        venue = Venue.objects.create(name="Private Venue")
        publication = Publication.objects.create(
            pub_type=publication_type,
            title_original="Private Publication",
            language=language,
            year=2022,
            venue=venue,
            private=True,
        )
        PublicationProject.objects.create(project=project, publication=publication)

        response = self.client.get(reverse("project_detail_page", kwargs={"project_id": project.id}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Private Publication")

        second_project = Project.objects.create(name="Second Project", owner=self.user)
        link_response = self.client.post(
            reverse("project_publication_link", kwargs={"project_id": second_project.id}),
            data={"publication_id": str(publication.id)},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(link_response.status_code, 404)
        self.assertFalse(PublicationProject.objects.filter(project=second_project, publication=publication).exists())

    def test_project_publication_file_upload_creates_publication_file_for_owner(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Publication Upload Project", owner=self.user)
        publication_type = PublicationType.objects.create(name="Upload article")
        language = Language.objects.create(code="it", name="Italian")
        venue = Venue.objects.create(name="Upload Venue")
        publication = Publication.objects.create(
            pub_type=publication_type,
            title_original="Upload Publication",
            language=language,
            year=2021,
            venue=venue,
            created_by=self.user,
        )
        PublicationProject.objects.create(project=project, publication=publication)
        uploaded = SimpleUploadedFile("appendix.pdf", b"%PDF-1.4 appendix", content_type="application/pdf")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("project_publication_file_upload", kwargs={"project_id": project.id}),
                data={
                    "publication_id": str(publication.id),
                    "description": "Appendix PDF",
                    "kind": "pdf",
                    "file": uploaded,
                },
            )

        self.assertEqual(response.status_code, 302)
        publication_file = PublicationFile.objects.get(publication=publication)
        self.assertEqual(publication_file.description, "Appendix PDF")
        self.assertEqual(publication_file.kind, "pdf")

    def test_project_publication_file_config_is_view_only_for_non_owner(self):
        owner = User.objects.create_user(username="pub-owner", password="testpass123")
        self.client.force_login(self.user)
        project = Project.objects.create(name="Shared Publication Project", owner=self.user)
        publication_type = PublicationType.objects.create(name="Shared article")
        language = Language.objects.create(code="es", name="Spanish")
        venue = Venue.objects.create(name="Shared Venue")
        publication = Publication.objects.create(
            pub_type=publication_type,
            title_original="Shared Publication",
            language=language,
            year=2020,
            venue=venue,
            created_by=owner,
        )
        PublicationProject.objects.create(project=project, publication=publication)

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            publication_file = PublicationFile.objects.create(
                publication=publication,
                kind="other",
                description="Shared Draft",
                file=SimpleUploadedFile(
                    "shared-draft.docx",
                    b"PK\x03\x04 shared docx",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            )

            response = self.client.get(
                reverse(
                    "project_publication_file_onlyoffice_config",
                    kwargs={"project_id": project.id, "file_id": publication_file.id},
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["editorConfig"]["mode"], "view")

    def test_project_file_create_adds_document_to_project(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Files Project", owner=self.user)

        response = self.client.post(
            reverse("project_file_create", kwargs={"project_id": project.id}),
            data={"file_type": "docx", "title": "Project Draft"},
        )

        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        document = project.documents.get()
        self.assertEqual(document.title, "Project Draft")
        self.assertEqual(document.file_type, "docx")
        self.assertTrue(document.file.name.endswith(".docx"))

    def test_project_file_upload_adds_uploaded_file_to_project(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Upload Project", owner=self.user)
        uploaded = SimpleUploadedFile(
            "notes.txt",
            b"hello world",
            content_type="text/plain",
        )

        response = self.client.post(
            reverse("project_file_upload", kwargs={"project_id": project.id}),
            data={"title": "Uploaded Notes", "file": uploaded},
        )

        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        document = project.documents.get()
        self.assertEqual(document.title, "Uploaded Notes")
        self.assertEqual(document.file_type, "txt")
        self.assertTrue(document.file.name.endswith(".txt"))
        self.assertEqual(Document.objects.filter(id=document.id, user=self.user).count(), 1)

    def test_project_agent_session_returns_messages(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Agent Session Project", owner=self.user)
        session = ChatSession.objects.create(user=self.user, project=project, title="Project chat")
        Message.objects.create(chat=session, body="User prompt", sended_from=Message.Sender.USER)
        Message.objects.create(chat=session, body="Assistant reply", sended_from=Message.Sender.BOT)

        response = self.client.get(reverse("project_agent_session", kwargs={"project_id": project.id}))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["session_id"], session.id)
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][1]["role"], "assistant")

    @patch("llm.views.AgentProgressPublisher.publish")
    def test_project_agent_task_returns_reference_suggestions(self, _publish_mock):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Agent Reference Project", owner=self.user)
        target_publication = Publication.objects.create(
            pub_type=self.publication_type,
            title_original="Target Publication",
            language=self.language,
            year=2024,
            venue=self.venue,
            private=False,
            created_by=self.user,
        )
        related_publication = Publication.objects.create(
            pub_type=self.publication_type,
            title_original="Relevant Publication",
            language=self.language,
            year=2023,
            venue=self.venue,
            private=False,
            created_by=self.user,
            abstract="Relevant abstract",
        )
        PublicationProject.objects.create(project=project, publication=target_publication)

        with patch(
            "llm.views.ProjectAgentOrchestrator.handle",
            return_value={
                "mode": "text_response",
                "response_text": "Подобрал релевантные работы.",
                "search_tags": [],
                "relevant_publication_ids": [related_publication.id],
            },
        ):
            response = self.client.post(
                reverse("project_agent_task", kwargs={"project_id": project.id}),
                data=json.dumps(
                    {
                        "message": "Найди релевантные работы",
                        "publication_id": target_publication.id,
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "text_response")
        self.assertEqual(payload["assistant_message"], "Подобрал релевантные работы.")
        self.assertEqual(len(payload["reference_suggestions"]), 1)
        suggestion = payload["reference_suggestions"][0]
        self.assertEqual(suggestion["id"], related_publication.id)
        self.assertEqual(suggestion["target_publication_id"], target_publication.id)
        self.assertTrue(suggestion["can_add_reference"])
        self.assertFalse(suggestion["already_added"])

        session = ChatSession.objects.get(user=self.user, project=project)
        self.assertEqual(session.messages.count(), 2)
        self.assertEqual(session.messages.order_by("created").last().body, "Подобрал релевантные работы.")

    def test_project_publication_reference_add_creates_reference(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Reference Add Project", owner=self.user)
        target_publication = Publication.objects.create(
            pub_type=self.publication_type,
            title_original="Target Publication",
            language=self.language,
            year=2024,
            venue=self.venue,
            private=False,
            created_by=self.user,
        )
        related_publication = Publication.objects.create(
            pub_type=self.publication_type,
            title_original="Relevant Publication",
            language=self.language,
            year=2022,
            venue=self.venue,
            private=False,
            created_by=self.user,
        )
        PublicationProject.objects.create(project=project, publication=target_publication)

        response = self.client.post(
            reverse("project_publication_reference_add", kwargs={"project_id": project.id}),
            data=json.dumps(
                {
                    "target_publication_id": target_publication.id,
                    "referenced_publication_id": related_publication.id,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["status"], "created")
        self.assertTrue(
            PublicationReference.objects.filter(
                publication=target_publication,
                referenced_publication=related_publication,
                order=1,
            ).exists()
        )

        duplicate = self.client.post(
            reverse("project_publication_reference_add", kwargs={"project_id": project.id}),
            data=json.dumps(
                {
                    "target_publication_id": target_publication.id,
                    "referenced_publication_id": related_publication.id,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["status"], "already_exists")
        self.assertEqual(
            PublicationReference.objects.filter(
                publication=target_publication,
                referenced_publication=related_publication,
            ).count(),
            1,
        )

    def test_project_publication_reference_add_handles_html_form_redirect(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Reference Form Project", owner=self.user)
        target_publication = Publication.objects.create(
            pub_type=self.publication_type,
            title_original="Target Form Publication",
            language=self.language,
            year=2024,
            venue=self.venue,
            private=False,
            created_by=self.user,
        )
        related_publication = Publication.objects.create(
            pub_type=self.publication_type,
            title_original="Related Form Publication",
            language=self.language,
            year=2021,
            venue=self.venue,
            private=False,
            created_by=self.user,
        )
        PublicationProject.objects.create(project=project, publication=target_publication)

        response = self.client.post(
            reverse("project_publication_reference_add", kwargs={"project_id": project.id}),
            data={
                "target_publication_id": str(target_publication.id),
                "referenced_publication_id": str(related_publication.id),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers.get("Location"),
            f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={target_publication.id}",
        )
        self.assertTrue(
            PublicationReference.objects.filter(
                publication=target_publication,
                referenced_publication=related_publication,
            ).exists()
        )

    def test_project_detail_shows_publication_reference_list_read_only(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Reference List Project", owner=self.user)
        target_publication = Publication.objects.create(
            pub_type=self.publication_type,
            title_original="Target With References",
            language=self.language,
            year=2024,
            venue=self.venue,
            private=False,
            created_by=self.user,
        )
        referenced_publication = Publication.objects.create(
            pub_type=self.publication_type,
            title_original="Visible Referenced Publication",
            language=self.language,
            year=2022,
            venue=self.venue,
            private=False,
            created_by=self.user,
        )
        PublicationProject.objects.create(project=project, publication=target_publication)
        PublicationReference.objects.create(
            publication=target_publication,
            referenced_publication=referenced_publication,
            order=1,
        )

        response = self.client.get(
            reverse("project_detail_page", kwargs={"project_id": project.id}),
            data={"publication": str(target_publication.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Связанные ссылки")
        self.assertContains(response, "Только чтение")
        self.assertContains(response, "Visible Referenced Publication")

    @patch(
        "llm.views._plan_project_agent_edits",
        return_value={
            "assistant_reply": "Applied edits.",
            "operations": [{"op": "replace", "old": "world", "new": "team", "count": 1}],
        },
    )
    def test_project_agent_ask_updates_document_with_planner_operations(self, _plan_mock):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Agent Edit Project", owner=self.user)
        uploaded = SimpleUploadedFile("draft.txt", b"Hello world", content_type="text/plain")
        document = Document.objects.create(
            title="Draft",
            content="Hello world",
            file=uploaded,
            file_type="txt",
            user=self.user,
            version=1,
            is_deleted=False,
        )
        project.documents.add(document)

        response = self.client.post(
            reverse("project_agent_ask", kwargs={"project_id": project.id}),
            data=json.dumps(
                {
                    "message": "Fix the text",
                    "document_id": document.id,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["document_updated"])
        self.assertEqual(payload["applied_operations"], 1)
        self.assertIn("Applied edits", payload["assistant_message"])

        document.refresh_from_db()
        self.assertIn("Hello team", document.content)
        self.assertEqual(document.version, 2)
        self.assertTrue(ChatSession.objects.filter(user=self.user, project=project).exists())
        chat_session = ChatSession.objects.get(user=self.user, project=project)
        self.assertEqual(chat_session.messages.count(), 2)

    @patch(
        "llm.views._plan_project_agent_edits",
        return_value={
            "assistant_reply": "Based on the current document, here is the answer.",
            "operations": [],
            "patch": "",
        },
    )
    def test_project_agent_ask_non_edit_request_uses_planner(self, plan_mock):
        self.client.force_login(self.user)
        project = Project.objects.create(name="Agent Help Project", owner=self.user)
        uploaded = SimpleUploadedFile("note.txt", b"Initial text", content_type="text/plain")
        document = Document.objects.create(
            title="Note",
            content="Initial text",
            file=uploaded,
            file_type="txt",
            user=self.user,
            version=1,
            is_deleted=False,
        )
        project.documents.add(document)

        response = self.client.post(
            reverse("project_agent_ask", kwargs={"project_id": project.id}),
            data=json.dumps(
                {
                    "message": "What can you do for this document?",
                    "document_id": document.id,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["document_updated"])
        self.assertIn("Based on the current document", payload["assistant_message"])
        plan_mock.assert_called_once()

    def test_share_create_returns_referral_link_for_owned_session(self):
        session = self._create_chat_with_messages(self.user)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("llm_share_create"),
            data=json.dumps({"session_id": session.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("/llm/share/", payload.get("share_url", ""))
        self.assertTrue(ChatShareLink.objects.filter(session=session, created_by=self.user).exists())

    def test_share_create_blocks_foreign_session(self):
        owner = User.objects.create_user(username="owner", password="pass123")
        foreign_session = self._create_chat_with_messages(owner)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("llm_share_create"),
            data=json.dumps({"session_id": foreign_session.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_shared_chat_is_readonly_for_guests(self):
        session = self._create_chat_with_messages(self.user)
        share_link = ChatShareLink.objects.create(
            session=session,
            created_by=self.user,
        )

        response = self.client.get(reverse("llm_shared_chat", kwargs={"token": share_link.token}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Read-only mode")
        self.assertContains(response, "User question")
        self.assertContains(response, "Assistant answer")

    def test_shared_chat_imports_once_for_authenticated_viewer(self):
        session = self._create_chat_with_messages(self.user)
        share_link = ChatShareLink.objects.create(
            session=session,
            created_by=self.user,
        )
        viewer = User.objects.create_user(username="viewer", password="viewer-pass")
        self.client.force_login(viewer)

        first = self.client.get(reverse("llm_shared_chat", kwargs={"token": share_link.token}))
        self.assertEqual(first.status_code, 302)

        import_row = ChatShareImport.objects.get(share_link=share_link, user=viewer)
        self.assertIn(f"/llm/?session={import_row.session_id}", first.headers.get("Location", ""))
        self.assertEqual(import_row.session.user_id, viewer.id)
        self.assertEqual(import_row.session.messages.count(), session.messages.count())

        second = self.client.get(reverse("llm_shared_chat", kwargs={"token": share_link.token}))
        self.assertEqual(second.status_code, 302)
        self.assertEqual(ChatShareImport.objects.filter(share_link=share_link, user=viewer).count(), 1)

    def test_shared_chat_owner_redirects_without_copy(self):
        session = self._create_chat_with_messages(self.user)
        share_link = ChatShareLink.objects.create(
            session=session,
            created_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("llm_shared_chat", kwargs={"token": share_link.token}))

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/llm/?session={session.id}", response.headers.get("Location", ""))
        self.assertFalse(ChatShareImport.objects.filter(share_link=share_link, user=self.user).exists())

    def test_shared_chat_invalid_token_returns_404(self):
        response = self.client.get(reverse("llm_shared_chat", kwargs={"token": "invalid-token-value"}))
        self.assertEqual(response.status_code, 404)

