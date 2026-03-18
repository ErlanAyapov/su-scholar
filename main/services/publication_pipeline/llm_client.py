import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from openai import OpenAI
from utils.openai_client import build_openai_client_kwargs

from .utils import (
    MAX_CLEANED_TEXT_CHARS,
    MAX_RAW_PROMPT_CHARS,
    estimate_tokens,
    json_dumps,
    parse_json_loose,
    preview_text,
    smart_truncate,
    truncate_text,
)


logger = logging.getLogger(__name__)


PROMPT_CANDIDATES = (
    Path(__file__).resolve().parent / "prompts" / "publication_extraction_prompt.txt",
    Path(__file__).resolve().parent.parent / "preset_context.txt",
)


@dataclass
class LlmExtractionResult:
    payload: dict[str, Any]
    raw_output: str
    elapsed_sec: float
    prompt_tokens_est: int
    response_tokens_est: int
    usage: dict[str, Any]


def load_system_prompt() -> str:
    for path in PROMPT_CANDIDATES:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError("Publication extraction prompt not found")


def _select_prompt_meta(meta: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}

    selected = {}
    preferred_prefixes = ("citation_", "dc.", "dcterms.", "og:", "prism.", "twitter:")
    preferred_keys = {"description", "author", "title", "doi", "identifier", "keywords"}
    for key, value in meta.items():
        lowered = key.lower()
        if any(lowered.startswith(prefix) for prefix in preferred_prefixes) or any(token in lowered for token in preferred_keys):
            selected[key] = value
    return selected or dict(list(meta.items())[:40])


def build_messages(intermediate_payload: dict[str, Any], system_prompt: str) -> list[dict[str, str]]:
    prompt_payload = {
        "source_type": intermediate_payload.get("source_type", ""),
        "source_url": intermediate_payload.get("source_url", ""),
        "page_title": intermediate_payload.get("page_title", ""),
        "headings": intermediate_payload.get("headings", {}),
        "meta": _select_prompt_meta(intermediate_payload.get("meta", {})),
        "json_ld": intermediate_payload.get("json_ld", [])[:5],
        "links": intermediate_payload.get("links", [])[:80],
        "cleaned_text": smart_truncate(intermediate_payload.get("cleaned_text", ""), MAX_CLEANED_TEXT_CHARS),
        "raw_html_preview": truncate_text(intermediate_payload.get("raw_html", ""), MAX_RAW_PROMPT_CHARS),
    }

    user_prompt = (
        "Extract publication metadata from the provided source payload.\n"
        "Return valid JSON only.\n"
        "Do not invent an abstract from citation lines, bibliographic references, author/title/journal blocks, or URL lines.\n"
        "If only a citation snippet is visible, set abstract to an empty string and mark needs_review=true.\n"
        f"{json_dumps(prompt_payload, indent=2)}"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


class PublicationLLMClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.model = (model or getattr(settings, "LLM_MODEL", "gpt-oss:20b")).strip() or "gpt-oss:20b"
        self.base_url = (base_url or getattr(settings, "LLM_API", "")).strip()
        self.api_key = (api_key or getattr(settings, "LLM_API_KEY", "")).strip() or "ollama"
        if not self.base_url:
            raise ValueError("LLM_API is not configured")
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key, **build_openai_client_kwargs())
        self.system_prompt = load_system_prompt()

    def extract(self, intermediate_payload: dict[str, Any], publication_id: int | None = None) -> LlmExtractionResult:
        messages = build_messages(intermediate_payload, self.system_prompt)
        prompt_tokens_est = estimate_tokens(json_dumps(messages, indent=2))

        started = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=False,
            temperature=0,
        )
        elapsed_sec = time.perf_counter() - started

        content = response.choices[0].message.content or ""
        payload = parse_json_loose(content)
        usage = {
            "prompt_tokens": getattr(response.usage, "prompt_tokens", None),
            "completion_tokens": getattr(response.usage, "completion_tokens", None),
            "total_tokens": getattr(response.usage, "total_tokens", None),
        }

        payload_publication = payload.get("publication", {}) if isinstance(payload, dict) else {}
        logger.info(
            "LLM extracted publication_id=%s source_type=%s source_url=%s title=%s abstract=%s model=%s",
            publication_id or "",
            intermediate_payload.get("source_type", ""),
            intermediate_payload.get("source_url", ""),
            preview_text(payload_publication.get("title_original", ""), 140),
            preview_text(payload_publication.get("abstract", ""), 200),
            self.model,
        )

        return LlmExtractionResult(
            payload=payload,
            raw_output=content,
            elapsed_sec=elapsed_sec,
            prompt_tokens_est=prompt_tokens_est,
            response_tokens_est=estimate_tokens(content),
            usage=usage,
        )


def extract_structured_with_llm(intermediate_payload: dict[str, Any], publication_id: int | None = None) -> dict[str, Any]:
    return PublicationLLMClient().extract(intermediate_payload, publication_id=publication_id).payload
