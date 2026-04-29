from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db.models import F
from django.utils import timezone
from docx import Document as DocxDocument

from document.models import Document, DocumentPermission
from llm.mdpi_template_extractor import get_extractor as get_mdpi_extractor
from llm.mdpi_template_extractor import is_mdpi_request
from main.models import Project, Publication, PublicationFile

from .context_builder import ContextBuilder
from .document_editor import DocumentEditor
from .formula_utils import has_latex_formula_markers
from .llm_core import LlmJsonClient
from .llm_edit_planner import LlmEditPlanner
from .utils import search_from_db, trim_db_result
from .ws_confirm_gate import ConfirmationGate
from .ws_doc_edit_publisher import DocEditPublisher
from .ws_publisher import AgentProgressPublisher


logger = logging.getLogger(__name__)



def _normalize_search_tag(tag: str) -> str:
    return " ".join((tag or "").strip().lower().split())


_SEARCH_TAG_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё+#.-]*")
_ALLOWED_SHORT_TERMS = {
    "ai",
    "ml",
    "dl",
    "cv",
    "nlp",
    "llm",
    "iot",
    "rl",
    "ar",
    "vr",
    "3d",
    "2d",
    "5g",
    "6g",
    "rfid",
    "gps",
    "gis",
    "api",
    "sdk",
    "sql",
    "cnn",
    "rnn",
    "gnn",
}
_SEARCH_TAG_STOPWORDS = {
    "и",
    "или",
    "для",
    "по",
    "на",
    "в",
    "во",
    "к",
    "ко",
    "от",
    "до",
    "над",
    "под",
    "без",
    "с",
    "со",
    "у",
    "о",
    "об",
    "из",
    "это",
    "этот",
    "эта",
    "эти",
    "the",
    "a",
    "an",
    "and",
    "or",
    "for",
    "to",
    "from",
    "in",
    "on",
    "of",
    "with",
    "by",
    "about",
}
_SEARCH_TAG_NOISE_TOKENS = {
    "публикация",
    "публикации",
    "публикаций",
    "publication",
    "publications",
    "проект",
    "проекты",
    "проектов",
    "project",
    "projects",
    "исследование",
    "исследования",
    "research",
    "материал",
    "материалы",
    "material",
    "materials",
    "работа",
    "работы",
    "work",
    "works",
    "статья",
    "статьи",
    "article",
    "articles",
    "тема",
    "topics",
    "topic",
    "данные",
    "data",
    "контекст",
    "context",
    "документ",
    "документы",
    "document",
    "documents",
    "поиск",
    "search",
    "ключевые",
    "ключевое",
    "слово",
    "слова",
    "keywords",
    "keyword",
    "релевантные",
    "релевантный",
    "релевантность",
    "semantic",
    "matching",
    "json",
    "status",
    "success",
    "search_again",
    "string",
    "user_query",
    "database_result",
    "used_keywords",
    "ui",
    "sidebar",
    "workspace",
    "projectworkspaceleftsidebar",
    "publicationreference",
    "publicationreferences",
    "publicationfile",
    "publicationfiles",
    "project_page",
    "добавить",
    "добавление",
    "список",
    "файл",
    "файлы",
    "участники",
    "проекта",
}


def _tokenize_tag(tag: str) -> list[str]:
    return [token.lower() for token in _SEARCH_TAG_TOKEN_RE.findall(tag or "")]


def _sanitize_search_tag(tag: str) -> str:
    cleaned = " ".join(str(tag or "").strip().split()).strip(".,;:!?\"'`[](){}")
    if not cleaned:
        return ""

    tokens = _tokenize_tag(cleaned)
    if not tokens:
        return ""

    filtered = []
    seen = set()
    for token in tokens:
        if token in _SEARCH_TAG_STOPWORDS or token in _SEARCH_TAG_NOISE_TOKENS:
            continue
        if token in seen:
            continue
        seen.add(token)
        filtered.append(token)

    if not filtered:
        return ""

    if all(token.isdigit() for token in filtered):
        return ""

    if len(filtered) == 1:
        token = filtered[0]
        if len(token) < 3 and token not in _ALLOWED_SHORT_TERMS:
            return ""

    # Keep tags concise and focused.
    return " ".join(filtered[:5])


def _is_informative_tag_token(token: str) -> bool:
    if not token:
        return False
    if token in _SEARCH_TAG_STOPWORDS or token in _SEARCH_TAG_NOISE_TOKENS:
        return False
    if token.isdigit():
        return False
    if len(token) < 3 and token not in _ALLOWED_SHORT_TERMS:
        return False
    return True


def _derive_fallback_search_tags(*texts: str, exclude_normalized: set[str] | None = None, limit: int = 8) -> list[str]:
    exclude = exclude_normalized or set()
    score_map: dict[str, int] = {}

    for text in texts:
        tokens = [token for token in _tokenize_tag(text or "") if _is_informative_tag_token(token)]
        for token in tokens:
            normalized = _normalize_search_tag(token)
            if normalized in exclude:
                continue
            score_map[normalized] = score_map.get(normalized, 0) + 1

        for i in range(len(tokens) - 1):
            left = tokens[i]
            right = tokens[i + 1]
            bigram = f"{left} {right}"
            normalized_bigram = _normalize_search_tag(bigram)
            if normalized_bigram in exclude:
                continue
            score_map[normalized_bigram] = score_map.get(normalized_bigram, 0) + 2

    ranked = sorted(
        score_map.items(),
        key=lambda item: (-item[1], -len(item[0]), item[0]),
    )
    return [tag for tag, _score in ranked[: max(1, limit)]]


def _prepare_unique_tags(tags) -> list[str]:
    result = []
    seen = set()

    if not isinstance(tags, (list, tuple)):
        return result

    for tag in tags:
        if not isinstance(tag, str):
            continue
        cleaned = _sanitize_search_tag(tag)
        normalized = _normalize_search_tag(cleaned)
        if not cleaned or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(cleaned)

    return result


def _filter_new_tags(tags, used_tags_normalized: set[str]) -> list[str]:
    result = []
    local_seen = set()

    for tag in _prepare_unique_tags(tags):
        normalized = _normalize_search_tag(tag)
        if normalized in used_tags_normalized or normalized in local_seen:
            continue
        local_seen.add(normalized)
        result.append(tag)

    return result


def _prepare_unique_ids(values) -> list[int]:
    result = []
    seen = set()
    if not isinstance(values, (list, tuple)):
        return result

    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        result.append(parsed)

    return result


def _normalize_document_target_type(raw_type: str) -> str:
    value = str(raw_type or "").strip().lower()
    if value in {"project", "project_document", "document", "project_file"}:
        return "project"
    if value in {"publication_file", "publicationfile", "pub_file", "publication"}:
        return "publication_file"
    if value in {"auto", "project/publication_file", "project_or_publication_file"}:
        return "auto"

    parts = [part for part in re.split(r"[\\/|,]", value) if part]
    for part in parts:
        normalized = _normalize_document_target_type(part)
        if normalized:
            return normalized
    return ""


def _prepare_document_targets(values) -> list[dict]:
    result = []
    seen = set()
    if not isinstance(values, (list, tuple)):
        return result

    for raw_item in values:
        if not isinstance(raw_item, dict):
            continue
        try:
            parsed_id = int(raw_item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if parsed_id <= 0:
            continue
        normalized_type = _normalize_document_target_type(raw_item.get("type"))
        if not normalized_type:
            continue
        dedupe_key = f"{normalized_type}:{parsed_id}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append(
            {
                "id": parsed_id,
                "type": normalized_type,
            }
        )

    return result


def _safe_llm_response(response: dict | None) -> dict:
    if not isinstance(response, dict):
        return {
            "status": "search_again",
            "confidence": 0.0,
            "publications": [],
            "projects": [],
            "search_tags": [],
        }

    status = response.get("status")
    if status not in {"success", "search_again"}:
        status = "search_again"

    confidence = response.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    publications = _prepare_unique_ids(response.get("publications"))
    projects = _prepare_unique_ids(response.get("projects"))

    search_tags = response.get("search_tags")
    if not isinstance(search_tags, list):
        search_tags = []

    return {
        "status": status,
        "confidence": max(0.0, min(1.0, confidence)),
        "publications": publications,
        "projects": projects,
        "search_tags": _prepare_unique_tags(search_tags),
    }


class ProjectAgentOrchestrator:
    def __init__(
        self,
        user,
        project,
        session_id=None,
        document_id=None,
        publication_id=None,
        publication_file_id=None,
        thinking_mode="middle",
        access_scope="active_project",
        interaction_mode="ask",
        auth_cookie_header="",
        active_doc_key=None,
        task_id=None,
        
    ):
        normalized_mode = str(thinking_mode or "").strip().lower()
        if normalized_mode not in ["fast", "middle", "high"]:
            raise ValueError("Invalid thinking mode. Must be 'fast', 'middle' or 'high'.")

        self.thinking_mode = normalized_mode
        self.model_map = self._build_model_map()
        self.llm_extraction = LlmJsonClient(
            timeout=self._llm_timeout_for_mode(self.thinking_mode),
            max_retries=self._llm_max_retries_for_mode(self.thinking_mode),
        )
        self.active_doc_key = str(active_doc_key or "").strip()

        self.session_id = session_id
        self.document_id = document_id
        self.publication_file_id = publication_file_id
        self.access_scope = self._normalize_access_scope(access_scope)
        self.interaction_mode = self._normalize_interaction_mode(interaction_mode)
        self.auth_cookie_header = str(auth_cookie_header or "").strip()
        self.task_id = str(task_id or "").strip()
        self.project = project
        self.user = user
        self.context_builder = ContextBuilder(project, user)
        self.progress_publisher = AgentProgressPublisher(project_id=project.id, session_id=session_id)
        self.search_again_attempt_count = 3
        self.active_document_context = self.context_builder.get_document_context_by_id(self.document_id) if self.document_id else None
        self.active_publication_file_context = (
            self.context_builder.get_publication_file_context_by_id(self.publication_file_id)
            if self.publication_file_id
            else None
        )
        self.active_publication = self._resolve_active_publication(publication_id)
        self.active_publication_context = self._build_active_publication_context()
        self._last_prompt = ""
        self._last_context = None

    @staticmethod
    def _cancel_cache_key(task_id: str) -> str:
        return f"project_agent_cancel:{str(task_id or '').strip()}"

    def _is_cancelled(self) -> bool:
        return bool(self.task_id and cache.get(self._cancel_cache_key(self.task_id)))

    def _raise_if_cancelled(self) -> None:
        if self._is_cancelled():
            raise RuntimeError("Операция остановлена пользователем.")

    @staticmethod
    def _llm_timeout_for_mode(mode):
        if mode == "fast":
            return float(getattr(settings, "PROJECT_AGENT_LLM_TIMEOUT_FAST", 120))
        if mode == "high":
            return float(getattr(settings, "PROJECT_AGENT_LLM_TIMEOUT_HIGH", 1200))
        return float(getattr(settings, "PROJECT_AGENT_LLM_TIMEOUT_MIDDLE", 300))

    @staticmethod
    def _llm_max_retries_for_mode(mode):
        if mode == "high":
            return int(getattr(settings, "PROJECT_AGENT_LLM_MAX_RETRIES_HIGH", 4))
        if mode == "fast":
            return int(getattr(settings, "PROJECT_AGENT_LLM_MAX_RETRIES_FAST", 2))
        return int(getattr(settings, "PROJECT_AGENT_LLM_MAX_RETRIES_MIDDLE", 3))

    @staticmethod
    def _build_model_map():
        fast_model = (getattr(settings, "PROJECT_AGENT_MODEL_FAST", "gpt-oss:20b") or "").strip() or "gpt-oss:20b"
        middle_model = (getattr(settings, "PROJECT_AGENT_MODEL_MIDDLE", "qwen3:30b") or "").strip() or fast_model
        high_model = (getattr(settings, "PROJECT_AGENT_MODEL_HIGH", "gpt-oss:120b") or "").strip() or middle_model
        return {
            "fast": fast_model,
            "middle": middle_model,
            "high": high_model,
        }

    @staticmethod
    def _normalize_access_scope(raw_value: str) -> str:
        value = str(raw_value or "").strip().lower()
        if value in {"active_document", "active_project", "full_access"}:
            return value
        if value in {"high", "full", "all", "global"}:
            return "full_access"
        return "active_project"

    @staticmethod
    def _normalize_interaction_mode(raw_value: str) -> str:
        value = str(raw_value or "").strip().lower()
        if value in {"autonomous", "ask"}:
            return value
        return "ask"

    def get_model(self, task_type):
        selected_model = self.model_map.get(self.thinking_mode)
        if selected_model:
            return selected_model
        return self.model_map.get("middle") or self.model_map.get("fast") or "gpt-oss:20b"

    def _resolve_active_publication(self, publication_id):
        try:
            parsed_id = int(publication_id or 0)
        except (TypeError, ValueError):
            parsed_id = 0
        if parsed_id <= 0:
            return None
        return (
            Publication.objects.filter(
                publicationproject__project=self.project,
                id=parsed_id,
                private=False,
            )
            .select_related("venue")
            .prefetch_related("authors")
            .first()
        )

    def _build_active_publication_context(self) -> str:
        publication = self.active_publication
        if not publication:
            return ""

        authors = ", ".join(
            author.full_name.strip()
            for author in publication.authors.all()
            if (author.full_name or "").strip()
        )
        lines = [
            f"ID: {publication.id}",
            f"Название: {publication.title_original}",
            f"Год: {publication.year}",
        ]
        if publication.venue_id and publication.venue and publication.venue.name:
            lines.append(f"Источник: {publication.venue.name}")
        if publication.abstract:
            lines.append(f"Аннотация: {publication.abstract}")
        if publication.keywords:
            lines.append(f"Ключевые слова: {publication.keywords}")
        if authors:
            lines.append(f"Авторы: {authors}")
        if publication.doi:
            lines.append(f"DOI: {publication.doi}")
        return "\n".join(lines)

    def _combined_context(self) -> str:
        parts = []
        if self.active_publication_context:
            parts.append("Контекст активной публикации:\n" + self.active_publication_context)
        if self.active_document_context:
            parts.append("Контекст активного документа:\n" + self.active_document_context)
        if self.active_publication_file_context:
            parts.append("Контекст активного файла публикации:\n" + self.active_publication_file_context)
        return "\n\n".join(parts).strip()

    def _combined_context_for_access(self) -> str:
        if self.access_scope == "active_document":
            if self.active_document_context:
                return "Контекст активного документа:\n" + self.active_document_context
            if self.active_publication_file_context:
                return "Контекст активного файла публикации:\n" + self.active_publication_file_context
            return ""
        return self._combined_context()

    def _external_search_allowed(self) -> bool:
        return self.access_scope == "full_access"

    def _document_details_allowed(self) -> bool:
        return self.access_scope != "active_document"

    def _build_context_for_access_scope(self):
        if self.access_scope != "active_document":
            return self.context_builder.build()
        return {
            "project": {
                "id": self.project.id,
                "title": self.project.name,
            } if self.project else None,
            "user": {
                "id": self.user.id,
                "username": self.user.username,
            } if self.user else None,
            "documents": {},
            "references": {
                "total_references": 0,
                "publications": [],
            },
        }

    def _scope_restriction_message(self) -> str:
        if self.access_scope == "active_document":
            return (
                "Доступ ограничен активным документом. Для расширенного поиска переключите доступ на "
                "\"Проект\" или \"Полный доступ\"."
            )
        if self.access_scope == "active_project":
            return (
                "Доступ ограничен материалами проекта. Для поиска внешних работ переключите доступ на "
                "\"Полный доступ\"."
            )
        return "Недостаточно данных для уверенного ответа."

    def _serialize_publication(self, publication: Publication) -> dict:
        authors = ", ".join(
            author.full_name.strip()
            for author in publication.authors.all()
            if (author.full_name or "").strip()
        )
        return {
            "id": publication.id,
            "title": publication.title_original or "",
            "year": publication.year,
            "abstract": (publication.abstract or "")[:4000],
            "keywords": (publication.keywords or "")[:1000],
            "doi": publication.doi or "",
            "venue": publication.venue.name if publication.venue_id and publication.venue else "",
            "authors": authors,
        }

    @staticmethod
    def _serialize_project(project: Project) -> dict:
        return {
            "id": project.id,
            "name": project.name or "",
            "description": (project.description or "")[:4000],
            "project_type": getattr(project, "project_type", "") or "",
        }

    def _collect_relevant_entities(self, publication_ids, project_ids) -> dict:
        normalized_publication_ids = [
            publication_id
            for publication_id in _prepare_unique_ids(publication_ids)
            if publication_id != getattr(self.active_publication, "id", None)
        ]
        normalized_project_ids = _prepare_unique_ids(project_ids)

        publication_map = {
            publication.id: publication
            for publication in Publication.objects.filter(private=False, id__in=normalized_publication_ids)
            .select_related("venue")
            .prefetch_related("authors")
        }
        project_map = {
            project.id: project
            for project in Project.objects.filter(id__in=normalized_project_ids)
        }

        publications = [
            self._serialize_publication(publication_map[publication_id])
            for publication_id in normalized_publication_ids
            if publication_id in publication_map
        ]
        projects = [
            self._serialize_project(project_map[project_id])
            for project_id in normalized_project_ids
            if project_id in project_map
        ]

        return {
            "publications": publications[:8],
            "projects": projects[:5],
        }

    def _generate_response_from_context(self, prompt: str, relevant_context: dict) -> dict:
        publications = relevant_context.get("publications") if isinstance(relevant_context, dict) else []
        projects = relevant_context.get("projects") if isinstance(relevant_context, dict) else []
        context_payload = {
            "user_query": prompt,
            "active_context": self._combined_context(),
            "relevant_publications": publications,
            "relevant_projects": projects,
        }

        schema_hint = """
        {
            "response_text": "string",
            "confidence": 0.0,
            "language": "string"
        }
        """.strip()

        result = self.llm_extraction.send_json_request(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — академический исследовательский ассистент. "
                        "Сформируй ответ строго в JSON и используй только данные из переданного контекста. "
                        "Не выдумывай факты, не добавляй несуществующие цитаты и не ссылайся на работы, которых нет в списке."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Подготовь содержательный ответ на запрос пользователя на основе активного контекста "
                        "и найденных релевантных публикаций/проектов.\n\n"
                        "Требования:\n"
                        "1. Ответ должен быть полезным и конкретным.\n"
                        "2. Если релевантные публикации найдены, кратко интегрируй их в объяснение.\n"
                        "3. Не используй Markdown-таблицы.\n"
                        "4. Верни только JSON.\n\n"
                        + json.dumps(context_payload, ensure_ascii=False)
                    ),
                },
            ],
            schema_hint=schema_hint,
            temperature=0.2,
        )

        response_text = str(result.get("response_text") or "").strip() if isinstance(result, dict) else ""
        if not response_text:
            fallback_titles = [item.get("title") for item in publications[:3] if item.get("title")]
            if fallback_titles:
                response_text = (
                    "Найдены релевантные публикации по теме запроса: "
                    + "; ".join(fallback_titles)
                    + ". Используйте их как основу для дальнейшей проработки ответа."
                )
            else:
                response_text = "Собрал релевантный контекст, но не удалось сформировать содержательный ответ."

        return {
            "mode": "text_response",
            "response_text": response_text,
            "search_tags": [],
            "confidence": result.get("confidence") if isinstance(result, dict) else 0.0,
            "language": result.get("language") if isinstance(result, dict) else "",
            "relevant_publication_ids": [item.get("id") for item in publications if item.get("id")],
            "relevant_project_ids": [item.get("id") for item in projects if item.get("id")],
            "document_targets": [],
        }
         
    def _project_document_for_operation(self) -> Document | None:
        try:
            parsed_id = int(self.document_id or 0)
        except (TypeError, ValueError):
            parsed_id = 0
        if parsed_id <= 0:
            return None
        return (
            Document.objects.filter(
                id=parsed_id,
                is_deleted=False,
                projects=self.project,
            )
            .only("id", "title", "file", "file_type", "version", "user_id")
            .first()
        )

    def _project_publication_file_for_operation(self) -> PublicationFile | None:
        try:
            parsed_id = int(self.publication_file_id or 0)
        except (TypeError, ValueError):
            parsed_id = 0
        if parsed_id <= 0:
            return None
        return (
            PublicationFile.objects.filter(
                id=parsed_id,
                publication__publicationproject__project=self.project,
                publication__private=False,
            )
            .select_related("publication")
            .first()
        )

    def _can_edit_project_document(self, document: Document | None) -> bool:
        if not document or not self.user or not self.user.is_authenticated:
            return False
        if document.user_id == self.user.id:
            return True
        permission = (
            DocumentPermission.objects.filter(document=document, user=self.user)
            .only("can_edit")
            .first()
        )
        return bool(permission and permission.can_edit)

    def _can_edit_publication_file(self, publication_file: PublicationFile | None) -> bool:
        if not publication_file or not self.user or not self.user.is_authenticated:
            return False
        publication = publication_file.publication
        return bool(publication and not publication.private and publication.created_by_id == self.user.id)

    @staticmethod
    def _is_docx_project_document(document: Document | None) -> bool:
        if not document or not document.file:
            return False
        file_type = str(document.file_type or "").strip().lower()
        if file_type == "docx":
            return True
        extension = os.path.splitext(document.file.name or "")[1].lower()
        return extension == ".docx"

    @staticmethod
    def _is_docx_publication_file(publication_file: PublicationFile | None) -> bool:
        if not publication_file or not publication_file.file:
            return False
        extension = os.path.splitext(publication_file.file.name or "")[1].lower()
        return extension == ".docx"

    def _get_onlyoffice_document_key(self, document_id: int) -> str | None:
        try:
            parsed_id = int(document_id or 0)
        except (TypeError, ValueError):
            parsed_id = 0
        if parsed_id <= 0:
            return None

        document = (
            Document.objects.filter(
                id=parsed_id,
                is_deleted=False,
                projects=self.project,
            )
            .only("id", "version")
            .first()
        )
        if not document:
            return None
        version = int(document.version or 1)
        return f"doc-{document.id}-v{version}"

    def _get_onlyoffice_publication_file_key(self, publication_file_id: int) -> str | None:
        publication_file = (
            PublicationFile.objects.filter(
                id=publication_file_id,
                publication__publicationproject__project=self.project,
                publication__private=False,
            )
            .select_related("publication")
            .first()
        )
        if not publication_file:
            return None

        file_size = 0
        if publication_file.file:
            try:
                file_size = publication_file.file.size
            except OSError:
                file_size = 0

        publication_updated_at = 0
        publication = publication_file.publication
        if publication and publication.updated_at:
            publication_updated_at = int(publication.updated_at.timestamp())

        return (
            f"project-{self.project.id}-publication-file-{publication_file.id}"
            f"-u{publication_updated_at}-s{file_size}"
        )

    # ИСПРАВЛЕНИЕ — вернуть обратно ветку publication_file
    def _resolve_document_operation_target(self) -> dict | None:
        project_document = self._project_document_for_operation()
        if project_document:
            return {
                "type": "project",
                "id": project_document.id,
                "object": project_document,
                "title": project_document.title or f"Документ #{project_document.id}",
                "can_edit": self._can_edit_project_document(project_document),
                "is_docx": self._is_docx_project_document(project_document),
                "document_key": self._get_onlyoffice_document_key(project_document.id),
            }

        publication_file = self._project_publication_file_for_operation()
        if publication_file:
            publication = publication_file.publication
            return {
                "type": "publication_file",
                "id": publication_file.id,
                "object": publication_file,
                "title": (
                    publication_file.description
                    or (publication.title_original if publication else "")
                    or f"Файл публикации #{publication_file.id}"
                ),
                "can_edit": self._can_edit_publication_file(publication_file),
                "is_docx": self._is_docx_publication_file(publication_file),
                "document_key": self._get_onlyoffice_publication_file_key(publication_file.id),
            }

        return None

    @staticmethod
    def _document_operation_target_payload(target: dict | None) -> list[dict]:
        if not isinstance(target, dict):
            return []
        try:
            target_id = int(target.get("id") or 0)
        except (TypeError, ValueError):
            return []
        if target_id <= 0:
            return []
        target_type = str(target.get("type") or "").strip().lower()
        if target_type not in {"project", "publication_file"}:
            return []
        return [{"id": target_id, "type": target_type}]

    def _increment_document_version(self, target: dict) -> None:
        """
        Поднимает версию документа в БД.
        OnlyOffice строит document_key из версии — после смены ключа
        он при следующем открытии загрузит файл заново с диска,
        не используя старую Co-editing сессию.
        """
        target_type = target.get("type")
        obj = target.get("object")

        if target_type == "project" and isinstance(obj, Document):
            Document.objects.filter(id=obj.id).update(
                version=F("version") + 1,
                updated_at=timezone.now(),
            )
            obj.refresh_from_db(fields=["version", "updated_at"])
        elif target_type == "publication_file" and isinstance(obj, PublicationFile):
            publication = getattr(obj, "publication", None)
            if publication:
                publication.updated_at = timezone.now()
                publication.save(update_fields=["updated_at"])

    @staticmethod
    def _trim_for_preview(value: str, max_len: int = 180) -> str:
        text = " ".join(str(value or "").split()).strip()
        if len(text) <= max_len:
            return text
        return f"{text[:max_len].rstrip()}..."

    @staticmethod
    def _node_agent_base_url() -> str:
        configured = str(getattr(settings, "PROJECT_AGENT_NODE_URL", "") or "").strip()
        if configured:
            return configured.rstrip("/")
        return "http://node:3000"

    @staticmethod
    def _internal_app_base_url() -> str:
        configured = str(getattr(settings, "PROJECT_AGENT_INTERNAL_APP_BASE_URL", "") or "").strip()
        if configured:
            return configured.rstrip("/")
        return "http://web:8000"

    def _target_onlyoffice_config_url(self, target: dict) -> str:
        target_type = str(target.get("type") or "").strip().lower()
        target_id = int(target.get("id") or 0)
        base_url = self._internal_app_base_url()
        if target_type == "project" and target_id > 0:
            return f"{base_url}/onlyoffice/config/{target_id}/"
        if target_type == "publication_file" and target_id > 0:
            return (
                f"{base_url}/llm/project/{self.project.id}/"
                f"publication-files/{target_id}/config/"
            )
        return ""

    @staticmethod
    def _document_operation_engine() -> str:
        raw_value = str(
            getattr(settings, "PROJECT_AGENT_DOCUMENT_OPERATION_ENGINE", "node_realtime")
            or "node_realtime"
        ).strip().lower()
        if raw_value in {"docx_patch", "patch", "offline_patch"}:
            return "docx_patch"
        return "node_realtime"

    @staticmethod
    def _plan_contains_formulas(operations: list[dict]) -> bool:
        if not isinstance(operations, list):
            return False
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            for key in ("new_text", "text"):
                if has_latex_formula_markers(str(operation.get(key) or "")):
                    return True
        return False

    @classmethod
    def _document_operation_engine_for_plan(cls, base_engine: str, operations: list[dict]) -> str:
        normalized_engine = str(base_engine or "node_realtime").strip().lower()
        if normalized_engine == "node_realtime" and cls._plan_contains_formulas(operations):
            return "docx_patch"
        if normalized_engine == "docx_patch":
            return "docx_patch"
        return "node_realtime"

    @staticmethod
    def _target_file_field(target: dict):
        obj = target.get("object") if isinstance(target, dict) else None
        return getattr(obj, "file", None)

    def _read_target_docx_bytes(self, target: dict) -> bytes:
        file_field = self._target_file_field(target)
        if not file_field:
            raise FileNotFoundError("У активного документа отсутствует файл.")
        try:
            file_field.open("rb")
            return file_field.read() or b""
        finally:
            try:
                file_field.close()
            except Exception:
                pass

    def _save_target_docx_bytes(self, target: dict, raw_bytes: bytes) -> None:
        if not raw_bytes:
            raise ValueError("Сохранение пустого файла запрещено.")

        obj = target.get("object")
        file_field = self._target_file_field(target)
        if obj is None or file_field is None:
            raise ValueError("Активный документ недоступен для сохранения.")

        target_type = str(target.get("type") or "").strip().lower()
        target_id = int(target.get("id") or 0)
        current_name = Path(getattr(file_field, "name", "") or "")
        base_stem = current_name.stem or f"{target_type or 'document'}-{target_id}"
        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        storage_name = f"{base_stem}-{timestamp}.docx"

        if file_field.name:
            file_field.delete(save=False)
        file_field.save(storage_name, ContentFile(raw_bytes), save=False)

        if target_type == "project" and isinstance(obj, Document):
            obj.file_type = "docx"
            obj.save(update_fields=["file", "file_type"])
        elif target_type == "publication_file" and isinstance(obj, PublicationFile):
            obj.save(update_fields=["file"])
        else:
            obj.save(update_fields=["file"])

        self._increment_document_version(target)

    def _read_target_docx_source(self, target: dict) -> tuple[bytes, list[dict]]:
        source_bytes = self._read_target_docx_bytes(target)
        if not source_bytes:
            raise ValueError("Исходный DOCX-файл пуст.")
        try:
            source_doc = DocxDocument(io.BytesIO(source_bytes))
            # Передаём индекс, текст и стиль — LLM видит точную структуру
            paragraphs = [
                {
                    "index": i,
                    "text": para.text or "",
                    "style": para.style.name if para.style else "Normal",
                }
                for i, para in enumerate(source_doc.paragraphs)
            ]
        except Exception as exc:
            raise ValueError(f"Не удалось разобрать DOCX: {exc}") from exc
        return source_bytes, paragraphs
  
    @staticmethod
    def _mdpi_enriched_edit_context(prompt: str, paragraphs: list[dict], edit_context: str = "") -> str:
        if not is_mdpi_request(prompt):
            return str(edit_context or "").strip()

        extractor = get_mdpi_extractor()
        if extractor is None:
            return str(edit_context or "").strip()

        try:
            issues = extractor.validate_section_order(paragraphs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MDPI section validation failed: %s", exc)
            issues = []

        if not issues:
            return str(edit_context or "").strip()

        issues_block = "MDPI STRUCTURE ISSUES DETECTED:\n" + "\n".join(f"- {issue}" for issue in issues)
        current_context = str(edit_context or "").strip()
        return f"{current_context}\n\n{issues_block}".strip() if current_context else issues_block

    def _build_document_edit_plan(self, *, prompt: str, paragraphs: list[str], edit_context: str = "") -> dict:
        planner = LlmEditPlanner(self.llm_extraction)
        active_context = self._combined_context_for_access()

        doc_snapshot = "\n".join(
            f"[{p['index']}] ({p['style']}) {p['text']}"
            for p in paragraphs
            if p["text"].strip()
        )
        active_context = (
            f"DOCUMENT SNAPSHOT (index | style | text):\n{doc_snapshot}\n\n"
            + (active_context or "")
        )

        edit_context = self._mdpi_enriched_edit_context(prompt, paragraphs, edit_context)
        if edit_context:
            active_context = (
                f"{active_context}\n\nДополнительный контекст для редактирования:\n{edit_context}"
                if active_context
                else edit_context
            )
        plan = planner.plan(
            user_prompt=prompt,
            paragraphs=paragraphs,
            active_context=active_context,
        )
        operations = plan.get("operations", []) if isinstance(plan, dict) else []
        if not isinstance(operations, list):
            operations = []
        summary = str(plan.get("summary") or "").strip() if isinstance(plan, dict) else ""
        planner_source = str(plan.get("planner_source") or "llm").strip() if isinstance(plan, dict) else "llm"
        planner_error = str(plan.get("planner_error") or "").strip() if isinstance(plan, dict) else ""
        raw_plan_result = plan.get("raw_result") if isinstance(plan, dict) else {}
        confidence = plan.get("confidence") if isinstance(plan, dict) else None
        return {
            "plan": plan if isinstance(plan, dict) else {},
            "operations": operations,
            "summary": summary,
            "planner_source": planner_source,
            "planner_error": planner_error,
            "raw_plan_result": raw_plan_result,
            "confidence": confidence,
        }

    def _document_operation_via_docx_patch(
        self,
        *,
        target: dict,
        prompt: str,
        interaction_mode: str,
        document_targets: list[dict],
        doc_publisher: DocEditPublisher,
        edit_context: str = "",
        prebuilt_plan: dict | None = None,
    ) -> dict:
        target_type = str(target.get("type") or "").strip().lower()
        target_id = int(target.get("id") or 0)
        logger.info(
            "document_operation docx patch start: type=%s id=%s",
            target_type,
            target_id,
        )

        try:
            source_bytes, paragraphs = self._read_target_docx_source(target)
        except Exception as exc:
            message = str(exc)
            doc_publisher.error(message)
            return self._doc_op_error(message, document_targets=document_targets)

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "source.docx")
            edited_path = os.path.join(temp_dir, "edited.docx")

            with open(source_path, "wb") as source_stream:
                source_stream.write(source_bytes)

            planned = (
                prebuilt_plan
                if isinstance(prebuilt_plan, dict)
                else self._build_document_edit_plan(
                    prompt=prompt,
                    paragraphs=paragraphs,
                    edit_context=edit_context,
                )
            )
            self._raise_if_cancelled()
            operations = planned["operations"]
            summary = planned["summary"]
            planner_source = planned["planner_source"]
            planner_error = planned["planner_error"]
            raw_plan_result = planned["raw_plan_result"]
            plan = planned["plan"]
            confidence = planned["confidence"]

            logger.info(
                "document_operation planner result: type=%s id=%s source=%s operations=%s confidence=%s error=%s",
                target_type,
                target_id,
                planner_source,
                len(operations),
                confidence,
                planner_error,
            )
            if raw_plan_result:
                logger.info(
                    "document_operation planner raw_result: type=%s id=%s raw=%s",
                    target_type,
                    target_id,
                    raw_plan_result,
                )

            if not operations:
                if not summary or summary == "No document changes are required.":
                    summary = (
                        "Could not build an applicable edit plan for this request. "
                        "Specify exactly what to change (quote/paragraph/insert text)."
                    )
                doc_publisher.error(summary)
                return self._doc_op_error(summary, document_targets=document_targets)

            if interaction_mode == "ask":
                doc_publisher.plan_ready(
                    plan={
                        "summary": summary or "LLM предлагает изменения для документа.",
                        "operations": operations,
                        "confidence": confidence or 0.0,
                    },
                    operations_count=len(operations),
                )
                self._publish(
                    "status",
                    stage="document_operation",
                    message=f"Ожидаю подтверждения пользователя ({len(operations)} операций)...",
                )
                gate = ConfirmationGate(project_id=self.project.id, session_id=self.session_id)
                decision = gate.wait(timeout=120)
                if decision == "timeout":
                    doc_publisher.timeout()
                    return {
                        "mode": "text_response",
                        "response_text": "Время ожидания подтверждения истекло. Изменения не применены.",
                        "search_tags": [],
                        "document_targets": document_targets,
                        "applied_operations": 0,
                    }
                if decision != "confirm":
                    doc_publisher.rejected()
                    return {
                        "mode": "text_response",
                        "response_text": "Изменения отклонены пользователем.",
                        "search_tags": [],
                        "document_targets": document_targets,
                        "applied_operations": 0,
                    }
                doc_publisher.publish("confirmed", message="Подтверждение получено. Применяю правки.")

            self._publish("status", stage="document_operation", message="Применяю правки к документу...")
            doc_publisher.applying(f"Применяю {len(operations)} операций к документу...")
            self._raise_if_cancelled()

            editor = DocumentEditor(source_path)
            applied_count = editor.apply_operations(operations)
            editor.save(edited_path)

            logger.info(
                "document_operation operations applied: type=%s id=%s requested=%s applied=%s",
                target_type,
                target_id,
                len(operations),
                applied_count,
            )

            if applied_count <= 0:
                safe_summary = summary or "Ни одна операция не была применена к документу."
                doc_publisher.done(safe_summary, applied=0)
                return {
                    "mode": "text_response",
                    "response_text": safe_summary,
                    "search_tags": [],
                    "document_targets": document_targets,
                    "applied_operations": 0,
                }

            try:
                edited_bytes = Path(edited_path).read_bytes()
                self._save_target_docx_bytes(target, edited_bytes)
            except Exception as exc:
                message = f"Не удалось сохранить обновлённый DOCX: {exc}"
                doc_publisher.error(message)
                return self._doc_op_error(message, document_targets=document_targets)

        final_summary = summary or (
            f"Документ обновлён. Применено изменений: {applied_count}. "
            "Откройте документ заново, чтобы увидеть обновлённую версию."
        )
        doc_publisher.done(final_summary, applied=applied_count, reload_editor=True)
        return {
            "mode": "document_operation",
            "response_text": final_summary,
            "search_tags": [],
            "document_targets": document_targets,
            "applied_operations": applied_count,
        }

    def _document_operation_via_node_realtime(
        self,
        *,
        target: dict,
        prompt: str,
        interaction_mode: str,
        document_targets: list[dict],
        doc_publisher: DocEditPublisher,
        edit_context: str = "",
    ) -> dict:
        target_id = target.get("id")
        target_type = str(target.get("type") or "").strip().lower()

        logger.info(
            "document_operation live queue start: type=%s id=%s",
            target_type,
            target_id,
        )
        try:
            _source_bytes, paragraphs = self._read_target_docx_source(target)
        except Exception as exc:
            message = str(exc)
            doc_publisher.error(message)
            return self._doc_op_error(message, document_targets=document_targets)

        planned = self._build_document_edit_plan(
            prompt=prompt,
            paragraphs=paragraphs,
            edit_context=edit_context,
        )
        self._raise_if_cancelled()
        operations = planned["operations"]
        summary = planned["summary"]
        planner_source = planned["planner_source"]
        planner_error = planned["planner_error"]
        raw_plan_result = planned["raw_plan_result"]
        confidence = planned["confidence"]

        logger.info(
            "document_operation live planner result: type=%s id=%s source=%s operations=%s confidence=%s error=%s",
            target_type,
            target_id,
            planner_source,
            len(operations),
            confidence,
            planner_error,
        )
        if raw_plan_result:
            logger.info(
                "document_operation live planner raw_result: type=%s id=%s raw=%s",
                target_type,
                target_id,
                raw_plan_result,
            )

        if not operations:
            safe_summary = summary or "Не удалось построить применимый план правок для открытого документа."
            doc_publisher.error(safe_summary)
            return self._doc_op_error(safe_summary, document_targets=document_targets)

        if self._document_operation_engine_for_plan("node_realtime", operations) == "docx_patch":
            logger.info(
                "document_operation live path skipped because plan contains LaTeX formulas: type=%s id=%s",
                target_type,
                target_id,
            )
            return self._document_operation_via_docx_patch(
                target=target,
                prompt=prompt,
                interaction_mode=interaction_mode,
                document_targets=document_targets,
                doc_publisher=doc_publisher,
                edit_context=edit_context,
                prebuilt_plan=planned,
            )

        if interaction_mode == "ask":
            doc_publisher.plan_ready(
                plan={
                    "summary": summary or "LLM предлагает изменения для активного документа.",
                    "operations": operations,
                    "confidence": confidence or 0.0,
                },
                operations_count=len(operations),
            )
            self._publish(
                "status",
                stage="document_operation",
                message=f"Ожидаю подтверждения пользователя ({len(operations)} операций)...",
            )
            gate = ConfirmationGate(project_id=self.project.id, session_id=self.session_id)
            decision = gate.wait(timeout=120)
            if decision == "timeout":
                doc_publisher.timeout()
                return {
                    "mode": "text_response",
                    "response_text": "Время ожидания подтверждения истекло. Изменения не применены.",
                    "search_tags": [],
                    "document_targets": document_targets,
                    "applied_operations": 0,
                }
            if decision != "confirm":
                doc_publisher.rejected()
                return {
                    "mode": "text_response",
                    "response_text": "Изменения отклонены пользователем.",
                    "search_tags": [],
                    "document_targets": document_targets,
                    "applied_operations": 0,
                }
            doc_publisher.publish("confirmed", message="Подтверждение получено. Отправляю правки в live-редактор.")

        self._publish("status", stage="document_operation", message="Ставлю live-правки в очередь agent.js...")
        doc_publisher.applying("Передаю операции в активную OnlyOffice-сессию через agent.js...")
        self._raise_if_cancelled()

        success, error_message, node_result = self._invoke_node_document_insert(
            target=target,
            prompt=prompt,
            operations=operations,
            summary=summary,
        )
        if not success:
            logger.warning(
                "document_operation node failed: type=%s id=%s error=%s result=%s",
                target_type,
                target_id,
                error_message,
                node_result,
            )
            doc_publisher.error(error_message)
            return self._doc_op_error(error_message, document_targets=document_targets)

        logger.info(
            "document_operation node success: type=%s id=%s result=%s",
            target_type,
            target_id,
            node_result,
        )
        applied_operations = len(operations)
        queue_summary = (
            summary
            or f"Правки отправлены в активный редактор для «{target.get('title') or f'документа #{target_id}'}»."
        )
        live_result = {}
        if isinstance(node_result, dict):
            command_id = str(node_result.get("command_id") or "").strip()
            queued_count = node_result.get("queued_commands")
            if command_id:
                live_result = self._wait_node_command_result(command_id, timeout_seconds=30.0)
                queue_summary += f" command_id={command_id}."
            if queued_count is not None:
                queue_summary += f" queued={queued_count}."

        if not live_result:
            message = (
                "Активный редактор не подтвердил применение правок. "
                "Проверьте, что документ открыт в текущей сессии и live-плагин загружен."
            )
            doc_publisher.error(message)
            return self._doc_op_error(message, document_targets=document_targets)

        live_status = str(live_result.get("status") or "").strip().lower()
        if live_status == "applied":
            applied_operations = int(live_result.get("applied") or applied_operations)
            queue_summary = summary or (
                f"Правки применены в активном редакторе для «{target.get('title') or f'документа #{target_id}'}»."
            )
        elif live_status == "partial":
            applied_operations = int(live_result.get("applied") or 0)
            queue_summary = (
                f"{summary or 'Правки частично применены в активном редакторе.'} "
                f"Успешно операций: {applied_operations}."
            ).strip()
        elif live_status == "error":
            error_items = live_result.get("errors") if isinstance(live_result.get("errors"), list) else []
            detail = ""
            if error_items:
                first_error = error_items[0]
                if isinstance(first_error, dict):
                    detail = str(first_error.get("message") or "").strip()
            message = detail or "Live-редактор не смог применить правки."
            doc_publisher.error(message)
            return self._doc_op_error(message, document_targets=document_targets)

        doc_publisher.done(queue_summary, applied=applied_operations)
        return {
            "mode": "document_operation",
            "response_text": queue_summary,
            "search_tags": [],
            "document_targets": document_targets,
            "applied_operations": applied_operations,
        }

    def _invoke_node_document_insert(
        self,
        *,
        target: dict,
        prompt: str,
        operations: list[dict] | None = None,
        summary: str = "",
    ) -> tuple[bool, str, dict]:
        config_url = self._target_onlyoffice_config_url(target)
        if not config_url:
            return False, "Не удалось определить URL конфигурации OnlyOffice.", {}

        node_url = self._node_agent_base_url()
        endpoint = urljoin(f"{node_url}/", "insert")
        user_name = (
            (self.user.get_full_name() or "").strip()
            or (getattr(self.user, "username", "") or "").strip()
            or f"user-{self.user.id}"
        )
        payload = {
            "text": str(prompt or "").strip(),
            "user_id": str(self.user.id),
            "user_name": user_name,
            "app_base_url": self._internal_app_base_url(),
            "config_url": config_url,
            "target_type": str(target.get("type") or "").strip().lower(),
            "target_id": int(target.get("id") or 0),
            "summary": str(summary or "").strip(),
        }
        if isinstance(operations, list) and operations:
            payload["operations"] = operations

        if self.active_doc_key:
            payload["doc_key"] = self.active_doc_key
            # config_url всё равно передаём для получения JWT токена
            payload["config_url"] = config_url
        else:
            payload["config_url"] = config_url
             
        if self.auth_cookie_header:
            payload["config_cookie"] = self.auth_cookie_header

        try:
            response = requests.post(endpoint, json=payload, timeout=90)
        except requests.RequestException as exc:
            return False, f"Не удалось обратиться к agent.js: {exc}", {}

        raw_payload: dict = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                raw_payload = parsed
        except ValueError:
            raw_payload = {}

        if response.status_code >= 400:
            detail = (
                str(raw_payload.get("error") or "").strip()
                or str(raw_payload.get("detail") or "").strip()
                or response.text.strip()
                or f"HTTP {response.status_code}"
            )
            return False, f"agent.js вернул ошибку: {detail}", raw_payload

        success_flag = raw_payload.get("success", True)
        if success_flag is False:
            detail = str(raw_payload.get("error") or "agent.js вернул success=false").strip()
            return False, detail, raw_payload

        return True, "", raw_payload

    def _wait_node_command_result(self, command_id: str, timeout_seconds: float = 20.0) -> dict:
        normalized_command_id = str(command_id or "").strip()
        if not normalized_command_id:
            return {}

        node_url = self._node_agent_base_url()
        endpoint = urljoin(
            f"{node_url}/",
            f"plugins/llm-doc-editor/result?command_id={normalized_command_id}",
        )
        deadline = time.monotonic() + max(float(timeout_seconds or 0), 0.0)

        while time.monotonic() < deadline:
            try:
                response = requests.get(endpoint, timeout=5)
            except requests.RequestException:
                response = None

            payload = {}
            if response is not None:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}

            if response is not None and response.ok and isinstance(payload, dict):
                result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                status = str(result.get("status") or "").strip().lower()
                if status in {"applied", "partial", "error"}:
                    return result

            time.sleep(1.0)

        return {}

    def _serialize_edit_relevant_context(self, relevant_context: dict) -> str:
        publications = relevant_context.get("publications") if isinstance(relevant_context, dict) else []
        projects = relevant_context.get("projects") if isinstance(relevant_context, dict) else []
        payload = {
            "active_context": self._combined_context_for_access(),
            "relevant_publications": publications[:8] if isinstance(publications, list) else [],
            "relevant_projects": projects[:5] if isinstance(projects, list) else [],
        }
        serialized = json.dumps(payload, ensure_ascii=False)
        max_chars = 90000
        if len(serialized) > max_chars:
            serialized = f"{serialized[:max_chars].rstrip()}...[truncated]"
        return serialized

    def _prepare_document_operation_context(self, prompt: str, context=None) -> dict:
        self._raise_if_cancelled()
        combined_context = self._combined_context_for_access()
        scoped_context = context if isinstance(context, dict) else {}
        if not scoped_context:
            scoped_context = self._build_context_for_access_scope()

        if self.access_scope == "active_document":
            scoped_context = {
                "project": scoped_context.get("project"),
                "user": scoped_context.get("user"),
                "documents": {},
                "references": {"total_references": 0, "publications": []},
            }

        project_context = self._project_context_for_prompt(scoped_context)
        external_search_allowed = self._external_search_allowed()
        document_details_allowed = self._document_details_allowed()

        banned_terms = (
            "публикация, публикации, проект, проекты, исследование, исследования, материал, материалы, "
            "работа, работы, статья, статьи, тема, данные, publication, publications, project, projects, "
            "research, article, articles, material, materials, data"
        ) 
        scope_instructions = {
            "active_document": (
                "Область доступа: active_document. "
                "Используй только active_context и текст активного документа. "
                "Не запрашивай файлы проекта, связанные документы или внешний поиск."
            ),
            "active_project": (
                "Область доступа: active_project. "
                "Используй project_context, active_context и документы проекта. "
                "Не запрашивай внешний или глобальный поиск."
            ),
            "full_access": (
                "Область доступа: full_access. "
                "Используй project_context и active_context. "
                "Разрешён внешний семантический поиск, если без него нельзя безопасно подготовить контекст для редактирования."
            ),
        }

        document_details_instruction = (
            "Если перед редактированием необходимо сначала определить конкретные файлы проекта или публикации, "
            "верни mode='document_details' и заполни document_targets.\n"
            if document_details_allowed
            else "Режим 'document_details' запрещён для этого запроса.\n"
        )

        system_prompt = (
            "Ты подготавливаешь контекст для редактирования активного академического DOCX-документа. "
            "Верни только JSON без пояснений, комментариев и любого дополнительного текста.\n"
            "На этом этапе не создавай операции редактирования и не предлагай изменения текста документа. "
            "Нужно только определить, достаточно ли текущего контекста для безопасного и обоснованного редактирования.\n"
            "Верни mode='ready', если текущего активного или проектного контекста достаточно для выполнения запрошенного редактирования.\n"
            "Верни mode='more_context', если для безопасного редактирования требуется дополнительный предметный контекст.\n"
            + document_details_instruction +
            "Для mode='more_context' поле search_tags должно содержать только узкие предметные термины, "
            "непосредственно относящиеся к теме запроса.\n"
            "Не включай в search_tags общие, расплывчатые или служебные слова.\n"
            f"Запрещённые общие/сущностные слова: {banned_terms}.\n"
            "Запрещённые служебные или UI-слова: context, search, json, status, success, search_again, "
            "user_query, keywords, ui, sidebar, file, files.\n"
            "search_tags должны быть короткими, точными и тематическими. "
            "Они не должны описывать тип сущности, интерфейс, формат ответа или сам процесс поиска.\n"
            "Если mode='ready' или mode='document_details', поле search_tags должно быть пустым массивом.\n"
            "Если контекст уже достаточен, не запрашивай дополнительный поиск.\n"
            "Если контекст недостаточен, запрашивай только тот минимум предметных терминов, который действительно нужен.\n"
            + scope_instructions.get(self.access_scope, scope_instructions["active_project"])
        )

        schema_hint = """
        {
            "mode": "ready | more_context | document_details",
            "confidence": 0.0,
            "response_text": "string",
            "search_tags": ["string"],
            "document_targets": [{"id": 1, "type": "project | publication_file | auto"}],
            "language": "string"
        }
        """.strip()

        model = self.get_model(task_type="document_operation")
        self.llm_extraction.change_model(model)

        result = self.llm_extraction.send_json_request(
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        "Проанализируй, достаточно ли предоставленного контекста для безопасной подготовки редактирования документа.\n\n"
                        "Правила:\n"
                        "1. Верни mode='ready', если контекста достаточно, чтобы обоснованно подготовить план редактирования.\n"
                        "2. Верни mode='more_context', если текущего контекста недостаточно.\n"
                        "3. Верни mode='document_details', если для подготовки редактирования необходимо получить содержимое конкретных файлов проекта.\n"
                        "4. Не придумывай факты, термины, источники, выводы или связи, которых нет в предоставленном контексте.\n"
                        "5. Каждый элемент document_targets должен иметь формат: {\"id\": int, \"type\": \"project | publication_file | auto\"}.\n"
                        "6. Для document_targets используй только id из списка документов, переданного в project_context.\n"
                        "7. Поле search_tags должно содержать от 2 до 8 узких тематических терминов.\n"
                        "8. Каждый элемент search_tags должен состоять из 1-4 слов.\n"
                        "9. search_tags должны описывать предметный смысл запроса, а не типы сущностей, документов, файлов или интерфейсных объектов.\n"
                        f"10. Запрещённые слова в search_tags: {banned_terms}.\n"
                        "11. Если mode='ready' или mode='document_details', то search_tags должен быть пустым массивом [].\n"
                        "12. Если mode='more_context', поле response_text должно кратко объяснять, какого именно контекста не хватает.\n"
                        f"13. Область доступа: {self.access_scope}.\n"
                        + (
                            "14. Если требуется полный текст одного или нескольких файлов, ты можешь вернуть mode='document_details' и указать от 1 до 8 элементов в document_targets.\n"
                            if document_details_allowed
                            else "14. Режим 'document_details' запрещён. Возвращай только 'ready' или 'more_context'.\n"
                        )
                        + (
                            "15. Внешний семантический поиск разрешён только при mode='more_context'.\n"
                            if external_search_allowed
                            else "15. Внешний семантический поиск запрещён для этого запроса.\n"
                        )
                        + "16. Не запрашивай дополнительный контекст, если уже можно безопасно принять решение.\n"
                        "17. При выборе mode='more_context' указывай только минимально необходимый набор search_tags.\n"
                        "18. Не включай в search_tags общие слова, дубли, служебные формулировки и слова из запроса, которые не несут предметного смысла.\n"
                        "19. Ответ должен быть строго в JSON и только в пределах ожидаемой схемы.\n"
                        "\n"
                        f"Контекст проекта (JSON, сокращённый):\n{project_context}\n\n"
                        f"Запрос пользователя на редактирование:\n{prompt}\n\n"
                        f"{combined_context or 'Контекст отсутствует.'}\n"
                    ),
                },
            ],
            schema_hint=schema_hint,
            temperature=0,
        )
        self._raise_if_cancelled()
        normalized_initial = self._normalize_text_result(result)
        mode = normalized_initial["mode"]
        print(f"CONTEXT RESULT: {result}")

        if mode in {"ready", "text_response"}:
            return {
                "status": "ready",
                "edit_context": combined_context,
                "document_targets": [],
            }

        if mode == "document_details":
            if not document_details_allowed:
                return {
                    "status": "blocked",
                    "response": {
                        "mode": "more_context",
                        "response_text": normalized_initial.get("response_text") or self._scope_restriction_message(),
                        "search_tags": _prepare_unique_tags(normalized_initial.get("search_tags") or []),
                        "confidence": normalized_initial.get("confidence"),
                        "language": normalized_initial.get("language"),
                        "relevant_publication_ids": [],
                        "relevant_project_ids": [],
                        "document_targets": [],
                    },
                }

            requested_targets = normalized_initial.get("document_targets") or []
            self._publish(
                "status",
                stage="document_details",
                message="Загружаю выбранные файлы для подготовки правок...",
            )
            detailed_payload = self.context_builder.collect_detailed_documents(
                targets=requested_targets,
                active_document_id=self.document_id,
                active_publication_file_id=self.publication_file_id,
                text_limit=600000,
            )
            self._raise_if_cancelled()
            documents = detailed_payload.get("documents") if isinstance(detailed_payload, dict) else []
            if not isinstance(documents, list) or not documents:
                return {
                    "status": "blocked",
                    "response": {
                        "mode": "more_context",
                        "response_text": normalized_initial.get("response_text")
                        or "Не удалось загрузить выбранные файлы для подготовки правок.",
                        "search_tags": normalized_initial.get("search_tags") or [],
                        "confidence": normalized_initial.get("confidence"),
                        "language": normalized_initial.get("language"),
                        "relevant_publication_ids": [],
                        "relevant_project_ids": [],
                        "document_targets": _prepare_document_targets(requested_targets),
                    },
                }

            self._publish(
                "status",
                stage="document_details",
                message=f"Детально загружено файлов: {len(documents)}. Планирую правки...",
            )
            return {
                "status": "ready",
                "edit_context": self._document_details_context_for_prompt(detailed_payload),
                "document_targets": _prepare_document_targets(requested_targets),
            }

        search_tags = _prepare_unique_tags(normalized_initial.get("search_tags") or [])
        if not search_tags:
            search_tags = _filter_new_tags(
                _derive_fallback_search_tags(prompt, combined_context, limit=8),
                set(),
            )

        if not external_search_allowed:
            return {
                "status": "blocked",
                "response": {
                    "mode": "more_context",
                    "response_text": normalized_initial.get("response_text") or self._scope_restriction_message(),
                    "search_tags": _prepare_unique_tags(search_tags),
                    "confidence": normalized_initial.get("confidence"),
                    "language": normalized_initial.get("language"),
                    "relevant_publication_ids": [],
                    "relevant_project_ids": [],
                    "document_targets": [],
                },
            }

        if not search_tags:
            return {
                "status": "blocked",
                "response": {
                    "mode": "more_context",
                    "response_text": normalized_initial.get("response_text")
                    or "Недостаточно данных для подготовки правок. Уточните предметную область запроса.",
                    "search_tags": [],
                    "confidence": normalized_initial.get("confidence"),
                    "language": normalized_initial.get("language"),
                    "relevant_publication_ids": [],
                    "relevant_project_ids": [],
                    "document_targets": [],
                },
            }

        self._publish(
            "status",
            stage="context_search",
            message="Ищу релевантные публикации и проекты для подготовки правок...",
        )
        collected_context = self.collect_context(
            prompt=prompt,
            keywords=search_tags,
            document_context=combined_context,
        )
        self._raise_if_cancelled()
        if collected_context.get("status") != "success":
            fallback_text = normalized_initial.get("response_text") or "Недостаточно данных для подготовки правок."
            fallback_tags = _prepare_unique_tags(
                collected_context.get("search_tags")
                if isinstance(collected_context.get("search_tags"), list)
                else search_tags
            )
            return {
                "status": "blocked",
                "response": {
                    "mode": "more_context",
                    "response_text": fallback_text,
                    "search_tags": fallback_tags,
                    "confidence": collected_context.get("confidence", normalized_initial.get("confidence")),
                    "language": normalized_initial.get("language"),
                    "relevant_publication_ids": [],
                    "relevant_project_ids": [],
                    "document_targets": [],
                },
            }

        relevant_context = self._collect_relevant_entities(
            collected_context.get("publications"),
            collected_context.get("projects"),
        )
        if not relevant_context["publications"] and not relevant_context["projects"]:
            return {
                "status": "blocked",
                "response": {
                    "mode": "more_context",
                    "response_text": normalized_initial.get("response_text")
                    or "Не удалось найти достаточно релевантных данных для подготовки правок.",
                    "search_tags": _prepare_unique_tags(search_tags),
                    "confidence": collected_context.get("confidence", normalized_initial.get("confidence")),
                    "language": normalized_initial.get("language"),
                    "relevant_publication_ids": [],
                    "relevant_project_ids": [],
                    "document_targets": [],
                },
            }

        self._publish(
            "status",
            stage="context_search",
            message="Найден релевантный контекст. Планирую правки документа...",
        )
        return {
            "status": "ready",
            "edit_context": self._serialize_edit_relevant_context(relevant_context),
            "relevant_publication_ids": [
                item.get("id") for item in relevant_context.get("publications", []) if item.get("id")
            ],
            "relevant_project_ids": [
                item.get("id") for item in relevant_context.get("projects", []) if item.get("id")
            ],
            "document_targets": [],
        }
            
    def document_operation(self):
        prompt = str(getattr(self, "_last_prompt", "") or "").strip()
        interaction_mode = str(self.interaction_mode or "ask").strip().lower()
        operation_engine = self._document_operation_engine()
        self._raise_if_cancelled()

        target = self._resolve_document_operation_target()
        if not target:
            return self._doc_op_error("Нет активного документа для редактирования.")

        document_targets = self._document_operation_target_payload(target)
        target_type = str(target.get("type") or "").strip().lower()
        target_id = target.get("id")
        logger.info(
            "document_operation target resolved: type=%s id=%s session=%s",
            target_type,
            target_id,
            self.session_id,
        )
        logger.info(
            "document_operation engine selected: %s",
            operation_engine,
        )
        doc_publisher = DocEditPublisher(project_id=self.project.id, session_id=self.session_id)

        if not target.get("can_edit"):
            message = "Нет прав на редактирование активного документа."
            doc_publisher.error(message)
            return self._doc_op_error(message, document_targets=document_targets)

        if not target.get("is_docx"):
            return {
                "mode": "text_response",
                "response_text": "Автоматическое редактирование поддерживается только для DOCX.",
                "search_tags": [],
                "document_targets": document_targets,
            }

        document_key = str(target.get("document_key") or "").strip()
        if not document_key:
            message = "Не удалось получить ключ документа OnlyOffice."
            doc_publisher.error(message)
            return self._doc_op_error(message, document_targets=document_targets)

        if not prompt:
            message = "Пустой запрос на редактирование."
            doc_publisher.error(message)
            return self._doc_op_error(message, document_targets=document_targets)
        self._raise_if_cancelled()

        context_preflight = self._prepare_document_operation_context(
            prompt,
            context=getattr(self, "_last_context", None),
        )
        print(f"CONTEXT FOR ANSWER: {context_preflight}")
        self._raise_if_cancelled()
        if context_preflight.get("status") != "ready":
            response = context_preflight.get("response")
            if isinstance(response, dict):
                return response
            return {
                "mode": "more_context",
                "response_text": "Недостаточно контекста для подготовки правок документа.",
                "search_tags": [],
                "relevant_publication_ids": [],
                "relevant_project_ids": [],
                "document_targets": [],
            }
        edit_context = str(context_preflight.get("edit_context") or "").strip()
        extra_document_targets = _prepare_document_targets(context_preflight.get("document_targets"))
        if extra_document_targets:
            document_targets = document_targets + [
                item for item in extra_document_targets if item not in document_targets
            ]

        if operation_engine == "node_realtime":
            return self._document_operation_via_node_realtime(
                target=target,
                prompt=prompt,
                interaction_mode=interaction_mode,
                document_targets=document_targets,
                doc_publisher=doc_publisher,
                edit_context=edit_context,
            )

        return self._document_operation_via_docx_patch(
            target=target,
            prompt=prompt,
            interaction_mode=interaction_mode,
            document_targets=document_targets,
            doc_publisher=doc_publisher,
            edit_context=edit_context,
        )

    def _doc_op_error(self, message: str, document_targets: list | None = None) -> dict:
        return {
            "mode": "error",
            "response_text": str(message or "").strip(),
            "search_tags": [],
            "document_targets": document_targets or [],
        }

    def _publish(self, event_type, **payload):
        try:
            self.progress_publisher.publish(event_type, **payload)
        except Exception as exc:
            print("Failed to publish websocket event:", exc)

    @staticmethod
    def _normalize_text_result(result):
        if not isinstance(result, dict):
            return {
                "mode": "error",
                "response_text": "",
                "search_tags": [],
                "relevant_publication_ids": [],
                "relevant_project_ids": [],
                "document_targets": [],
            }
        return {
            "mode": str(result.get("mode") or "").strip(),
            "response_text": str(result.get("response_text") or "").strip(),
            "search_tags": _prepare_unique_tags(result.get("search_tags") if isinstance(result.get("search_tags"), list) else []),
            "confidence": result.get("confidence"),
            "language": result.get("language"),
            "relevant_publication_ids": _prepare_unique_ids(result.get("relevant_publication_ids")),
            "relevant_project_ids": _prepare_unique_ids(result.get("relevant_project_ids")),
            "document_targets": _prepare_document_targets(
                result.get("document_targets") if isinstance(result.get("document_targets"), list) else []
            ),
            "raw_result": result,
        }

    @staticmethod
    def _trim_prompt_text(value, max_len=400):
        text = " ".join(str(value or "").split()).strip()
        if len(text) <= max_len:
            return text
        return f"{text[:max_len].rstrip()}..."

    def _project_context_for_prompt(self, context) -> str:
        if not isinstance(context, dict):
            return "{}"

        project_block = context.get("project") if isinstance(context.get("project"), dict) else {}
        user_block = context.get("user") if isinstance(context.get("user"), dict) else {}
        documents = context.get("documents") if isinstance(context.get("documents"), dict) else {}
        references = context.get("references") if isinstance(context.get("references"), dict) else {}

        normalized_documents = []
        document_items = [item for item in documents.values() if isinstance(item, dict)]
        document_items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        for item in document_items[:5]:
            source_type = str(item.get("source_type") or "project_document").strip().lower()
            target_type = "publication_file" if "publication_file" in source_type else "project"
            normalized_documents.append(
                {
                    "id": item.get("id"),
                    "title": self._trim_prompt_text(item.get("title"), max_len=180),
                    "file_type": str(item.get("file_type") or "").strip().lower(),
                    "source_type": source_type,
                    "target_type": target_type,
                    "version": item.get("version"),
                    "updated_at": item.get("updated_at"),
                    "content_excerpt": self._trim_prompt_text(item.get("content"), max_len=1200),
                }
            )

        reference_publications = []
        for publication in (references.get("publications") or [])[:4]:
            if not isinstance(publication, dict):
                continue

            normalized_reference_items = []
            for reference in (publication.get("references") or [])[:4]:
                if not isinstance(reference, dict):
                    continue
                linked_publication = reference.get("referenced_publication")
                linked_publication_id = linked_publication.get("id") if isinstance(linked_publication, dict) else None
                normalized_reference_items.append(
                    {
                        "order": reference.get("order"),
                        "display_text": self._trim_prompt_text(reference.get("display_text"), max_len=320),
                        "linked_publication_id": linked_publication_id,
                    }
                )

            reference_publications.append(
                {
                    "publication_id": publication.get("publication_id"),
                    "title": self._trim_prompt_text(publication.get("title"), max_len=180),
                    "year": publication.get("year"),
                    "references": normalized_reference_items,
                }
            )

        total_references = references.get("total_references", 0)
        try:
            total_references = int(total_references)
        except (TypeError, ValueError):
            total_references = 0

        prompt_context_payload = {
            "project": {
                "id": project_block.get("id"),
                "title": self._trim_prompt_text(project_block.get("title"), max_len=200),
            },
            "user": {
                "id": user_block.get("id"),
                "username": self._trim_prompt_text(user_block.get("username"), max_len=80),
            },
            "documents": normalized_documents,
            "references": {
                "total_references": max(0, total_references),
                "publications": reference_publications,
            },
        }

        serialized = json.dumps(prompt_context_payload, ensure_ascii=False)
        max_chars = 24000
        if len(serialized) > max_chars:
            serialized = f"{serialized[:max_chars].rstrip()}...[truncated]"
        return serialized

    def _document_details_context_for_prompt(self, detailed_payload) -> str:
        if not isinstance(detailed_payload, dict):
            return "{}"

        documents = detailed_payload.get("documents") if isinstance(detailed_payload.get("documents"), list) else []
        requested_targets = (
            detailed_payload.get("requested_targets")
            if isinstance(detailed_payload.get("requested_targets"), list)
            else []
        )

        normalized_documents = []
        for item in documents[:8]:
            if not isinstance(item, dict):
                continue
            normalized_documents.append(
                {
                    "id": item.get("id"),
                    "type": str(item.get("type") or "").strip().lower(),
                    "title": self._trim_prompt_text(item.get("title"), max_len=220),
                    "file_type": str(item.get("file_type") or "").strip().lower(),
                    "reason": str(item.get("reason") or "").strip().lower(),
                    "content": self._trim_prompt_text(item.get("content"), max_len=18000),
                }
            )

        payload = {
            "requested_targets": requested_targets[:12],
            "documents": normalized_documents,
            "active_context": self._combined_context_for_access(),
        }
        serialized = json.dumps(payload, ensure_ascii=False)
        max_chars = 120000
        if len(serialized) > max_chars:
            serialized = f"{serialized[:max_chars].rstrip()}...[truncated]"
        return serialized

    def _generate_response_from_document_details(self, prompt: str, detailed_payload: dict, base_result: dict) -> dict:
        schema_hint = """
        {
            "mode": "text_response | more_context",
            "response_text": "string",
            "search_tags": ["string"],
            "confidence": 0.0,
            "language": "string"
        }
        """.strip()

        details_context = self._document_details_context_for_prompt(detailed_payload)

        result = self.llm_extraction.send_json_request(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты академический ассистент. "
                        "Тебе передан детальный контекст выбранных файлов и активного файла. "
                        "Сформируй ответ строго в JSON.\n"
                        "Важно: не используй project_context и не ссылайся на данные вне переданного detailed context.\n"
                        "Если данных достаточно — mode='text_response'. "
                        "Если недостаточно — mode='more_context' и верни предметные search_tags."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Ответь на запрос пользователя, используя только detailed context ниже.\n\n"
                        f"User query:\n{prompt}\n\n"
                        f"Detailed context:\n{details_context}\n"
                    ),
                },
            ],
            schema_hint=schema_hint,
            temperature=0.15,
        )

        normalized = self._normalize_text_result(result)
        if normalized["mode"] not in {"text_response", "more_context"}:
            normalized["mode"] = "text_response"
        if normalized["mode"] == "text_response":
            normalized["search_tags"] = []

        if not normalized["response_text"]:
            normalized["response_text"] = base_result.get("response_text") or "Не удалось сформировать ответ по выбранным файлам."

        normalized["document_targets"] = _prepare_document_targets(
            detailed_payload.get("requested_targets") if isinstance(detailed_payload.get("requested_targets"), list) else []
        )
        return normalized

    def _resolve_document_details_mode(self, prompt: str, normalized_initial: dict) -> dict:
        if not self._document_details_allowed():
            return {
                "mode": "more_context",
                "response_text": normalized_initial.get("response_text") or self._scope_restriction_message(),
                "search_tags": _prepare_unique_tags(normalized_initial.get("search_tags") or []),
                "confidence": normalized_initial.get("confidence"),
                "language": normalized_initial.get("language"),
                "relevant_publication_ids": [],
                "relevant_project_ids": [],
                "document_targets": [],
            }

        requested_targets = normalized_initial.get("document_targets") or []
        self._publish(
            "status",
            stage="document_details",
            message="Загружаю выбранные файлы для детального анализа...",
        )
        detailed_payload = self.context_builder.collect_detailed_documents(
            targets=requested_targets,
            active_document_id=self.document_id,
            active_publication_file_id=self.publication_file_id,
            text_limit=120000,
        )

        documents = detailed_payload.get("documents") if isinstance(detailed_payload, dict) else []
        if not isinstance(documents, list) or not documents:
            return {
                "mode": "more_context",
                "response_text": normalized_initial.get("response_text")
                or "Не удалось загрузить выбранные файлы для детального анализа.",
                "search_tags": normalized_initial.get("search_tags") or [],
                "confidence": normalized_initial.get("confidence"),
                "language": normalized_initial.get("language"),
                "relevant_publication_ids": [],
                "relevant_project_ids": [],
                "document_targets": _prepare_document_targets(requested_targets),
            }

        self._publish(
            "status",
            stage="document_details",
            message=f"Детально загружено файлов: {len(documents)}. Формирую ответ...",
        )
        return self._generate_response_from_document_details(
            prompt=prompt,
            detailed_payload=detailed_payload,
            base_result=normalized_initial,
        )

    def text_message(self, prompt, context=None):
        self._raise_if_cancelled()
        combined_context = self._combined_context_for_access()
        scoped_context = context if isinstance(context, dict) else {}
        if self.access_scope == "active_document":
            scoped_context = {
                "project": scoped_context.get("project"),
                "user": scoped_context.get("user"),
                "documents": {},
                "references": {"total_references": 0, "publications": []},
            }
        project_context = self._project_context_for_prompt(scoped_context)
        external_search_allowed = self._external_search_allowed()
        document_details_allowed = self._document_details_allowed()

        banned_terms = (
            "публикация, публикации, проект, проекты, исследование, исследования, материал, материалы, "
            "работа, работы, статья, статьи, тема, данные, publication, publications, project, projects, "
            "research, article, articles, material, materials, data"
        )
        scope_instructions = {
            "active_document": (
                "Access scope is active_document: use only active_context. "
                "Do not rely on project_context and do not request external search."
            ),
            "active_project": (
                "Access scope is active_project: use only project_context and active_context. "
                "Do not request external/global search."
            ),
            "full_access": (
                "Access scope is full_access: use project_context and active_context, and external semantic search is allowed."
            ),
        }
        document_details_instruction = (
            "If you need detailed file contents to answer, return mode='document_details' and provide document_targets.\n"
            if document_details_allowed
            else "Mode 'document_details' is not allowed for this request. Use only text_response or more_context.\n"
        )
        system_prompt = (
            "You are an academic assistant for project knowledge retrieval. Return JSON only.\n"
            "If context is sufficient, return mode='text_response'. "
            "If context is insufficient, return mode='more_context'.\n"
            + document_details_instruction
            + "For mode='more_context', search_tags must contain only domain-specific terms for narrowing search.\n"
            + "search_tags must focus on subject area, methods, technologies, standards, tools, and close synonyms.\n"
            + f"Forbidden generic/entity words: {banned_terms}.\n"
            + "Forbidden service or UI words: context, search, json, status, success, search_again, user_query, keywords, ui, sidebar, file, files.\n"
            + "Do not repeat words from template instructions.\n"
            + "No duplicates in search_tags.\n"
            + "If mode='text_response', search_tags must be an empty array.\n"
            + "If mode='document_details', search_tags must be an empty array.\n"
            + scope_instructions.get(self.access_scope, scope_instructions["active_project"])
        )

        schema_hint = """
        {
            "mode": "text_response | more_context | document_details",
            "confidence": 0.0,
            "response_text": "string",
            "search_tags": ["string"],
            "document_targets": [{"id": 1, "type": "project | publication_file | auto"}],
            "language": "string"
        }
        """.strip()

        model = self.get_model(task_type="text_message")
        self.llm_extraction.change_model(model)

        result = self.llm_extraction.send_json_request(
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        "Analyze user query and available context.\n\n"
                        "Rules:\n"
                        "1. If context is enough, return mode='text_response'.\n"
                        "2. If context is not enough, return mode='more_context'.\n"
                        "3. Do not guess facts that are absent in context.\n"
                        "4. document_targets item format: {\"id\": int, \"type\": \"project | publication_file | auto\"}.\n"
                        "5. Use ids only from the provided documents list in project context.\n"
                        "6. For mode='text_response' and mode='more_context', document_targets = [].\n"
                        "7. search_tags must be 2-8 focused terms, each 1-4 words.\n"
                        "8. search_tags must be about domain meaning, not about entity types.\n"
                        f"9. Forbidden words in search_tags: {banned_terms}.\n"
                        "10. Forbidden UI/service words: context, search, json, status, success, search_again, user_query, keywords, ui, sidebar, file, files.\n"
                        "11. If mode='text_response' or mode='document_details', search_tags = [].\n"
                        "12. If mode='more_context', response_text briefly explains what is missing.\n"
                        f"13. Access scope: {self.access_scope}.\n"
                        + (
                            "14. If full file content is required, you may return mode='document_details' with 1-8 targets.\n"
                            if document_details_allowed
                            else "14. Mode='document_details' is forbidden. Return only text_response or more_context.\n"
                        )
                        + (
                            "15. External semantic search is allowed only when mode='more_context'.\n"
                            if external_search_allowed
                            else "15. External semantic search is forbidden for this request.\n"
                        )
                        + "\n"
                        f"Контекст проекта (JSON, сокращённый):\n{project_context}\n\n"
                        f"User query:\n{prompt}\n\n"
                        f"{combined_context or 'Контекст отсутствует.'}\n"
                    ),
                },
            ],
            schema_hint=schema_hint,
            temperature=0.15,
        )
        self._raise_if_cancelled()
        normalized_initial = self._normalize_text_result(result)

        if normalized_initial["mode"] == "document_details":
            if not document_details_allowed:
                return {
                    "mode": "more_context",
                    "response_text": normalized_initial.get("response_text") or self._scope_restriction_message(),
                    "search_tags": _prepare_unique_tags(normalized_initial.get("search_tags") or []),
                    "confidence": normalized_initial.get("confidence"),
                    "language": normalized_initial.get("language"),
                    "relevant_publication_ids": [],
                    "relevant_project_ids": [],
                    "document_targets": [],
                }
            return self._resolve_document_details_mode(prompt, normalized_initial)
        if normalized_initial["mode"] == "text_response":
            normalized_initial["document_targets"] = []
            return normalized_initial

        search_tags = _prepare_unique_tags(normalized_initial.get("search_tags") or [])
        if not search_tags:
            search_tags = _filter_new_tags(
                _derive_fallback_search_tags(prompt, combined_context, limit=8),
                set(),
            )

        if not external_search_allowed:
            return {
                "mode": "more_context",
                "response_text": normalized_initial.get("response_text")
                or self._scope_restriction_message(),
                "search_tags": _prepare_unique_tags(search_tags),
                "confidence": normalized_initial.get("confidence"),
                "language": normalized_initial.get("language"),
                "relevant_publication_ids": [],
                "relevant_project_ids": [],
                "document_targets": [],
            }

        if not search_tags:
            return {
                "mode": "more_context",
                "response_text": normalized_initial.get("response_text")
                or "Недостаточно данных для уверенного ответа. Уточните предметную область запроса.",
                "search_tags": [],
                "confidence": normalized_initial.get("confidence"),
                "language": normalized_initial.get("language"),
                "relevant_publication_ids": [],
                "relevant_project_ids": [],
                "document_targets": [],
            }

        self._publish(
            "status",
            stage="context_search",
            message="Ищу релевантные публикации и проекты...",
        )
        self._raise_if_cancelled()
        collected_context = self.collect_context(
            prompt=prompt,
            keywords=search_tags,
            document_context=combined_context,
        )
        self._raise_if_cancelled()
        if collected_context.get("status") != "success":
            fallback_text = normalized_initial.get("response_text") or "Недостаточно данных для уверенного ответа."
            fallback_tags = _prepare_unique_tags(
                collected_context.get("search_tags") if isinstance(collected_context.get("search_tags"), list) else search_tags
            )
            return {
                "mode": "more_context",
                "response_text": fallback_text,
                "search_tags": fallback_tags,
                "confidence": collected_context.get("confidence", normalized_initial.get("confidence")),
                "language": normalized_initial.get("language"),
                "relevant_publication_ids": [],
                "relevant_project_ids": [],
                "document_targets": [],
            }

        relevant_context = self._collect_relevant_entities(
            collected_context.get("publications"),
            collected_context.get("projects"),
        )
        if not relevant_context["publications"] and not relevant_context["projects"]:
            return {
                "mode": "more_context",
                "response_text": normalized_initial.get("response_text") or "Не удалось найти достаточно релевантных данных.",
                "search_tags": _prepare_unique_tags(search_tags),
                "confidence": collected_context.get("confidence", normalized_initial.get("confidence")),
                "language": normalized_initial.get("language"),
                "relevant_publication_ids": [],
                "relevant_project_ids": [],
                "document_targets": [],
            }

        self._publish(
            "status",
            stage="answer_generation",
            message="Найден релевантный контекст, формирую ответ...",
        )
        return self._generate_response_from_context(prompt, relevant_context)

    def collect_context(self, prompt, keywords, document_context=""):
        max_attempts = max(1, int(getattr(self, "search_again_attempt_count", 1)))
        self._raise_if_cancelled()

        initial_keywords = _prepare_unique_tags(keywords if isinstance(keywords, list) else [keywords])
        if not initial_keywords:
            initial_keywords = _prepare_unique_tags(_derive_fallback_search_tags(prompt, document_context, limit=8))
        current_keywords = initial_keywords[:]

        used_tags_normalized = {_normalize_search_tag(tag) for tag in current_keywords}
        attempts_history = []

        banned_terms = (
            "публикация, публикации, проект, проекты, исследование, исследования, материал, материалы, "
            "работа, работы, статья, статьи, тема, данные, publication, publications, project, projects, "
            "research, article, articles, material, materials, data"
        )
        system_prompt = (
            "Ты выполняешь semantic matching между запросом пользователя и объектами базы данных.\n"
            "Нужно отобрать только релевантные publications и projects.\n\n"
            "Правила:\n"
            "- сравнивай по смыслу, тематике, ключевым словам и области применения;\n"
            "- учитывай document_context как дополнительный контекст, если он передан;\n"
            "- не включай нерелевантные элементы;\n"
            "- отсортируй результаты по убыванию релевантности;\n"
            "- выбирай только действительно подходящие объекты;\n"
            "- если релевантных данных недостаточно, верни status='search_again' и сформируй search_tags для повторного поиска;\n"
            "- search_tags должны состоять только из предметных тематических терминов из сути запроса;\n"
            "- search_tags должны отражать тему, технологии, методы, предметную область, синонимы и близкие термины;\n"
            f"- запрещено добавлять слишком общие слова и типы сущностей, такие как: {banned_terms};\n"
            "- запрещено добавлять служебные слова UI и шаблона: context, search, json, status, success, search_again, user_query, keywords, database_result, used_keywords, ui, sidebar, file, files;\n"
            "- запрещено повторять слова из used_keywords и повторять теги между собой;\n"
            "- запрещено добавлять слова, которые не сужают поиск по смыслу;\n"
            "- search_tags должны быть полезны для поиска в базе и сужать выборку;\n"
            "- если status='success', search_tags должен быть пустым массивом;\n"
            "- если status='search_again', publications и projects должны быть пустыми или почти пустыми;\n"
            "- верни только JSON без пояснений."
        )

        model = self.model_map.get(self.thinking_mode)
        self.llm_extraction.change_model(model)

        last_response = {
            "status": "search_again",
            "confidence": 0.0,
            "publications": [],
            "projects": [],
            "search_tags": [],
        }

        for attempt_index in range(max_attempts):
            self._raise_if_cancelled()
            if not current_keywords:
                break

            self._publish(
                "status",
                stage="context_search",
                message=f"Ищу материалы по ключевым словам: {', '.join(current_keywords[:5])}",
            )
            db_result = trim_db_result(search_from_db(current_keywords))

            payload = {
                "user_query": prompt,
                "keywords": current_keywords,
                "used_keywords": sorted(used_tags_normalized),
                "document_context": document_context or "",
                "database_result": db_result,
                "attempt": attempt_index + 1,
                "max_attempts": max_attempts,
            }

            user_content = json.dumps(payload, ensure_ascii=False)

            schema_hint = """
            {
                "status": "success | search_again",
                "confidence": 0.0,
                "publications": [1, 2, 3],
                "projects": [10, 11],
                "search_tags": ["string"]
            }
            """.strip()

            raw_response = self.llm_extraction.send_json_request(
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_content,
                    },
                ],
                schema_hint=schema_hint,
                temperature=0.0,
            )
            self._raise_if_cancelled()

            response = _safe_llm_response(raw_response)
            last_response = response

            attempts_history.append(
                {
                    "attempt": attempt_index + 1,
                    "keywords": current_keywords[:],
                    "response": response,
                }
            )

            has_results = bool(response["publications"] or response["projects"])

            if response["status"] == "success" and has_results:
                response["attempts_history"] = attempts_history
                response["used_keywords"] = sorted(used_tags_normalized)
                return response

            new_tags = _filter_new_tags(response["search_tags"], used_tags_normalized)
            if not new_tags:
                fallback_candidates = _prepare_unique_tags(
                    _derive_fallback_search_tags(
                        prompt,
                        document_context,
                        " ".join(current_keywords),
                        " ".join(response.get("search_tags") or []),
                        exclude_normalized=used_tags_normalized,
                        limit=8,
                    )
                )
                new_tags = _filter_new_tags(fallback_candidates, used_tags_normalized)

            if not new_tags:
                break

            for tag in new_tags:
                used_tags_normalized.add(_normalize_search_tag(tag))

            current_keywords = new_tags

        fallback_tags = _prepare_unique_tags(
            last_response.get("search_tags") if isinstance(last_response.get("search_tags"), list) else []
        )
        if not fallback_tags:
            fallback_tags = _filter_new_tags(
                _prepare_unique_tags(
                    _derive_fallback_search_tags(
                        prompt,
                        document_context,
                        exclude_normalized=used_tags_normalized,
                        limit=8,
                    )
                ),
                used_tags_normalized,
            )

        return {
            "status": "not_found",
            "confidence": last_response.get("confidence", 0.0),
            "publications": [],
            "projects": [],
            "search_tags": fallback_tags,
            "used_keywords": sorted(used_tags_normalized),
            "attempts_history": attempts_history,
        }

    def handle(self, prompt):
        self._last_prompt = str(prompt or "")
        try:
            self._raise_if_cancelled()
            self._publish(
                "status",
                stage="context_building",
                message="Собираю контекст документа...",
            )
            context = self._build_context_for_access_scope()
            self._last_context = context
            self._raise_if_cancelled()

            self._publish(
                "status",
                stage="intent_detection",
                message="Думаю",
            )

            model = self.get_model(task_type="first_prompt")
            self.llm_extraction.change_model(model)

            result = self.llm_extraction.send_json_request(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Определи тип пользовательского запроса.\n\n"
                            "Нужно вернуть только классификацию:\n"
                            "- text_message: во всех случаях, где цель НЕ является прямым редактированием конкретного файла/документа.\n"
                            "  Сюда относятся: вопросы по проекту, поиск/подбор материалов, поиск публикаций и проектов, "
                            "сборка контекста, анализ, рекомендации, предложение search_tags, поиск по другим файлам проекта, "
                            "добавление файлов в проект, добавление публикаций, добавление PublicationReference из внешних источников.\n"
                            "- document_operation: ТОЛЬКО когда пользователь явно просит изменить текст конкретного документа/файла "
                            "(исправить, переписать, сократить, дополнить раздел, отредактировать содержимое).\n\n"
                            "Правило при сомнении: выбирай text_message.\n"
                            "Верни только результат классификации.\n\n"
                            f"Уровень доступа: {self.access_scope}.\n"
                            f"Объединённый активный контекст: {self._combined_context_for_access()} возможно будет полезен для определения типа запроса.\n\n"
                            "User request:\n"
                            f"{prompt}"
                        ),
                    }
                ],
                schema_hint="""
                {
                    "intent": "text_message | document_operation",
                    "confidence": float_from_0.0_to_1.0,
                    "reason": "string",
                    "language": "string"
                }
                """.strip(),
            )
            self._raise_if_cancelled()
            self._publish(
                "status",
                stage="intent_detection",
                message=result.get("reason", "Не удалось определить тип запроса"),
            )

            if result.get("intent") == "text_message":
                self._raise_if_cancelled()
                text_result = self._normalize_text_result(self.text_message(prompt, context))
                return text_result

            if result.get("intent") == "document_operation":
                self._raise_if_cancelled()
                doc_result = self.document_operation()
                return doc_result

            unknown_message = "Не удалось определить тип запроса."
            print("Неизвестный intent. Результат классификации:", result)
            self._publish("error", message=unknown_message)
            return {
                "mode": "error",
                "response_text": unknown_message,
                "search_tags": [],
                "document_targets": [],
            }
        except Exception as exc:
            raw_error = str(exc).strip()
            if "LLM модель не отвечает" in raw_error:
                error_message = raw_error
            else:
                error_message = f"Ошибка обработки запроса: {raw_error or 'Неизвестная ошибка'}"
            self._publish("error", message=error_message)
            return {
                "mode": "error",
                "response_text": error_message,
                "search_tags": [],
                "document_targets": [],
            }
