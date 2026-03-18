import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TestCase

from llm.consumers import LlmChatConsumer
from llm.models import ChatSession, Message

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


class LlmSessionConsumerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="llm-user",
            password="testpass123",
        )

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
                    "prompt": "Какие проекты есть на тему IoT?",
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
        scripts = LlmChatConsumer._select_agent_scripts("Какие проекты есть на тему IoT")

        self.assertIn("dataset_stats", scripts)
        self.assertIn("search_projects", scripts)
        self.assertIn("request_emulator", scripts)

    def test_select_agent_scripts_for_identity_prompt(self):
        scripts = LlmChatConsumer._select_agent_scripts("Ты кто?")

        self.assertEqual(scripts, ["dataset_stats", "request_emulator"])

    def test_select_agent_scripts_for_general_prompt_is_publication_first(self):
        scripts = LlmChatConsumer._select_agent_scripts("hello")

        self.assertEqual(scripts, ["dataset_stats", "search_publications", "request_emulator"])

    def test_route_for_projects_enables_publication_fallback(self):
        route = LlmChatConsumer._route_agent_prompt("Give projects about IoT")

        self.assertEqual(route["intent"], "projects")
        self.assertIn("search_projects", route["scripts"])
        self.assertTrue(route["fallback_publications_on_empty_projects"])

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
