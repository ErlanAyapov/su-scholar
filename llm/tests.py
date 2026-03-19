import httpx
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from openai import APITimeoutError

from llm.consumers import LlmChatConsumer
from llm.models import ChatSession, ChatShareImport, ChatShareLink, Message
from main.models import Language, Publication, PublicationType, Venue

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
        scripts = LlmChatConsumer._select_agent_scripts("Какие проекты есть на тему IoT")

        self.assertEqual(
            scripts,
            ["dataset_stats", "search_publications", "search_projects", "request_emulator"],
        )

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
        self.assertIn("search_publications", route["scripts"])
        self.assertTrue(route["fallback_publications_on_empty_projects"])

    def test_resolve_agent_prompt_uses_previous_user_query_for_short_follow_up(self):
        messages = [
            {"role": "user", "content": "Какие исследователи есть в области iot?"},
            {"role": "assistant", "content": "Вот список..."},
            {"role": "user", "content": "2"},
        ]

        resolved = LlmChatConsumer._resolve_agent_prompt("2", messages)

        self.assertIn("iot", resolved.casefold())
        self.assertIn("Follow-up: 2", resolved)

    def test_resolve_agent_prompt_keeps_regular_prompt_unchanged(self):
        prompt = "Покажи публикации по blockchain за 2025"
        messages = [
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Здравствуйте"},
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
