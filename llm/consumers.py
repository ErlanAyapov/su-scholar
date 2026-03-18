"""WebSocket consumer for LLM chat with persistent sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from urllib.parse import quote_plus

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth import get_user_model
from django.conf import settings
from django.db.models import Count, Max, Q
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from llm.models import ChatSession, Message
from main.models import Project, Publication
from main.utils import collect_preset_context_for_llm
from utils.openai_client import build_openai_client_kwargs

logger = logging.getLogger(__name__)
USER_MODEL = get_user_model()


class LlmChatConsumer(AsyncJsonWebsocketConsumer):
    """Realtime chat consumer with optional DB-backed history."""

    MAX_HISTORY_MESSAGES = 20
    MAX_LOADED_MESSAGES = 300
    MAX_CONTENT_LENGTH = 4000
    MAX_SESSIONS = 60
    PRESET_CONTEXT_MAX_CHARS = 16000
    PRESET_CONTEXT_MARKER = "SU_SCIENCE_PRESET_CONTEXT_JSON"
    PRESET_PUBLICATIONS_LIMIT = 80
    PRESET_USERS_LIMIT = 80
    CORE_SYSTEM_MARKER = "SU_SCIENCE_CORE_SYSTEM"
    AGENT_CONTEXT_MARKER = "SU_SCIENCE_AGENT_CONTEXT_JSON"
    AGENT_CONTEXT_MAX_CHARS = 8000
    AGENT_RESULT_LIMIT = 8
    AGENT_SUPPORTED_SCRIPTS = (
        "dataset_stats",
        "search_publications",
        "search_researchers",
        "search_projects",
        "request_emulator",
    )
    AGENT_STOP_WORDS = {
        "ты",
        "вы",
        "я",
        "мы",
        "он",
        "она",
        "они",
        "и",
        "в",
        "во",
        "на",
        "по",
        "для",
        "о",
        "об",
        "про",
        "к",
        "ко",
        "как",
        "что",
        "где",
        "кто",
        "есть",
        "дай",
        "дайте",
        "можно",
        "please",
        "the",
        "a",
        "an",
        "of",
        "to",
    }
    AGENT_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁёІіҢңҒғҚқӨөҰұҮүӘәҺһ][0-9A-Za-zА-Яа-яЁёІіҢңҒғҚқӨөҰұҮүӘәҺһ_-]{1,}")
    AGENT_PUBLICATION_KEYWORDS = (
        "публикац",
        "стать",
        "paper",
        "article",
        "doi",
        "journal",
        "work",
        "работ",
    )
    AGENT_RESEARCHER_KEYWORDS = (
        "автор",
        "исследоват",
        "researcher",
        "сотруд",
        "employee",
        "profile",
        "учен",
    )
    AGENT_PROJECT_KEYWORDS = (
        "проект",
        "project",
        "grant",
        "грант",
        "funding",
        "финанс",
    )
    AGENT_META_KEYWORDS = (
        "кто ты",
        "ты кто",
        "что ты умеешь",
        "что умеешь",
        "who are you",
        "what can you do",
        "help",
        "помощь",
    )

    @staticmethod
    def _utc_iso() -> str:
        return datetime.utcnow().isoformat() + "Z"

    @staticmethod
    def _parse_session_id(raw_value) -> int | None:
        try:
            session_id = int(raw_value)
        except (TypeError, ValueError):
            return None
        if session_id <= 0:
            return None
        return session_id

    @staticmethod
    def _trim_text(raw_value, *, max_length: int) -> str:
        return str(raw_value or "").strip()[:max_length]

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

    @staticmethod
    def _serialize_session(session: ChatSession, *, last_message_at=None) -> dict:
        last_value = last_message_at or getattr(session, "last_message_created", None) or session.updated or session.created
        return {
            "id": session.id,
            "title": session.title,
            "created": session.created.isoformat(),
            "updated": session.updated.isoformat(),
            "last_message_at": last_value.isoformat() if last_value else None,
        }

    @staticmethod
    def _serialize_message(message: Message) -> dict:
        return {
            "id": message.id,
            "role": message.role,
            "content": message.body,
            "created": message.created.isoformat(),
        }

    @property
    def _user(self):
        return self.scope.get("user")

    @property
    def _is_authenticated(self) -> bool:
        user = self._user
        return bool(user and user.is_authenticated)

    async def connect(self):
        self.is_busy = False
        self._generation_task: asyncio.Task | None = None
        self._generation_stop_requested = False
        self._generation_stop_reason = ""
        await self.accept()
        await self.send_json(
            {
                "type": "llm_ready",
                "timestamp": self._utc_iso(),
                "authenticated": self._is_authenticated,
            }
        )

    async def disconnect(self, code):
        await self._stop_active_generation(notify_client=False, reason="Connection closed")

    async def _safe_send_json(self, payload: dict):
        try:
            await self.send_json(payload)
        except Exception:
            logger.debug("Failed to send websocket payload", exc_info=True)

    async def receive_json(self, content, **kwargs):
        payload = content or {}
        action = (payload.get("action") or "").strip().lower()

        if action == "ping":
            await self.send_json(
                {
                    "type": "pong",
                    "timestamp": self._utc_iso(),
                }
            )
            return

        if action == "session_list":
            await self._handle_session_list(payload)
            return

        if action == "session_create":
            await self._handle_session_create(payload)
            return

        if action == "session_open":
            await self._handle_session_open(payload)
            return

        if action == "session_delete":
            await self._handle_session_delete(payload)
            return

        if action == "session_rename":
            await self._handle_session_rename(payload)
            return

        if action == "ask":
            await self._handle_ask(payload)
            return

        if action == "generation_stop":
            await self._handle_generation_stop(payload)
            return

        if action == "agent_run":
            await self._handle_agent_run(payload)
            return

        await self.send_json(
            {
                "type": "llm_error",
                "message": "Unsupported action",
            }
        )

    async def _send_auth_required(self):
        await self.send_json(
            {
                "type": "llm_error",
                "message": "Authentication required",
            }
        )

    async def _handle_session_list(self, payload):
        if not self._is_authenticated:
            await self._send_auth_required()
            return

        sessions = await self._list_user_sessions(self._user)
        requested_active_id = self._parse_session_id(payload.get("active_session_id"))
        existing_ids = {item["id"] for item in sessions}

        if requested_active_id in existing_ids:
            active_session_id = requested_active_id
        else:
            active_session_id = sessions[0]["id"] if sessions else None

        await self.send_json(
            {
                "type": "llm_sessions",
                "sessions": sessions,
                "active_session_id": active_session_id,
                "timestamp": self._utc_iso(),
            }
        )

    async def _handle_session_create(self, payload):
        if not self._is_authenticated:
            await self._send_auth_required()
            return

        title = self._trim_text(payload.get("title"), max_length=500)
        session_payload = await self._create_session(self._user, title)
        await self.send_json(
            {
                "type": "llm_session_created",
                "session": session_payload,
                "timestamp": self._utc_iso(),
            }
        )

    async def _handle_session_open(self, payload):
        if not self._is_authenticated:
            await self._send_auth_required()
            return

        session_id = self._parse_session_id(payload.get("session_id"))
        if not session_id:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Session id is invalid",
                }
            )
            return

        loaded = await self._load_session(self._user, session_id)
        if not loaded:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Chat session not found",
                }
            )
            return

        await self.send_json(
            {
                "type": "llm_session_opened",
                "session": loaded["session"],
                "messages": loaded["messages"],
                "timestamp": self._utc_iso(),
            }
        )

    async def _handle_session_delete(self, payload):
        if not self._is_authenticated:
            await self._send_auth_required()
            return

        session_id = self._parse_session_id(payload.get("session_id"))
        if not session_id:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Session id is invalid",
                }
            )
            return

        deleted = await self._delete_session(self._user, session_id)
        if not deleted:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Chat session not found",
                }
            )
            return

        await self.send_json(
            {
                "type": "llm_session_deleted",
                "session_id": session_id,
                "timestamp": self._utc_iso(),
            }
        )

    async def _handle_session_rename(self, payload):
        if not self._is_authenticated:
            await self._send_auth_required()
            return

        session_id = self._parse_session_id(payload.get("session_id"))
        if not session_id:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Session id is invalid",
                }
            )
            return

        title = self._trim_text(payload.get("title"), max_length=500)
        if not title:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Title is empty",
                }
            )
            return

        session_payload = await self._rename_session(self._user, session_id, title)
        if not session_payload:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Chat session not found",
                }
            )
            return

        await self.send_json(
            {
                "type": "llm_session_renamed",
                "session": session_payload,
                "timestamp": self._utc_iso(),
            }
        )

    async def _handle_ask(self, payload):
        prompt = self._trim_text(payload.get("prompt"), max_length=self.MAX_CONTENT_LENGTH)
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

        session_id = self._parse_session_id(payload.get("session_id"))
        active_session_payload = None

        if session_id:
            if not self._is_authenticated:
                await self._send_auth_required()
                return
            active_session_payload = await self._append_user_message(self._user, session_id, prompt)
            if not active_session_payload:
                await self.send_json(
                    {
                        "type": "llm_error",
                        "message": "Chat session not found",
                    }
                )
                return
            messages = await self._context_messages_from_session(self._user, session_id)
        else:
            messages = self._prepare_messages(payload.get("messages"), prompt)

        messages = self._inject_core_system_message(messages)
        messages = await self._inject_agent_context(messages, prompt)
        if not messages:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Prompt is empty",
                }
            )
            return

        model = (payload.get("model") or getattr(settings, "LLM_MODEL", "gpt-oss:20b")).strip() or "gpt-oss:20b"
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
        self._generation_stop_requested = False
        self._generation_stop_reason = ""
        await self.send_json(
            {
                "type": "llm_started",
                "model": model,
                "session_id": session_id,
                "session": active_session_payload,
                "timestamp": self._utc_iso(),
            }
        )

        if self._generation_task and not self._generation_task.done():
            await self._stop_active_generation(notify_client=False, reason="Superseded by new request")

        self._generation_task = asyncio.create_task(
            self._run_generation(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                session_id=session_id,
                active_session_payload=active_session_payload,
            )
        )

    async def _handle_generation_stop(self, payload):
        if not self.is_busy:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "No active generation",
                }
            )
            return

        await self._stop_active_generation(notify_client=True, reason="Generation stopped by user")

    async def _stop_active_generation(self, *, notify_client: bool, reason: str):
        self._generation_stop_requested = True
        self._generation_stop_reason = reason or "Generation stopped"
        task = self._generation_task
        if task and not task.done():
            task.cancel()
        if notify_client:
            await self._safe_send_json(
                {
                    "type": "llm_stopping",
                    "reason": self._generation_stop_reason,
                    "timestamp": self._utc_iso(),
                }
            )

    def _reset_generation_state(self):
        self.is_busy = False
        self._generation_task = None
        self._generation_stop_requested = False
        self._generation_stop_reason = ""

    async def _run_generation(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        session_id: int | None,
        active_session_payload: dict | None,
    ):
        client: AsyncOpenAI | None = None
        collected_chunks: list[str] = []
        stopped = False
        stop_reason = ""

        try:
            client = AsyncOpenAI(base_url=base_url, api_key=api_key, **build_openai_client_kwargs())
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
            )
            async for chunk in stream:
                if self._generation_stop_requested:
                    stopped = True
                    stop_reason = self._generation_stop_reason or "Generation stopped"
                    break

                choice = chunk.choices[0] if chunk.choices else None
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None)
                if not text:
                    continue
                collected_chunks.append(text)
                await self._safe_send_json(
                    {
                        "type": "llm_delta",
                        "delta": text,
                    }
                )
        except asyncio.CancelledError:
            stopped = True
            stop_reason = self._generation_stop_reason or "Generation stopped"
        except APITimeoutError as exc:
            logger.warning(
                "LLM websocket stream timed out: base_url=%s model=%s error=%s",
                base_url,
                model,
                exc,
            )
            await self._safe_send_json(
                {
                    "type": "llm_error",
                    "message": "LLM backend timed out. Check that the configured endpoint is reachable.",
                }
            )
            self._reset_generation_state()
            return
        except APIConnectionError as exc:
            logger.warning(
                "LLM websocket stream connection failed: base_url=%s model=%s error=%s",
                base_url,
                model,
                exc,
            )
            await self._safe_send_json(
                {
                    "type": "llm_error",
                    "message": "LLM backend connection failed. Check the configured endpoint and network access.",
                }
            )
            self._reset_generation_state()
            return
        except Exception as exc:
            logger.exception("LLM websocket stream failed: %s", exc)
            await self._safe_send_json(
                {
                    "type": "llm_error",
                    "message": str(exc),
                }
            )
            self._reset_generation_state()
            return
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    logger.debug("Failed to close OpenAI client", exc_info=True)

        full_text = "".join(collected_chunks).strip()
        if session_id and full_text:
            active_session_payload = await self._append_assistant_message(self._user, session_id, full_text)

        done_payload = {
            "type": "llm_done",
            "model": model,
            "text": full_text,
            "session_id": session_id,
            "session": active_session_payload,
            "timestamp": self._utc_iso(),
        }
        if stopped:
            done_payload["stopped"] = True
            done_payload["stop_reason"] = stop_reason

        await self._safe_send_json(done_payload)
        self._reset_generation_state()

    async def _handle_agent_run(self, payload):
        if not self._is_authenticated:
            await self._send_auth_required()
            return

        prompt = self._trim_text(
            payload.get("prompt") or payload.get("query"),
            max_length=self.MAX_CONTENT_LENGTH,
        )
        raw_scripts = payload.get("scripts")
        if isinstance(raw_scripts, str):
            forced_scripts = [raw_scripts]
        elif isinstance(raw_scripts, list):
            forced_scripts = [item for item in raw_scripts if isinstance(item, str)]
        else:
            forced_scripts = []

        if not prompt and not forced_scripts:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Prompt or scripts are required",
                }
            )
            return

        agent_context = await self._build_agent_context(
            prompt,
            forced_scripts=forced_scripts,
        )
        if not agent_context:
            await self.send_json(
                {
                    "type": "llm_error",
                    "message": "Failed to run agent scripts",
                }
            )
            return

        await self.send_json(
            {
                "type": "llm_agent_result",
                "context": agent_context,
                "timestamp": self._utc_iso(),
            }
        )

    @database_sync_to_async
    def _list_user_sessions(self, user):
        queryset = (
            ChatSession.objects.filter(user=user)
            .annotate(last_message_created=Max("messages__created"))
            .order_by("-last_message_created", "-updated", "-id")
        )
        sessions = []
        for session in queryset[: self.MAX_SESSIONS]:
            sessions.append(self._serialize_session(session))
        return sessions

    @database_sync_to_async
    def _create_session(self, user, title: str):
        normalized_title = title or ChatSession.DEFAULT_TITLE
        session = ChatSession.objects.create(
            user=user,
            title=normalized_title,
        )
        return self._serialize_session(session)

    @database_sync_to_async
    def _load_session(self, user, session_id: int):
        session = ChatSession.objects.filter(user=user, id=session_id).first()
        if not session:
            return None

        messages_qs = session.messages.order_by("created", "id")[: self.MAX_LOADED_MESSAGES]
        messages = [self._serialize_message(message) for message in messages_qs]

        return {
            "session": self._serialize_session(session),
            "messages": messages,
        }

    @database_sync_to_async
    def _delete_session(self, user, session_id: int):
        deleted_count, _ = ChatSession.objects.filter(user=user, id=session_id).delete()
        return bool(deleted_count)

    @database_sync_to_async
    def _rename_session(self, user, session_id: int, title: str):
        session = ChatSession.objects.filter(user=user, id=session_id).first()
        if not session:
            return None

        session.title = title
        session.save()
        return self._serialize_session(session)

    @database_sync_to_async
    def _append_user_message(self, user, session_id: int, prompt: str):
        session = ChatSession.objects.filter(user=user, id=session_id).first()
        if not session:
            return None

        Message.objects.create(
            chat=session,
            body=prompt,
            sended_from=Message.Sender.USER,
        )

        if session.title == ChatSession.DEFAULT_TITLE:
            session.title = ChatSession.title_from_prompt(prompt)
        session.save()
        return self._serialize_session(session)

    @database_sync_to_async
    def _append_assistant_message(self, user, session_id: int, text: str):
        session = ChatSession.objects.filter(user=user, id=session_id).first()
        if not session:
            return None

        Message.objects.create(
            chat=session,
            body=text,
            sended_from=Message.Sender.BOT,
        )
        session.save()
        return self._serialize_session(session)

    @database_sync_to_async
    def _context_messages_from_session(self, user, session_id: int):
        session = ChatSession.objects.filter(user=user, id=session_id).first()
        if not session:
            return []

        history = list(session.messages.order_by("-created", "-id")[: self.MAX_HISTORY_MESSAGES])
        history.reverse()
        return [
            {
                "role": message.role,
                "content": message.body[: self.MAX_CONTENT_LENGTH],
            }
            for message in history
        ]

    async def _inject_preset_context(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if not messages:
            return messages

        if self._has_preset_system_message(messages):
            return messages

        preset_context = await self._collect_preset_context()
        system_message = self._build_preset_system_message(preset_context)
        if not system_message:
            return messages
        return [system_message, *messages]

    async def _inject_agent_context(self, messages: list[dict[str, str]], prompt: str) -> list[dict[str, str]]:
        if not messages:
            return messages
        if self._has_agent_system_message(messages):
            return messages

        agent_context = await self._build_agent_context(prompt)
        agent_message = self._build_agent_system_message(agent_context)
        if not agent_message:
            return messages

        insert_at = 1 if self._has_core_system_message(messages) else 0
        return [*messages[:insert_at], agent_message, *messages[insert_at:]]

    @classmethod
    def _inject_core_system_message(cls, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if not messages:
            return messages
        if cls._has_core_system_message(messages):
            return messages
        return [cls._build_core_system_message(), *messages]

    @classmethod
    def _has_core_system_message(cls, messages: list[dict[str, str]]) -> bool:
        if not messages:
            return False
        first = messages[0]
        if not isinstance(first, dict):
            return False
        if str(first.get("role") or "").strip().lower() != "system":
            return False
        content = str(first.get("content") or "")
        return cls.CORE_SYSTEM_MARKER in content

    @classmethod
    def _build_core_system_message(cls) -> dict[str, str]:
        content = (
            f"{cls.CORE_SYSTEM_MARKER}\n"
            "You are SU Scholar Assistant for Satbayev University.\n"
            "Never identify yourself as ChatGPT, OpenAI model, or third-party assistant.\n"
            "Use AGENT context as the primary data source.\n"
            "If records are missing, explicitly say so and do not hallucinate.\n"
            "Answer in the same language as the user."
        )
        return {
            "role": "system",
            "content": content,
        }

    @classmethod
    def _has_preset_system_message(cls, messages: list[dict[str, str]]) -> bool:
        if not messages:
            return False
        first = messages[0]
        if not isinstance(first, dict):
            return False
        if str(first.get("role") or "").strip().lower() != "system":
            return False
        content = str(first.get("content") or "")
        return cls.PRESET_CONTEXT_MARKER in content

    @classmethod
    def _has_agent_system_message(cls, messages: list[dict[str, str]]) -> bool:
        if not messages:
            return False
        for item in messages[:3]:
            if not isinstance(item, dict):
                continue
            if str(item.get("role") or "").strip().lower() != "system":
                continue
            content = str(item.get("content") or "")
            if cls.AGENT_CONTEXT_MARKER in content:
                return True
        return False

    @classmethod
    def _build_preset_system_message(cls, preset_context: dict) -> dict[str, str] | None:
        if not isinstance(preset_context, dict):
            return None

        compact_context = cls._compact_preset_context(preset_context)
        compact_context = cls._fit_context_to_budget(compact_context, cls.PRESET_CONTEXT_MAX_CHARS)
        raw_json = json.dumps(compact_context, ensure_ascii=False, separators=(",", ":"))

        content = (
            "Ты — SU Scholar Assistant для системы SU Scholar (Satbayev University).\n"
            "Никогда не представляйся ChatGPT, OpenAI-моделью или сторонним ассистентом.\n"
            "Используй JSON-контекст ниже как основной источник данных.\n"
            "Если данных в контексте не хватает — прямо скажи об этом, не выдумывай.\n"
            "Отвечай на языке пользователя.\n\n"
            f"{cls.PRESET_CONTEXT_MARKER}:\n{raw_json}"
        )
        return {
            "role": "system",
            "content": content,
        }

    @classmethod
    def _compact_preset_context(cls, preset_context: dict) -> dict:
        publications = list(preset_context.get("publications") or [])
        users = list(preset_context.get("users") or [])

        compact_publications = []
        for item in publications[: cls.PRESET_PUBLICATIONS_LIMIT]:
            compact_publications.append(
                {
                    "id": item.get("id"),
                    "title": str(item.get("title") or "")[:220],
                    "authors": str(item.get("authors") or "")[:220],
                    "doi": str(item.get("doi") or "")[:120],
                    "first_author": str(item.get("first_author") or "")[:160],
                    "publication_page": str(item.get("publication_page") or "")[:120],
                }
            )

        compact_users = []
        for item in users[: cls.PRESET_USERS_LIMIT]:
            compact_users.append(
                {
                    "id": item.get("id"),
                    "name": str(item.get("name") or "")[:180],
                    "profile_page": str(item.get("profile_page") or "")[:120],
                }
            )

        return {
            "meta": preset_context.get("meta") or {},
            "system_role": preset_context.get("system_role") or {},
            "instructions": list(preset_context.get("instructions") or []),
            "stats": {
                "publications_total": len(publications),
                "users_total": len(users),
                "publications_in_prompt": len(compact_publications),
                "users_in_prompt": len(compact_users),
            },
            "publications": compact_publications,
            "users": compact_users,
        }

    @classmethod
    def _fit_context_to_budget(cls, compact_context: dict, max_chars: int) -> dict:
        context = dict(compact_context or {})
        publications = list(context.get("publications") or [])
        users = list(context.get("users") or [])

        def dump_len(pub_items, user_items) -> tuple[str, int]:
            payload = {
                **context,
                "publications": pub_items,
                "users": user_items,
                "stats": {
                    **(context.get("stats") or {}),
                    "publications_in_prompt": len(pub_items),
                    "users_in_prompt": len(user_items),
                },
            }
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            return raw, len(raw)

        raw_json, current_len = dump_len(publications, users)
        while current_len > max_chars and (publications or users):
            if len(publications) >= len(users) and publications:
                publications = publications[:-5]
            elif users:
                users = users[:-5]
            raw_json, current_len = dump_len(publications, users)

        context["publications"] = publications
        context["users"] = users
        context["stats"] = {
            **(context.get("stats") or {}),
            "publications_in_prompt": len(publications),
            "users_in_prompt": len(users),
            "context_truncated": current_len > max_chars,
        }
        return context

    @database_sync_to_async
    def _collect_preset_context(self) -> dict:
        try:
            return collect_preset_context_for_llm()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to collect preset LLM context: %s", exc)
            return {"publications": [], "users": []}

    async def _build_agent_context(self, prompt: str, *, forced_scripts: list[str] | None = None) -> dict | None:
        normalized_prompt = self._trim_text(prompt, max_length=self.MAX_CONTENT_LENGTH)
        terms = self._extract_query_terms(normalized_prompt)
        route = self._route_agent_prompt(normalized_prompt, forced_scripts=forced_scripts)
        scripts = list(route.get("scripts") or [])
        if not scripts:
            return None

        results: dict[str, object] = {}
        if "dataset_stats" in scripts:
            results["dataset_stats"] = await self._script_dataset_stats()
        if "search_publications" in scripts:
            results["search_publications"] = await self._script_search_publications(terms, self.AGENT_RESULT_LIMIT)
        if "search_researchers" in scripts:
            results["search_researchers"] = await self._script_search_researchers(terms, self.AGENT_RESULT_LIMIT)
        if "search_projects" in scripts:
            results["search_projects"] = await self._script_search_projects(terms, self.AGENT_RESULT_LIMIT)
            if route.get("fallback_publications_on_empty_projects") and not results.get("search_projects"):
                results["project_related_publications"] = await self._script_search_publications(
                    terms,
                    self.AGENT_RESULT_LIMIT,
                )
                route["fallback_used"] = bool(results["project_related_publications"])
        if "request_emulator" in scripts:
            results["request_emulator"] = self._script_request_emulator(normalized_prompt, terms)

        context = {
            "agent": "su_science_query_agent",
            "schema_version": "1.0",
            "query": normalized_prompt,
            "terms": terms,
            "router": route,
            "scripts_executed": scripts,
            "result_counts": self._agent_result_counts(results),
            "results": results,
        }
        return self._fit_agent_context_to_budget(context, self.AGENT_CONTEXT_MAX_CHARS)

    @classmethod
    def _route_agent_prompt(cls, prompt: str, *, forced_scripts: list[str] | None = None) -> dict:
        scripts = cls._select_agent_scripts(prompt, forced_scripts=forced_scripts)
        prompt_cf = str(prompt or "").casefold()
        wants_projects = any(keyword in prompt_cf for keyword in cls.AGENT_PROJECT_KEYWORDS)
        wants_publications = any(keyword in prompt_cf for keyword in cls.AGENT_PUBLICATION_KEYWORDS)
        wants_researchers = any(keyword in prompt_cf for keyword in cls.AGENT_RESEARCHER_KEYWORDS)
        is_meta = any(keyword in prompt_cf for keyword in cls.AGENT_META_KEYWORDS)

        intent = "general"
        if is_meta:
            intent = "meta"
        elif wants_projects and wants_publications:
            intent = "projects_and_publications"
        elif wants_projects:
            intent = "projects"
        elif wants_publications:
            intent = "publications"
        elif wants_researchers:
            intent = "researchers"

        return {
            "intent": intent,
            "scripts": scripts,
            "fallback_publications_on_empty_projects": bool(wants_projects and "search_publications" not in scripts),
            "fallback_used": False,
        }

    @classmethod
    def _extract_query_terms(cls, prompt: str) -> list[str]:
        if not prompt:
            return []
        tokens = cls.AGENT_TOKEN_RE.findall(prompt.casefold())
        terms: list[str] = []
        for token in tokens:
            if token in cls.AGENT_STOP_WORDS:
                continue
            if len(token) < 2:
                continue
            if token.isdigit():
                continue
            if token not in terms:
                terms.append(token)
            if len(terms) >= 8:
                break
        return terms

    @classmethod
    def _select_agent_scripts(cls, prompt: str, *, forced_scripts: list[str] | None = None) -> list[str]:
        if forced_scripts:
            normalized = []
            for item in forced_scripts:
                name = str(item or "").strip().lower()
                if name in cls.AGENT_SUPPORTED_SCRIPTS and name not in normalized:
                    normalized.append(name)
            return normalized

        prompt_cf = str(prompt or "").casefold()
        if any(keyword in prompt_cf for keyword in cls.AGENT_META_KEYWORDS):
            return ["dataset_stats", "request_emulator"]
        scripts = ["dataset_stats"]
        wants_any = False

        if any(keyword in prompt_cf for keyword in cls.AGENT_PUBLICATION_KEYWORDS):
            scripts.append("search_publications")
            wants_any = True
        if any(keyword in prompt_cf for keyword in cls.AGENT_RESEARCHER_KEYWORDS):
            scripts.append("search_researchers")
            wants_any = True
        if any(keyword in prompt_cf for keyword in cls.AGENT_PROJECT_KEYWORDS):
            scripts.append("search_projects")
            wants_any = True

        if not wants_any:
            scripts.append("search_publications")

        scripts.append("request_emulator")
        normalized: list[str] = []
        for name in scripts:
            if name in cls.AGENT_SUPPORTED_SCRIPTS and name not in normalized:
                normalized.append(name)
        return normalized

    @staticmethod
    def _agent_result_counts(results: dict[str, object]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name, value in (results or {}).items():
            if isinstance(value, list):
                counts[name] = len(value)
            elif isinstance(value, dict):
                counts[name] = len(value)
            elif value is None:
                counts[name] = 0
            else:
                counts[name] = 1
        return counts

    @classmethod
    def _fit_agent_context_to_budget(cls, context: dict, max_chars: int) -> dict:
        payload = dict(context or {})
        results = dict(payload.get("results") or {})
        payload["results"] = results

        def dump_len(value: dict) -> int:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

        current_len = dump_len(payload)
        trim_order = ["project_related_publications", "search_publications", "search_researchers", "search_projects"]
        while current_len > max_chars:
            trimmed = False
            for key in trim_order:
                items = results.get(key)
                if isinstance(items, list) and items:
                    results[key] = items[:-1]
                    trimmed = True
                    break
            if not trimmed:
                break
            payload["result_counts"] = cls._agent_result_counts(results)
            current_len = dump_len(payload)

        payload["result_counts"] = cls._agent_result_counts(results)
        payload["context_truncated"] = current_len > max_chars
        return payload

    @classmethod
    def _build_agent_system_message(cls, agent_context: dict) -> dict[str, str] | None:
        if not isinstance(agent_context, dict):
            return None
        if not agent_context.get("scripts_executed"):
            return None

        compact_context = cls._fit_agent_context_to_budget(agent_context, cls.AGENT_CONTEXT_MAX_CHARS)
        raw_json = json.dumps(compact_context, ensure_ascii=False, separators=(",", ":"))
        content = (
            "Agent mode is enabled for SU Scholar Assistant.\n"
            "The JSON below is generated by selective DB queries for the current question.\n"
            "Use these results as the freshest source of truth.\n"
            "If search_projects is empty but project_related_publications has items, explain that projects are not registered but related works exist.\n"
            "If result lists are empty, explicitly say that no records were found.\n"
            "If records exist, prioritize exact names, years, and links from the tool data.\n\n"
            f"{cls.AGENT_CONTEXT_MARKER}:\n{raw_json}"
        )
        return {
            "role": "system",
            "content": content,
        }

    @staticmethod
    def _apply_terms_with_fallback(queryset, terms: list[str], term_filter_factory):
        if not terms:
            return queryset.none()

        strict_filter = Q()
        for term in terms:
            strict_filter &= term_filter_factory(term)

        strict_qs = queryset.filter(strict_filter).distinct()
        if strict_qs.exists():
            return strict_qs

        any_filter = Q()
        for term in terms:
            any_filter |= term_filter_factory(term)
        return queryset.filter(any_filter).distinct()

    @staticmethod
    def _publication_term_filter(term: str) -> Q:
        return (
            Q(title_original__icontains=term)
            | Q(abstract__icontains=term)
            | Q(keywords__icontains=term)
            | Q(doi__icontains=term)
            | Q(authors__full_name__icontains=term)
            | Q(created_by__first_name__icontains=term)
            | Q(created_by__last_name__icontains=term)
            | Q(created_by__father_name__icontains=term)
            | Q(venue__name__icontains=term)
            | Q(record_id__icontains=term)
        )

    @staticmethod
    def _researcher_term_filter(term: str) -> Q:
        return (
            Q(first_name__icontains=term)
            | Q(last_name__icontains=term)
            | Q(father_name__icontains=term)
            | Q(username__icontains=term)
            | Q(email__icontains=term)
            | Q(orc_id__icontains=term)
            | Q(scopus_id__icontains=term)
            | Q(wos_id__icontains=term)
            | Q(department__name__icontains=term)
            | Q(department__institute__name__icontains=term)
            | Q(department__institute__university__name__icontains=term)
        )

    @staticmethod
    def _project_term_filter(term: str) -> Q:
        return (
            Q(name__icontains=term)
            | Q(contract_number__icontains=term)
            | Q(funding_source__icontains=term)
            | Q(publicationproject__publication__title_original__icontains=term)
            | Q(publicationproject__publication__authors__full_name__icontains=term)
        )

    @database_sync_to_async
    def _script_dataset_stats(self) -> dict:
        return {
            "publications_total": Publication.objects.count(),
            "researchers_total": USER_MODEL.objects.count(),
            "projects_total": Project.objects.count(),
        }

    @database_sync_to_async
    def _script_search_publications(self, terms: list[str], limit: int) -> list[dict]:
        queryset = Publication.objects.select_related("created_by", "venue").prefetch_related("authors")
        queryset = self._apply_terms_with_fallback(queryset, terms, self._publication_term_filter).order_by("-year", "-id")

        items: list[dict] = []
        for publication in queryset[:limit]:
            title = str(publication.title_original or "").strip()
            author_names = [
                str(author.full_name or "").strip()
                for author in publication.authors.all()
                if str(author.full_name or "").strip()
            ][:5]
            first_author = ""
            if publication.created_by:
                first_author = publication.created_by.full_name().strip()
            items.append(
                {
                    "id": publication.id,
                    "title": title[:240],
                    "year": publication.year,
                    "doi": str(publication.doi or "")[:120],
                    "venue": str(publication.venue.name if publication.venue else "")[:180],
                    "authors": author_names,
                    "first_author": first_author[:180],
                    "publication_page": f"/publications/{publication.id}/",
                }
            )
        return items

    @database_sync_to_async
    def _script_search_researchers(self, terms: list[str], limit: int) -> list[dict]:
        queryset = USER_MODEL.objects.select_related("department__institute__university")
        queryset = self._apply_terms_with_fallback(queryset, terms, self._researcher_term_filter).order_by(
            "last_name",
            "first_name",
            "id",
        )

        items: list[dict] = []
        for user in queryset[:limit]:
            full_name = " ".join(
                [
                    str(user.last_name or "").strip(),
                    str(user.first_name or "").strip(),
                    str(user.father_name or "").strip(),
                ]
            ).strip()
            department = user.department.name if user.department else ""
            institute = user.department.institute.name if user.department and user.department.institute else ""
            university = (
                user.department.institute.university.name
                if user.department and user.department.institute and user.department.institute.university
                else ""
            )
            items.append(
                {
                    "id": user.id,
                    "name": (full_name or user.username)[:180],
                    "username": str(user.username or "")[:120],
                    "department": str(department)[:160],
                    "institute": str(institute)[:160],
                    "university": str(university)[:160],
                    "profile_page": f"/employees/{user.id}/",
                }
            )
        return items

    @database_sync_to_async
    def _script_search_projects(self, terms: list[str], limit: int) -> list[dict]:
        queryset = Project.objects.annotate(publications_total=Count("publicationproject", distinct=True))
        queryset = self._apply_terms_with_fallback(queryset, terms, self._project_term_filter).order_by("name", "id")

        items: list[dict] = []
        for project in queryset[:limit]:
            items.append(
                {
                    "id": project.id,
                    "name": str(project.name or "")[:240],
                    "project_type": str(project.project_type or "")[:40],
                    "contract_number": str(project.contract_number or "")[:120],
                    "funding_source": str(project.funding_source or "")[:180],
                    "publications_total": int(getattr(project, "publications_total", 0) or 0),
                }
            )
        return items

    @classmethod
    def _script_request_emulator(cls, prompt: str, terms: list[str]) -> dict:
        query_text = " ".join(terms[:4]).strip()
        if not query_text:
            query_text = str(prompt or "").strip()[:120]
        encoded_query = quote_plus(query_text)
        return {
            "note": "Simulated internal requests for SU Scholar routes.",
            "requests": [
                {
                    "label": "Publication search",
                    "method": "GET",
                    "path": f"/advanced_search/?tab=documents&search={encoded_query}",
                },
                {
                    "label": "Researcher search",
                    "method": "GET",
                    "path": f"/advanced_search/?tab=researchers&research_q={encoded_query}",
                },
                {
                    "label": "LLM ask",
                    "method": "WS",
                    "path": "/ws/llm/",
                    "action": {
                        "action": "ask",
                        "prompt": str(prompt or "")[:200],
                    },
                },
            ],
        }
