from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from openai import APIConnectionError, APITimeoutError, BadRequestError, NotFoundError, OpenAI


JSON_ONLY_SYSTEM_PROMPT = """
Ты движок, который отвечает только в формате JSON.

Верни только валидный JSON.
Не пиши объяснений.
Не используй markdown.
Не оборачивай JSON в блоки кода.
Не добавляй текст до или после JSON.
Не добавляй комментарии.
Не используй trailing commas.
Если значение неизвестно — верни null, пустую строку, пустой массив или пустой объект в зависимости от схемы.
Строго следуй запрошенной JSON-схеме.
Ответ должен успешно разбираться через standard json.loads().
Если схема требует объект — верни JSON-объект.
Если схема требует массив — верни JSON-массив.
Никакого текста вне JSON.
Текстоые значения должны формироваться строго на языке, на котором задан вопрос.
""".strip()


class LlmJsonClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.0,
    ):
        self.model = (model or getattr(settings, "LLM_MODEL", "gpt-oss:20b")).strip() or "gpt-oss:20b"
        self.base_url = (base_url or getattr(settings, "LLM_API", "")).strip()
        self.api_key = (api_key or getattr(settings, "LLM_API_KEY", "")).strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature

        if not self.base_url:
            raise ValueError("LLM_API is not configured")

        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    def change_model(self, new_model: str) -> None:
        new_model = (new_model or "").strip()
        if new_model:
            self.model = new_model

    def _validate_messages(self, messages: list[dict[str, Any]]) -> None:
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")

        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ValueError(f"messages[{index}] must be a dict")

            role = message.get("role")
            content = message.get("content")

            if not role:
                raise ValueError(f"messages[{index}]['role'] is required")
            if content is None or str(content).strip() == "":
                raise ValueError(f"messages[{index}]['content'] is required")

    def _extract_json_text(self, raw: str) -> str:
        text = (raw or "").strip()

        if not text:
            raise ValueError("Model returned empty response")

        # На случай если модель всё-таки завернула в ```json ... ```
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()

        return text

    def send_json_request(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        schema_hint: str | None = None,
    ) -> dict[str, Any] | list[Any]:
        self._validate_messages(messages)

        final_messages = [
            {
                "role": "system",
                "content": JSON_ONLY_SYSTEM_PROMPT,
            }
        ]

        if schema_hint:
            final_messages.append(
                {
                    "role": "system",
                    "content": (
                        "Строго верни JSON, соответствующий этой схеме или формату:"
                        f"{schema_hint}"
                    ),
                }
            )

        final_messages.extend(messages)
        print("=== LLM REQUEST MESSAGES ===")
        # print(json.dumps(final_messages, indent=2, ensure_ascii=False))
        model_name = (model or self.model).strip()

        try:
            response = self.client.chat.completions.create(
                model=model_name,
                messages=final_messages,
                stream=False,
                temperature=self.temperature if temperature is None else temperature,
            )
        except (APIConnectionError, APITimeoutError) as exc:
            raise ConnectionError(
                "LLM модель не отвечает. Проверьте доступность API и повторите запрос."
            ) from exc
        except (BadRequestError, NotFoundError) as exc:
            raise ValueError(
                f"Неверный id модели '{model_name}' или модель недоступна на LLM API."
            ) from exc

        content = response.choices[0].message.content if response.choices else ""
        raw_text = content if isinstance(content, str) else ""
        json_text = self._extract_json_text(raw_text)

        try:
            return json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Model returned invalid JSON: {exc}. Raw response: {raw_text}"
            ) from exc
        
 
