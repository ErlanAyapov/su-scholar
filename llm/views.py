from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import uuid
import zipfile
from urllib.parse import urlencode
from xml.etree import ElementTree

import requests
from xml.sax.saxutils import escape

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core import signing
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from document.models import Document, DocumentPermission
from document.views import (
    ONLYOFFICE_ERROR_STATUSES,
    ONLYOFFICE_SAVE_STATUSES,
    _absolute_url,
    _append_project_agent_plugin_config,
    _build_onlyoffice_config_payload,
    _jwt_decode,
    _jwt_secret,
    _onlyoffice_file_params,
)
from llm.agent.ws_confirm_gate import ConfirmationGate
from llm.agent.ws_publisher import AgentProgressPublisher
from llm.models import ChatSession, ChatShareImport, ChatShareLink, Message
from main.models import Language, Project, Publication, PublicationFile, PublicationProject, PublicationReference, PublicationType, Venue
from llm.agent.orchestrator import ProjectAgentOrchestrator

logger = logging.getLogger(__name__)
User = get_user_model()
PROJECT_PUBLICATION_FILE_TOKEN_SALT = "project-publication-file-access"
PROJECT_PUBLICATION_ONLYOFFICE_EXTENSIONS = {
    "docx",
    "doc",
    "odt",
    "rtf",
    "xlsx",
    "xls",
    "ods",
    "csv",
    "pptx",
    "ppt",
    "odp",
    "txt",
}
PROJECT_PUBLICATION_IFRAME_EXTENSIONS = {"pdf"}
PROJECT_AGENT_CANCEL_CACHE_PREFIX = "project_agent_cancel"


def _project_agent_cancel_cache_key(task_id: str) -> str:
    return f"{PROJECT_AGENT_CANCEL_CACHE_PREFIX}:{str(task_id or '').strip()}"

@require_GET
def llm_page(request):
    return render(request, "llm/main.html")


def _resolve_owned_session(user, session_id: int | None) -> ChatSession | None:
    if not user.is_authenticated:
        return None
    if not session_id:
        return None
    return ChatSession.objects.filter(id=session_id, user=user).first()


def _parse_session_id(raw_value) -> int | None:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _normalize_agent_access_scope(raw_value) -> str:
    value = str(raw_value or "").strip().lower()
    if value in {"active_document", "active_project", "full_access"}:
        return value
    if value in {"high", "full", "all", "global"}:
        return "full_access"
    return "active_project"


def _normalize_agent_interaction_mode(raw_value) -> str:
    value = str(raw_value or "").strip().lower()
    if value in {"autonomous", "ask"}:
        return value
    return "ask"


@login_required
@require_POST
def llm_share_create(request):
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}

    session_id = _parse_session_id(payload.get("session_id"))
    chat_session = _resolve_owned_session(request.user, session_id)
    if not chat_session:
        return JsonResponse({"detail": "Chat session not found"}, status=404)

    now = timezone.now()
    share_link = (
        ChatShareLink.objects.filter(
            session=chat_session,
            created_by=request.user,
            is_active=True,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )
    if not share_link:
        share_link = ChatShareLink.objects.create(
            session=chat_session,
            created_by=request.user,
        )

    share_url = request.build_absolute_uri(
        reverse("llm_shared_chat", kwargs={"token": share_link.token}),
    )
    return JsonResponse(
        {
            "share_url": share_url,
            "expires_at": share_link.expires_at.isoformat() if share_link.expires_at else None,
        }
    )


def _clone_shared_session_for_user(chat_session: ChatSession, user) -> ChatSession:
    copied_title = f"Shared: {chat_session.title or ChatSession.DEFAULT_TITLE}"[:500]
    cloned_session = ChatSession.objects.create(
        user=user,
        title=copied_title,
    )
    source_messages = list(chat_session.messages.order_by("created", "id"))
    if source_messages:
        Message.objects.bulk_create(
            [
                Message(
                    chat=cloned_session,
                    body=item.body,
                    sended_from=item.sended_from,
                )
                for item in source_messages
            ]
        )
    return cloned_session


@require_GET
def llm_shared_chat(request, token: str):
    token_value = str(token or "").strip()
    if not token_value:
        raise Http404("Shared chat not found")

    share_link = (
        ChatShareLink.objects.select_related("session", "session__user")
        .prefetch_related("session__messages")
        .filter(token=token_value, is_active=True)
        .first()
    )
    if not share_link or share_link.is_expired:
        raise Http404("Shared chat not found")

    share_link.last_opened_at = timezone.now()
    share_link.save(update_fields=["last_opened_at"])

    if request.user.is_authenticated:
        source_session = share_link.session
        if request.user.id == source_session.user_id:
            return redirect(f"{reverse('llm_page')}?session={source_session.id}")

        with transaction.atomic():
            imported = (
                ChatShareImport.objects.select_related("session")
                .filter(share_link=share_link, user=request.user)
                .first()
            )
            if imported:
                target_session = imported.session
            else:
                target_session = _clone_shared_session_for_user(source_session, request.user)
                ChatShareImport.objects.create(
                    share_link=share_link,
                    user=request.user,
                    session=target_session,
                )

        return redirect(f"{reverse('llm_page')}?session={target_session.id}")

    read_only_messages = list(share_link.session.messages.order_by("created", "id"))
    return render(
        request,
        "llm/shared_chat.html",
        {
            "source_session": share_link.session,
            "messages": read_only_messages,
            "share_link": share_link,
        },
    )


PROJECT_FILE_KIND_MAP = {
    "docx": {"extension": "docx", "document_file_type": "docx", "default_title": "Р В Р’В Р РЋРЎС™Р В Р’В Р РЋРІР‚СћР В Р’В Р В РІР‚В Р В Р Р‹Р Р†Р вЂљРІвЂћвЂ“Р В Р’В Р Р†РІР‚С›РІР‚вЂњ Р В Р’В Р СћРІР‚ВР В Р’В Р РЋРІР‚СћР В Р’В Р РЋРІР‚СњР В Р Р‹Р РЋРІР‚СљР В Р’В Р РЋР’ВР В Р’В Р вЂ™Р’ВµР В Р’В Р В РІР‚В¦Р В Р Р‹Р Р†Р вЂљРЎв„ў"},
    "xlsx": {"extension": "xlsx", "document_file_type": "excel", "default_title": "Р В Р’В Р РЋРЎС™Р В Р’В Р РЋРІР‚СћР В Р’В Р В РІР‚В Р В Р’В Р вЂ™Р’В°Р В Р Р‹Р В Р РЏ Р В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р вЂ™Р’В°Р В Р’В Р вЂ™Р’В±Р В Р’В Р вЂ™Р’В»Р В Р’В Р РЋРІР‚ВР В Р Р‹Р Р†Р вЂљР’В Р В Р’В Р вЂ™Р’В°"},
    "pptx": {"extension": "pptx", "document_file_type": "pptx", "default_title": "Р В Р’В Р РЋРЎС™Р В Р’В Р РЋРІР‚СћР В Р’В Р В РІР‚В Р В Р’В Р вЂ™Р’В°Р В Р Р‹Р В Р РЏ Р В Р’В Р РЋРІР‚вЂќР В Р Р‹Р В РІР‚С™Р В Р’В Р вЂ™Р’ВµР В Р’В Р вЂ™Р’В·Р В Р’В Р вЂ™Р’ВµР В Р’В Р В РІР‚В¦Р В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р вЂ™Р’В°Р В Р Р‹Р Р†Р вЂљР’В Р В Р’В Р РЋРІР‚ВР В Р Р‹Р В Р РЏ"},
    "txt": {"extension": "txt", "document_file_type": "txt", "default_title": "Р В Р’В Р РЋРЎС™Р В Р’В Р РЋРІР‚СћР В Р’В Р В РІР‚В Р В Р’В Р вЂ™Р’В°Р В Р Р‹Р В Р РЏ Р В Р’В Р вЂ™Р’В·Р В Р’В Р вЂ™Р’В°Р В Р’В Р РЋР’ВР В Р’В Р вЂ™Р’ВµР В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р РЋРІР‚СњР В Р’В Р вЂ™Р’В°"},
}

PROJECT_MEMBER_PERMISSION_DEFAULTS = {
    "can_view": True,
    "can_edit": True,
    "can_comment": True,
    "can_review": True,
    "can_download": True,
    "can_print": True,
}


def _project_queryset_for_user(user):
    if not user or not user.is_authenticated:
        return Project.objects.none()
    return Project.objects.filter(Q(owner=user) | Q(collaborators=user)).distinct()


def _project_for_user_or_404(user, project_id: int) -> Project:
    project = (
        _project_queryset_for_user(user)
        .select_related("owner")
        .prefetch_related("collaborators")
        .filter(id=project_id)
        .first()
    )
    if not project:
        raise Http404("Project not found")
    return project


def _project_members(project: Project) -> list[dict]:
    members: list[dict] = []
    seen_ids: set[int] = set()
    if project.owner_id and project.owner:
        members.append({"user": project.owner, "role": "Owner"})
        seen_ids.add(project.owner_id)
    for collaborator in project.collaborators.all().order_by("last_name", "first_name", "username", "id"):
        if collaborator.id in seen_ids:
            continue
        members.append({"user": collaborator, "role": "Collaborator"})
        seen_ids.add(collaborator.id)
    return members


def _project_documents(project: Project):
    return (
        project.documents.filter(is_deleted=False)
        .select_related("user")
        .order_by("-updated_at", "-id")
    )


def _project_visible_publications_queryset():
    return Publication.objects.filter(private=False)


def _project_publication_for_project(project: Project, publication_id: int) -> Publication | None:
    if publication_id <= 0:
        return None
    return (
        _project_visible_publications_queryset()
        .filter(publicationproject__project=project, id=publication_id)
        .select_related("created_by", "pub_type", "language", "venue")
        .first()
    )


def _project_user_can_manage_publication(user, publication: Publication | None) -> bool:
    if not user or not user.is_authenticated or not publication:
        return False
    return publication.created_by_id == user.id and not publication.private


def _normalize_positive_int_list(values, limit: int = 8) -> list[int]:
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
        if len(result) >= limit:
            break
    return result


def _project_reference_suggestions_payload(
    project: Project,
    user,
    target_publication: Publication | None,
    publication_ids,
) -> list[dict]:
    ordered_ids = _normalize_positive_int_list(publication_ids, limit=8)
    if not ordered_ids:
        return []

    publication_map = {
        publication.id: publication
        for publication in (
            _project_visible_publications_queryset()
            .filter(id__in=ordered_ids)
            .select_related("venue", "created_by")
        )
    }
    target_publication_id = target_publication.id if target_publication else None
    existing_reference_ids = set()
    if target_publication_id:
        existing_reference_ids = set(
            PublicationReference.objects.filter(
                publication=target_publication,
                referenced_publication_id__in=ordered_ids,
            ).values_list("referenced_publication_id", flat=True)
        )

    can_manage_target = _project_user_can_manage_publication(user, target_publication)
    suggestions = []
    for rank, publication_id in enumerate(ordered_ids, start=1):
        publication = publication_map.get(publication_id)
        if not publication:
            continue
        if target_publication_id and publication.id == target_publication_id:
            continue

        already_added = publication.id in existing_reference_ids
        can_add_reference = bool(target_publication_id and can_manage_target and not already_added)
        disabled_reason = ""
        if already_added:
            disabled_reason = "Уже добавлено в список литературы."
        elif not target_publication_id:
            disabled_reason = "Выберите публикацию проекта, чтобы добавить reference."
        elif not can_manage_target:
            disabled_reason = "Добавлять references можно только в свои публичные публикации."

        suggestions.append(
            {
                "id": publication.id,
                "rank": rank,
                "title": publication.title_original,
                "year": publication.year,
                "venue": publication.venue.name if publication.venue_id and publication.venue else "",
                "abstract": (publication.abstract or "")[:700],
                "detail_url": reverse("publication_detail", kwargs={"pk": publication.id}),
                "already_added": already_added,
                "can_add_reference": can_add_reference,
                "disabled_reason": disabled_reason,
                "target_publication_id": target_publication_id,
            }
        )

    return suggestions


def _project_publication_links(project: Project):
    publication_files = PublicationFile.objects.order_by("kind", "id")
    publication_references = (
        PublicationReference.objects.select_related("referenced_publication", "referenced_publication__venue")
        .order_by("order", "id")
    )
    return (
        PublicationProject.objects.filter(project=project, publication__private=False)
        .select_related("publication", "publication__created_by")
        .prefetch_related(
            Prefetch("publication__files", queryset=publication_files),
            Prefetch("publication__reference_entries", queryset=publication_references),
        )
        .order_by("-publication__year", "publication__title_original", "id")
    )


def _project_publication_types():
    publication_types = list(PublicationType.objects.order_by("name", "id"))
    if publication_types:
        return publication_types
    fallback_type, _ = PublicationType.objects.get_or_create(name="Article")
    return [fallback_type]


def _project_publication_languages():
    languages = list(Language.objects.order_by("name", "id"))
    if languages:
        return languages
    fallback_language, _ = Language.objects.get_or_create(code="ru", defaults={"name": "Russian"})
    return [fallback_language]


def _project_publication_candidates(project: Project, limit: int = 80):
    linked_publication_ids = PublicationProject.objects.filter(project=project).values_list("publication_id", flat=True)
    return (
        _project_visible_publications_queryset().select_related("pub_type", "language", "venue", "created_by")
        .exclude(id__in=linked_publication_ids)
        .order_by("-updated_at", "-id")[:limit]
    )


def _project_publication_file_queryset(project: Project):
    return (
        PublicationFile.objects.filter(publication__publicationproject__project=project, publication__private=False)
        .select_related("publication", "publication__created_by")
        .distinct()
    )


def _project_publication_file_for_project(project: Project, file_id: int) -> PublicationFile | None:
    if file_id <= 0:
        return None
    return _project_publication_file_queryset(project).filter(id=file_id).first()


def _project_publication_sections(
    project: Project,
    *,
    user=None,
    selected_publication_id: int | None = None,
    selected_publication_file_id: int | None = None,
):
    sections: list[dict] = []
    first_publication_file: PublicationFile | None = None

    for link in _project_publication_links(project):
        publication = link.publication
        files = list(publication.files.all())
        references = []
        for reference in publication.reference_entries.all():
            referenced_publication = reference.referenced_publication
            if referenced_publication and referenced_publication.private:
                continue
            references.append(reference)
        if not first_publication_file and files:
            first_publication_file = files[0]
        sections.append(
            {
                "publication": publication,
                "files": files,
                "references": references,
                "can_manage": _project_user_can_manage_publication(user, publication),
                "is_open": bool(
                    (selected_publication_id and publication.id == selected_publication_id)
                    or (
                        selected_publication_file_id
                        and any(item.id == selected_publication_file_id for item in files)
                    )
                ),
            }
        )

    return sections, first_publication_file


def _project_publication_file_groups(publication_sections):
    groups = []
    total = 0
    for item in publication_sections or []:
        files = list(item.get("files") or [])
        if not files:
            continue
        total += len(files)
        groups.append(
            {
                "publication": item.get("publication"),
                "files": files,
                "can_manage": bool(item.get("can_manage")),
            }
        )
    return groups, total


def _project_publication_type_from_request(request) -> PublicationType:
    raw_value = (request.POST.get("pub_type_id") or "").strip()
    if raw_value.isdigit():
        publication_type = PublicationType.objects.filter(id=int(raw_value)).first()
        if publication_type:
            return publication_type
    return _project_publication_types()[0]


def _project_publication_language_from_request(request) -> Language:
    raw_value = (request.POST.get("language_id") or "").strip()
    if raw_value.isdigit():
        language = Language.objects.filter(id=int(raw_value)).first()
        if language:
            return language
    return _project_publication_languages()[0]


def _project_publication_year_from_request(request) -> int:
    raw_value = (request.POST.get("year") or "").strip()
    current_year = timezone.now().year
    if raw_value.isdigit():
        year = int(raw_value)
        if 1900 <= year <= current_year + 1:
            return year
    return current_year


def _project_publication_venue_from_request(request) -> Venue:
    venue_name = (request.POST.get("venue") or "").strip()[:300]
    if not venue_name:
        venue_name = "Unknown venue"
    existing = Venue.objects.filter(name__iexact=venue_name).first()
    if existing:
        return existing
    return Venue.objects.create(name=venue_name)


def _project_publication_file_extension(publication_file: PublicationFile) -> str:
    return os.path.splitext(publication_file.file.name or "")[1].lower().lstrip(".")


def _project_publication_file_mode(publication_file: PublicationFile) -> str:
    extension = _project_publication_file_extension(publication_file)
    if extension in PROJECT_PUBLICATION_IFRAME_EXTENSIONS:
        return "iframe"
    if extension in PROJECT_PUBLICATION_ONLYOFFICE_EXTENSIONS:
        return "onlyoffice"
    return "download"


def _project_publication_file_permissions(publication_file: PublicationFile, user) -> dict:
    can_edit = (
        _project_user_can_manage_publication(user, publication_file.publication)
        and _project_publication_file_mode(publication_file) == "onlyoffice"
    )
    return {
        "can_view": True,
        "can_edit": can_edit,
        "can_comment": can_edit,
        "can_review": can_edit,
        "can_download": True,
        "can_print": True,
    }


def _project_publication_file_key(project: Project, publication_file: PublicationFile) -> str:
    file_size = 0
    if publication_file.file:
        try:
            file_size = publication_file.file.size
        except OSError:
            file_size = 0
    publication_updated_at = 0
    if publication_file.publication and publication_file.publication.updated_at:
        publication_updated_at = int(publication_file.publication.updated_at.timestamp())
    return f"project-{project.id}-publication-file-{publication_file.id}-u{publication_updated_at}-s{file_size}"


def _project_publication_file_access_token(project: Project, publication_file: PublicationFile, user) -> str:
    payload = {
        "project_id": project.id,
        "publication_file_id": publication_file.id,
        "user_id": user.id,
    }
    return signing.dumps(payload, salt=PROJECT_PUBLICATION_FILE_TOKEN_SALT, compress=True)


def _validate_project_publication_file_access_token(
    token: str,
    project: Project,
    publication_file: PublicationFile,
) -> bool:
    try:
        payload = signing.loads(token, salt=PROJECT_PUBLICATION_FILE_TOKEN_SALT, max_age=60 * 60 * 12)
    except signing.BadSignature:
        return False
    except signing.SignatureExpired:
        return False

    if payload.get("project_id") != project.id:
        return False
    if payload.get("publication_file_id") != publication_file.id:
        return False

    user_id = payload.get("user_id")
    if not user_id:
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        return False

    return _project_queryset_for_user(user).filter(id=project.id).exists()


def _build_project_publication_file_onlyoffice_config(
    request,
    project: Project,
    publication_file: PublicationFile,
) -> dict:
    file_token = _project_publication_file_access_token(project, publication_file, request.user)
    file_url = _absolute_url(
        request,
        f"{reverse('project_publication_file_stream', kwargs={'project_id': project.id, 'file_id': publication_file.id})}?{urlencode({'access_token': file_token})}",
    )
    callback_url = _absolute_url(
        request,
        reverse(
            "project_publication_file_onlyoffice_callback",
            kwargs={"project_id": project.id, "file_id": publication_file.id},
        ),
    )
    user_name = request.user.get_full_name() or request.user.username or f"user-{request.user.pk}"
    file_name = publication_file.file.name if publication_file.file else ""
    document_type, file_type = _onlyoffice_file_params(file_name, fallback_file_type="pdf")
    title = os.path.basename(file_name) if file_name else (
        publication_file.description
        or publication_file.publication.title_original
        or f"publication-file-{publication_file.id}"
    )

    key = _project_publication_file_key(project, publication_file)
    config = _build_onlyoffice_config_payload(
        file_url=file_url,
        callback_url=callback_url,
        title=title,
        file_type=file_type,
        document_type=document_type,
        key=key,
        permissions=_project_publication_file_permissions(publication_file, request.user),
        user_id=request.user.pk,
        user_name=user_name,
    )
    return _append_project_agent_plugin_config(
        request,
        config,
        document_key=key,
        target_type="publication_file",
        target_id=publication_file.id,
    )


def _build_publication_file_name(label: str, extension: str) -> str:
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    base_name = slugify(label) or "publication-file"
    return f"{base_name}-{timestamp}.{extension}"


def _publication_file_kind_from_request(request, file_name: str = "") -> str:
    requested_kind = (request.POST.get("kind") or "").strip().lower()
    valid_kinds = {choice[0] for choice in PublicationFile.FILE_KIND_CHOICES}
    extension = os.path.splitext(file_name or "")[1].lower().lstrip(".")
    if extension == "pdf":
        return "pdf"
    if requested_kind == "pdf":
        return "other"
    if requested_kind in valid_kinds:
        return requested_kind
    return "other"


def _touch_publication(publication: Publication) -> None:
    publication.updated_at = timezone.now()
    publication.save(update_fields=["updated_at"])


def _save_publication_file_content(publication_file: PublicationFile, raw_bytes: bytes) -> None:
    extension = _project_publication_file_extension(publication_file) or "docx"
    file_name = _build_publication_file_name(
        publication_file.description or publication_file.publication.title_original or f"publication-file-{publication_file.id}",
        extension,
    )
    if publication_file.file:
        publication_file.file.delete(save=False)
    publication_file.file.save(file_name, ContentFile(raw_bytes), save=False)


def _sync_project_document_permissions(project: Project, document: Document) -> None:
    member_ids = set(project.collaborators.values_list("id", flat=True))
    if project.owner_id:
        member_ids.add(project.owner_id)
    member_ids.discard(document.user_id)
    for user_id in member_ids:
        DocumentPermission.objects.update_or_create(
            document=document,
            user_id=user_id,
            defaults=PROJECT_MEMBER_PERMISSION_DEFAULTS,
        )


def _build_project_file_name(title: str, extension: str) -> str:
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    base_name = slugify(title) or "project-file"
    return f"{base_name}-{timestamp}.{extension}"


def _build_minimal_docx_bytes(text: str) -> bytes:
    content = escape((text or "").strip() or "New document")
    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""
    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:dcterms="http://purl.org/dc/terms/"
                   xmlns:dcmitype="http://purl.org/dc/dcmitype/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{content}</dc:title>
  <dc:creator>SU Scholar</dc:creator>
  <cp:lastModifiedBy>SU Scholar</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ")}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ")}</dcterms:modified>
</cp:coreProperties>
"""
    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
            xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>SU Scholar</Application>
</Properties>
"""
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t xml:space="preserve">{content}</w:t></w:r></w:p>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
</w:styles>
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", styles_xml)
        archive.writestr("word/_rels/document.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>""")
    return buffer.getvalue()


def _build_minimal_xlsx_bytes(text: str) -> bytes:
    content = escape((text or "").strip() or "Sheet1")
    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""
    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    workbook_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="inlineStr"><is><t>{content}</t></is></c></row>
  </sheetData>
</worksheet>
"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("docProps/core.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Workbook</dc:title></cp:coreProperties>""")
        archive.writestr("docProps/app.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>SU Scholar</Application></Properties>""")
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        archive.writestr("xl/styles.xml", styles_xml)
    return buffer.getvalue()


def _build_minimal_pptx_bytes(text: str) -> bytes:
    content = escape((text or "").strip() or "New Presentation")
    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  <Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>
  <Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>
  <Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>
</Types>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""
    presentation_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>
  <p:sldSz cx="9144000" cy="6858000" type="screen4x3"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>
"""
    presentation_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps" Target="presProps.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/viewProps" Target="viewProps.xml"/>
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles" Target="tableStyles.xml"/>
</Relationships>
"""
    slide_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{content}</a:t></a:r></a:p></p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>
"""
    slide_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
"""
    slide_layout_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
             xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             type="title" preserve="1">
  <p:cSld name="Title Slide">
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>
"""
    slide_layout_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>
"""
    slide_master_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
             xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld name="">
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
</p:sldMaster>
"""
    slide_master_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>
"""
    theme_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">
  <a:themeElements>
    <a:clrScheme name="Office">
      <a:dk1><a:srgbClr val="000000"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="1F497D"/></a:dk2><a:lt2><a:srgbClr val="EEECE1"/></a:lt2>
      <a:accent1><a:srgbClr val="4F81BD"/></a:accent1><a:accent2><a:srgbClr val="C0504D"/></a:accent2>
      <a:accent3><a:srgbClr val="9BBB59"/></a:accent3><a:accent4><a:srgbClr val="8064A2"/></a:accent4>
      <a:accent5><a:srgbClr val="4BACC6"/></a:accent5><a:accent6><a:srgbClr val="F79646"/></a:accent6>
      <a:hlink><a:srgbClr val="0000FF"/></a:hlink><a:folHlink><a:srgbClr val="800080"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Office"><a:majorFont/><a:minorFont/></a:fontScheme>
    <a:fmtScheme name="Office"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme>
  </a:themeElements>
</a:theme>
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("docProps/core.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Presentation</dc:title></cp:coreProperties>""")
        archive.writestr("docProps/app.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>SU Scholar</Application></Properties>""")
        archive.writestr("ppt/presentation.xml", presentation_xml)
        archive.writestr("ppt/_rels/presentation.xml.rels", presentation_rels_xml)
        archive.writestr("ppt/slides/slide1.xml", slide_xml)
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", slide_rels_xml)
        archive.writestr("ppt/slideLayouts/slideLayout1.xml", slide_layout_xml)
        archive.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", slide_layout_rels_xml)
        archive.writestr("ppt/slideMasters/slideMaster1.xml", slide_master_xml)
        archive.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", slide_master_rels_xml)
        archive.writestr("ppt/theme/theme1.xml", theme_xml)
        archive.writestr("ppt/presProps.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentationPr xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>""")
        archive.writestr("ppt/viewProps.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:viewPr xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:normalViewPr/></p:viewPr>""")
        archive.writestr("ppt/tableStyles.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>""")
    return buffer.getvalue()


def _build_project_file_payload(file_kind: str, title: str) -> tuple[str, str, bytes, str]:
    config = PROJECT_FILE_KIND_MAP[file_kind]
    extension = config["extension"]
    document_file_type = config["document_file_type"]
    if file_kind == "docx":
        return extension, document_file_type, _build_minimal_docx_bytes(title), title
    if file_kind == "xlsx":
        return extension, document_file_type, _build_minimal_xlsx_bytes(title), ""
    if file_kind == "pptx":
        return extension, document_file_type, _build_minimal_pptx_bytes(title), ""
    return extension, document_file_type, (title or "").encode("utf-8"), title


def _document_file_type_from_name(file_name: str) -> str:
    extension = os.path.splitext(file_name or "")[1].lower().lstrip(".")
    if extension in {"docx", "doc", "odt", "rtf"}:
        return "docx"
    if extension in {"xlsx", "xls", "ods", "csv"}:
        return "excel"
    if extension in {"pptx", "ppt", "odp"}:
        return "pptx"
    if extension == "txt":
        return "txt"
    if extension == "pdf":
        return "pdf"
    return "other"


def _onlyoffice_api_js_url() -> str:
    origin = (settings.DOCK_EDITOR_URL or "").strip().rstrip("/")
    if not origin:
        return ""
    return f"{origin}/web-apps/apps/api/documents/api.js"


PROJECT_AGENT_MESSAGES_LIMIT = 120
PROJECT_AGENT_DOCUMENT_CHARS_LIMIT = 200000
PROJECT_AGENT_PREVIEW_CHARS = 3200
PROJECT_AGENT_OPERATIONS_LIMIT = 12
PROJECT_AGENT_EDITABLE_FILE_TYPES = {"docx", "txt"}
PROJECT_AGENT_READ_KEYWORDS = (
    "Р В Р’В Р РЋРІР‚вЂќР В Р Р‹Р В РІР‚С™Р В Р’В Р РЋРІР‚СћР В Р Р‹Р Р†Р вЂљР Р‹Р В Р’В Р РЋРІР‚ВР В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р вЂ™Р’В°Р В Р’В Р Р†РІР‚С›РІР‚вЂњ",
    "Р В Р’В Р РЋРІР‚вЂќР В Р’В Р РЋРІР‚СћР В Р’В Р РЋРІР‚СњР В Р’В Р вЂ™Р’В°Р В Р’В Р вЂ™Р’В¶Р В Р’В Р РЋРІР‚В",
    "Р В Р Р‹Р В РЎвЂњР В Р’В Р РЋРІР‚СћР В Р’В Р СћРІР‚ВР В Р’В Р вЂ™Р’ВµР В Р Р‹Р В РІР‚С™Р В Р’В Р вЂ™Р’В¶Р В Р’В Р РЋРІР‚ВР В Р’В Р РЋР’ВР В Р’В Р РЋРІР‚СћР В Р’В Р вЂ™Р’Вµ",
    "Р В Р Р‹Р Р†Р вЂљР Р‹Р В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р РЋРІР‚Сћ Р В Р’В Р В РІР‚В  Р В Р’В Р СћРІР‚ВР В Р’В Р РЋРІР‚СћР В Р’В Р РЋРІР‚СњР В Р Р‹Р РЋРІР‚СљР В Р’В Р РЋР’ВР В Р’В Р вЂ™Р’ВµР В Р’В Р В РІР‚В¦Р В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р вЂ™Р’Вµ",
    "Р В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р вЂ™Р’ВµР В Р’В Р РЋРІР‚СњР В Р Р‹Р В РЎвЂњР В Р Р‹Р Р†Р вЂљРЎв„ў Р В Р’В Р СћРІР‚ВР В Р’В Р РЋРІР‚СћР В Р’В Р РЋРІР‚СњР В Р Р‹Р РЋРІР‚СљР В Р’В Р РЋР’ВР В Р’В Р вЂ™Р’ВµР В Р’В Р В РІР‚В¦Р В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р вЂ™Р’В°",
    "read",
    "show text",
    "show document",
    "document text",
)
PROJECT_AGENT_EDIT_KEYWORDS = (
    "Р В Р’В Р РЋРІР‚ВР В Р Р‹Р В РЎвЂњР В Р’В Р РЋРІР‚вЂќР В Р Р‹Р В РІР‚С™Р В Р’В Р вЂ™Р’В°Р В Р’В Р В РІР‚В ",
    "Р В Р Р‹Р В РІР‚С™Р В Р’В Р вЂ™Р’ВµР В Р’В Р СћРІР‚ВР В Р’В Р вЂ™Р’В°Р В Р’В Р РЋРІР‚СњР В Р Р‹Р Р†Р вЂљРЎв„ў",
    "Р В Р’В Р В РІР‚В Р В Р’В Р В РІР‚В¦Р В Р’В Р вЂ™Р’ВµР В Р Р‹Р В РЎвЂњР В Р’В Р РЋРІР‚В",
    "Р В Р’В Р СћРІР‚ВР В Р’В Р РЋРІР‚СћР В Р’В Р вЂ™Р’В±Р В Р’В Р вЂ™Р’В°Р В Р’В Р В РІР‚В ",
    "Р В Р’В Р вЂ™Р’В·Р В Р’В Р вЂ™Р’В°Р В Р’В Р РЋР’ВР В Р’В Р вЂ™Р’ВµР В Р’В Р В РІР‚В¦Р В Р’В Р РЋРІР‚В",
    "Р В Р Р‹Р РЋРІР‚СљР В Р’В Р СћРІР‚ВР В Р’В Р вЂ™Р’В°Р В Р’В Р вЂ™Р’В»Р В Р’В Р РЋРІР‚В",
    "Р В Р’В Р РЋРІР‚вЂќР В Р’В Р вЂ™Р’ВµР В Р Р‹Р В РІР‚С™Р В Р’В Р вЂ™Р’ВµР В Р’В Р РЋРІР‚вЂќР В Р’В Р РЋРІР‚ВР В Р Р‹Р Р†РІР‚С™Р’В¬Р В Р’В Р РЋРІР‚В",
    "edit document",
    "update document",
    "patch",
    "diff",
)
PROJECT_AGENT_HUNK_RE = re.compile(r"^@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@")
PROJECT_AGENT_PATCH_BLOCK_RE = re.compile(r"```(?:diff|patch)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
PROJECT_AGENT_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

def _project_agent_session_for_user(
    user,
    project: Project,
    *,
    session_id: int | None = None,
    create: bool = False,
    title_hint: str = "",
) -> ChatSession | None:
    queryset = ChatSession.objects.filter(user=user, project=project).order_by("-updated", "-id")
    session = queryset.filter(id=session_id).first() if session_id else queryset.first()
    if session_id and not session:
        session = queryset.first()
    if session or not create:
        return session

    title = ChatSession.title_from_prompt(title_hint) if title_hint else ChatSession.DEFAULT_TITLE
    return ChatSession.objects.create(
        user=user,
        project=project,
        title=title,
    )


def _serialize_project_agent_message(message: Message) -> dict:
    return {
        "id": message.id,
        "role": "assistant" if message.sended_from == Message.Sender.BOT else "user",
        "content": message.body,
        "created": message.created.isoformat(),
    }


def _project_agent_messages(session: ChatSession) -> list[dict]:
    messages = session.messages.order_by("created", "id")[:PROJECT_AGENT_MESSAGES_LIMIT]
    return [_serialize_project_agent_message(message) for message in messages]


def _resolve_project_document(project: Project, document_id: int | None) -> Document | None:
    queryset = _project_documents(project)
    if document_id:
        match = queryset.filter(id=document_id).first()
        if match:
            return match
    return queryset.first()


def _project_agent_document_payload(document: Document | None) -> dict:
    if not document:
        return {
            "id": None,
            "title": "",
            "file_type": "",
            "version": None,
            "updated_at": None,
            "edit_url": "",
        }
    return {
        "id": document.id,
        "title": document.title,
        "file_type": document.file_type,
        "version": document.version,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
        "edit_url": reverse("document_edit", kwargs={"pk": document.id}),
    }


def _can_edit_project_document(user, document: Document) -> bool:
    if not user or not user.is_authenticated:
        return False
    if document.user_id == user.id:
        return True
    permission = DocumentPermission.objects.filter(document=document, user=user).only("can_edit").first()
    return bool(permission and permission.can_edit)


def _read_docx_text_bytes(raw_bytes: bytes) -> str:
    if not raw_bytes:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            document_xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile):
        return ""

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError:
        return ""

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        chunks: list[str] = []
        for chunk in paragraph.findall(".//w:t", namespace):
            if chunk.text:
                chunks.append(chunk.text)
        if chunks:
            paragraphs.append("".join(chunks))
    return "\n".join(paragraphs).strip()


def _read_document_text(document: Document) -> str:
    fallback = str(document.content or "").strip()
    if not document.file:
        return fallback[:PROJECT_AGENT_DOCUMENT_CHARS_LIMIT]

    try:
        with document.file.open("rb") as stream:
            raw_bytes = stream.read()
    except OSError:
        return fallback[:PROJECT_AGENT_DOCUMENT_CHARS_LIMIT]

    file_type = (document.file_type or "").lower()
    if file_type == "txt":
        try:
            return raw_bytes.decode("utf-8").strip()[:PROJECT_AGENT_DOCUMENT_CHARS_LIMIT]
        except UnicodeDecodeError:
            return raw_bytes.decode("utf-8", errors="replace").strip()[:PROJECT_AGENT_DOCUMENT_CHARS_LIMIT]
    if file_type == "docx":
        parsed = _read_docx_text_bytes(raw_bytes)
        if parsed:
            return parsed[:PROJECT_AGENT_DOCUMENT_CHARS_LIMIT]
    return fallback[:PROJECT_AGENT_DOCUMENT_CHARS_LIMIT]


def _save_project_document_text(document: Document, text: str) -> None:
    normalized = str(text or "")
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    base_name = slugify(document.title) or f"project-document-{document.id}"
    file_type = (document.file_type or "").lower()

    if file_type == "txt":
        extension = "txt"
        file_bytes = normalized.encode("utf-8")
    else:
        extension = "docx"
        file_bytes = _build_minimal_docx_bytes(normalized)
        document.file_type = "docx"

    file_name = f"{base_name}-{timestamp}.{extension}"
    if document.file:
        document.file.delete(save=False)
    document.file.save(file_name, ContentFile(file_bytes), save=False)
    document.content = normalized[:PROJECT_AGENT_DOCUMENT_CHARS_LIMIT]
    document.version = (document.version or 0) + 1
    document.save()


def _extract_project_patch_block(prompt: str) -> str:
    if not prompt:
        return ""
    match = PROJECT_AGENT_PATCH_BLOCK_RE.search(prompt)
    if match:
        return (match.group(1) or "").strip()
    if "@@" in prompt and ("+" in prompt or "-" in prompt):
        return prompt.strip()
    return ""


def _apply_unified_patch(original_text: str, patch_text: str) -> tuple[str, int]:
    source_lines = (original_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    patch_lines = (patch_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not patch_lines:
        return original_text, 0

    output: list[str] = []
    source_index = 0
    changed = 0
    index = 0

    while index < len(patch_lines):
        line = patch_lines[index]
        if not line:
            index += 1
            continue
        if line.startswith(("diff ", "index ", "--- ", "+++ ")):
            index += 1
            continue

        hunk_match = PROJECT_AGENT_HUNK_RE.match(line)
        if not hunk_match:
            index += 1
            continue

        old_start = int(hunk_match.group(1))
        expected_source_index = max(old_start - 1, 0)
        if expected_source_index < source_index:
            raise ValueError("Patch hunk overlaps previous hunk.")
        output.extend(source_lines[source_index:expected_source_index])
        source_index = expected_source_index
        index += 1

        while index < len(patch_lines):
            patch_line = patch_lines[index]
            if PROJECT_AGENT_HUNK_RE.match(patch_line):
                break
            if patch_line.startswith("\\"):
                index += 1
                continue
            if not patch_line:
                symbol = " "
                value = ""
            else:
                symbol = patch_line[0]
                value = patch_line[1:]

            if symbol == " ":
                if source_index >= len(source_lines) or source_lines[source_index] != value:
                    raise ValueError("Patch context mismatch.")
                output.append(source_lines[source_index])
                source_index += 1
            elif symbol == "-":
                if source_index >= len(source_lines) or source_lines[source_index] != value:
                    raise ValueError("Patch delete mismatch.")
                source_index += 1
                changed += 1
            elif symbol == "+":
                output.append(value)
                changed += 1
            else:
                raise ValueError("Unsupported patch symbol.")
            index += 1

    output.extend(source_lines[source_index:])
    return "\n".join(output), changed


def _extract_json_object(raw_text: str) -> dict:
    text = str(raw_text or "").strip()
    if not text:
        return {}

    code_block = PROJECT_AGENT_JSON_BLOCK_RE.search(text)
    if code_block:
        text = (code_block.group(1) or "").strip()

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    left = text.find("{")
    right = text.rfind("}")
    if left >= 0 and right > left:
        candidate = text[left : right + 1]
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            return {}
    return {}


def _project_agent_history_for_llm(session: ChatSession | None, *, limit: int = 12) -> list[dict]:
    if not session:
        return []

    raw_messages = list(session.messages.order_by("-created", "-id")[: max(1, limit)])
    history: list[dict] = []
    for item in reversed(raw_messages):
        body = str(item.body or "").strip()
        if not body:
            continue
        role = "assistant" if item.sended_from == Message.Sender.BOT else "user"
        history.append({"role": role, "content": body[:2000]})
    return history


def _plan_project_agent_edits(
    *,
    prompt: str,
    document_text: str,
    document: Document | None,
    history_messages: list[dict] | None = None,
    allow_edits: bool = True,
) -> dict:
    base_url = (settings.LLM_API or "").strip().rstrip("/")
    model = (settings.LLM_MODEL or "gpt-oss:20b").strip() or "gpt-oss:20b"
    api_key = (settings.LLM_API_KEY or "").strip()
    if not base_url:
        return {}

    document_title = document.title if document else "Untitled"
    document_type = document.file_type if document else "unknown"
    context_text = (document_text or "")[:12000]
    system_message = (
        "You are a project document assistant. Return ONLY valid JSON.\n"
        "JSON schema:\n"
        "{\n"
        '  "assistant_reply": "string for user",\n'
        '  "patch": "optional unified diff string",\n'
        '  "operations": [\n'
        '    {"op":"replace","old":"...","new":"...","count":1},\n'
        '    {"op":"insert_after","anchor":"...","text":"..."},\n'
        '    {"op":"insert_before","anchor":"...","text":"..."},\n'
        '    {"op":"append","text":"..."},\n'
        '    {"op":"prepend","text":"..."},\n'
        '    {"op":"set_text","text":"..."},\n'
        '    {"op":"delete","old":"...","count":1}\n'
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- Always answer in assistant_reply.\n"
        "- If no edits are needed, return empty patch and empty operations.\n"
        "- Prefer concise assistant_reply.\n"
        f"- allow_edits: {'true' if allow_edits else 'false'}.\n"
        "- If allow_edits is false, patch and operations must be empty."
    )
    context_message = (
        f"Document title: {document_title}\n"
        f"Document type: {document_type}\n"
        f"User request: {prompt}\n"
        "Current document text:\n"
        "<<<DOCUMENT>>>\n"
        f"{context_text}\n"
        "<<<END_DOCUMENT>>>"
    )

    llm_messages: list[dict] = [{"role": "system", "content": system_message}]
    for history_item in (history_messages or [])[-12:]:
        if not isinstance(history_item, dict):
            continue
        role = str(history_item.get("role") or "").strip().lower()
        content = str(history_item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        llm_messages.append({"role": role, "content": content[:2000]})
    llm_messages.append({"role": "user", "content": context_message})

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": llm_messages,
        "stream": False,
        "temperature": 0.1,
    }

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=25,
        )
        response.raise_for_status()
    except requests.RequestException:
        return {}

    try:
        data = response.json() if response.content else {}
    except ValueError:
        return {}
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return {}
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    message = first_choice.get("message") if isinstance(first_choice, dict) else {}
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "")
    parsed = _extract_json_object(content)
    if parsed:
        return parsed

    fallback_reply = str(content or "").strip()
    if fallback_reply:
        fallback_patch = _extract_project_patch_block(fallback_reply)
        return {
            "assistant_reply": fallback_reply,
            "patch": fallback_patch,
            "operations": [],
        }
    return {}


def _apply_project_operations(original_text: str, operations: list) -> tuple[str, int, list[str]]:
    text = str(original_text or "")
    applied = 0
    warnings: list[str] = []

    for raw_operation in (operations or [])[:PROJECT_AGENT_OPERATIONS_LIMIT]:
        if not isinstance(raw_operation, dict):
            continue
        operation = str(raw_operation.get("op") or "").strip().lower()
        if not operation:
            continue

        if operation == "set_text":
            text = str(raw_operation.get("text") or "")
            applied += 1
            continue

        if operation == "append":
            append_text = str(raw_operation.get("text") or "")
            if not append_text:
                warnings.append("append operation is empty.")
                continue
            separator = "\n" if text and not text.endswith("\n") else ""
            text = f"{text}{separator}{append_text}"
            applied += 1
            continue

        if operation == "prepend":
            prepend_text = str(raw_operation.get("text") or "")
            if not prepend_text:
                warnings.append("prepend operation is empty.")
                continue
            separator = "\n" if text and not prepend_text.endswith("\n") else ""
            text = f"{prepend_text}{separator}{text}"
            applied += 1
            continue

        if operation in {"replace", "delete"}:
            old_value = str(raw_operation.get("old") or "")
            if not old_value:
                warnings.append(f"{operation} operation is missing old value.")
                continue
            if old_value not in text:
                warnings.append(f"Text fragment not found for {operation}: {old_value[:60]}")
                continue
            count = raw_operation.get("count")
            try:
                count_value = int(count) if count is not None else 1
            except (TypeError, ValueError):
                count_value = 1
            if count_value <= 0:
                count_value = -1
            new_value = "" if operation == "delete" else str(raw_operation.get("new") or "")
            text = text.replace(old_value, new_value, count_value)
            applied += 1
            continue

        if operation in {"insert_after", "insert_before"}:
            anchor = str(raw_operation.get("anchor") or "")
            fragment = str(raw_operation.get("text") or "")
            if not anchor or not fragment:
                warnings.append(f"{operation} operation is missing anchor or text.")
                continue
            anchor_index = text.find(anchor)
            if anchor_index < 0:
                warnings.append(f"Anchor not found for {operation}: {anchor[:60]}")
                continue
            if operation == "insert_after":
                insert_index = anchor_index + len(anchor)
                text = f"{text[:insert_index]}{fragment}{text[insert_index:]}"
            else:
                text = f"{text[:anchor_index]}{fragment}{text[anchor_index:]}"
            applied += 1
            continue

        warnings.append(f"Unsupported operation: {operation}")

    return text, applied, warnings


def _looks_like_read_request(prompt: str) -> bool:
    lowered = str(prompt or "").strip().lower()
    if not lowered:
        return False
    return any(keyword in lowered for keyword in PROJECT_AGENT_READ_KEYWORDS)


def _looks_like_edit_request(prompt: str) -> bool:
    lowered = str(prompt or "").strip().lower()
    if not lowered:
        return False
    return any(keyword in lowered for keyword in PROJECT_AGENT_EDIT_KEYWORDS)


def _format_document_preview(document_text: str) -> str:
    normalized = str(document_text or "").strip()
    if not normalized:
        return "Document is empty."
    if len(normalized) > PROJECT_AGENT_PREVIEW_CHARS:
        return f"{normalized[:PROJECT_AGENT_PREVIEW_CHARS].rstrip()}\n\n... (truncated)"
    return normalized

@require_GET
@login_required(login_url="account_page")
def project_agent_session(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    requested_session_id = _parse_session_id(request.GET.get("session_id"))
    session = _project_agent_session_for_user(
        request.user,
        project,
        session_id=requested_session_id,
        create=False,
    )
    active_document = _resolve_project_document(project, _parse_session_id(request.GET.get("document_id")))
    return JsonResponse(
        {
            "project_id": project.id,
            "session_id": session.id if session else None,
            "messages": _project_agent_messages(session) if session else [],
            "active_document": _project_agent_document_payload(active_document),
        }
    )


@require_POST
@login_required(login_url="account_page")
def project_agent_ask(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    payload = {}
    if (request.content_type or "").startswith("application/json"):
        try:
            payload = json.loads((request.body or b"{}").decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
    else:
        payload = request.POST

    prompt = str(payload.get("message") or payload.get("prompt") or "").strip()[:4000]
    if not prompt:
        return JsonResponse({"detail": "Message is required"}, status=400)

    requested_session_id = _parse_session_id(payload.get("session_id"))
    requested_document_id = _parse_session_id(payload.get("document_id"))
    session = _project_agent_session_for_user(
        request.user,
        project,
        session_id=requested_session_id,
        create=True,
        title_hint=prompt,
    )
    if not session:
        return JsonResponse({"detail": "Unable to create chat session"}, status=500)

    Message.objects.create(chat=session, body=prompt, sended_from=Message.Sender.USER)
    if session.title == ChatSession.DEFAULT_TITLE:
        session.title = ChatSession.title_from_prompt(prompt)
    session.save()

    active_document = _resolve_project_document(project, requested_document_id)
    warnings: list[str] = []
    document_updated = False
    applied_operations = 0

    if not active_document:
        assistant_text = (
            "There are no project documents yet. Add a file, then the agent can read and edit it."
        )
        Message.objects.create(chat=session, body=assistant_text, sended_from=Message.Sender.BOT)
        session.save()
        return JsonResponse(
            {
                "session_id": session.id,
                "assistant_message": assistant_text,
                "document": _project_agent_document_payload(None),
                "document_updated": False,
                "applied_operations": 0,
                "warnings": warnings,
            }
        )

    document_text = _read_document_text(active_document)
    user_patch_block = _extract_project_patch_block(prompt)
    can_edit_document = _can_edit_project_document(request.user, active_document)
    editable_type = (active_document.file_type or "").lower() in PROJECT_AGENT_EDITABLE_FILE_TYPES
    allow_edits = can_edit_document and editable_type

    if not can_edit_document:
        warnings.append("No edit permission for this document. Running in read-only mode.")
    if can_edit_document and not editable_type:
        warnings.append("Automatic editing is supported only for DOCX and TXT.")

    history_messages = _project_agent_history_for_llm(session, limit=12)
    plan = _plan_project_agent_edits(
        prompt=prompt,
        document_text=document_text,
        document=active_document,
        history_messages=history_messages,
        allow_edits=allow_edits,
    )

    assistant_text = str(plan.get("assistant_reply") or "").strip() if isinstance(plan, dict) else ""
    updated_text = document_text

    if allow_edits:
        model_patch_block = str(plan.get("patch") or "").strip() if isinstance(plan, dict) else ""
        operations = plan.get("operations") if isinstance(plan, dict) else []

        if user_patch_block:
            try:
                updated_text, applied_operations = _apply_unified_patch(document_text, user_patch_block)
            except ValueError as exc:
                warnings.append(str(exc))
        elif model_patch_block:
            try:
                updated_text, applied_operations = _apply_unified_patch(document_text, model_patch_block)
            except ValueError as exc:
                warnings.append(f"Model patch rejected: {exc}")
        elif isinstance(operations, list) and operations:
            updated_text, applied_operations, plan_warnings = _apply_project_operations(document_text, operations)
            warnings.extend(plan_warnings)
    elif user_patch_block:
        warnings.append("Patch was not applied because this session is read-only.")

    if updated_text != document_text:
        _save_project_document_text(active_document, updated_text)
        document_updated = True

    if not assistant_text:
        if document_updated:
            assistant_text = (
                f"Done. Document '{active_document.title}' was updated. "
                f"Applied operations: {applied_operations}. Current version: {active_document.version}."
            )
        else:
            preview = _format_document_preview(document_text)
            assistant_text = (
                "Request processed. No document changes were applied.\n\n"
                f"Document: {active_document.title}\n"
                f"Type: {active_document.file_type.upper()} - Version: {active_document.version}\\n\\n"
                f"{preview}"
            )

    Message.objects.create(chat=session, body=assistant_text, sended_from=Message.Sender.BOT)
    session.save()
    return JsonResponse(
        {
            "session_id": session.id,
            "assistant_message": assistant_text,
            "document": _project_agent_document_payload(active_document),
            "document_updated": document_updated,
            "applied_operations": applied_operations,
            "warnings": warnings,
        }
    )


def parse_agent_payload(request) -> dict:
    if (request.content_type or "").startswith("application/json"):
        try:
            return json.loads((request.body or b"{}").decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
    return request.POST


@require_POST
@login_required(login_url="account_page")
def project_agent_cancel(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    payload = parse_agent_payload(request)
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return JsonResponse({"detail": "task_id is required"}, status=400)

    session_id = _parse_session_id(payload.get("session_id"))
    cache.set(_project_agent_cancel_cache_key(task_id), True, timeout=60 * 30)
    if session_id:
        ConfirmationGate.push_decision(project.id, session_id, "reject")
    logger.info("Project agent task cancel requested project=%s task_id=%s user=%s", project_id, task_id, request.user.id)
    return JsonResponse({"status": "cancelled", "task_id": task_id})


@require_POST
@login_required(login_url="account_page")
def project_agent_task(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    payload = parse_agent_payload(request)
    prompt = str(payload.get("message") or payload.get("prompt") or "").strip()[:4000]
    if not prompt:
        return JsonResponse({"detail": "Message is required"}, status=400)
    task_id = str(payload.get("task_id") or "").strip() or uuid.uuid4().hex
    raw_thinking_mode = str(payload.get("thinking_mode") or "").strip().lower()
    thinking_mode = raw_thinking_mode if raw_thinking_mode in {"fast", "middle", "high"} else "middle"
    access_scope = _normalize_agent_access_scope(
        payload.get("access_scope") or payload.get("agent_access_scope")
    )
    interaction_mode = _normalize_agent_interaction_mode(
        payload.get("interaction_mode") or payload.get("agent_interaction_mode")
    )

    requested_session_id = _parse_session_id(payload.get("session_id"))
    requested_document_id = _parse_session_id(payload.get("document_id"))
    requested_publication_id = _parse_session_id(
        payload.get("publication_id") or payload.get("active_publication_id") or payload.get("target_publication_id")
    )
    requested_publication_file_id = _parse_session_id(
        payload.get("publication_file_id") or payload.get("active_publication_file_id")
    )
    target_publication = _project_publication_for_project(project, requested_publication_id or 0) if requested_publication_id else None
    target_publication_file = (
        _project_publication_file_for_project(project, requested_publication_file_id or 0)
        if requested_publication_file_id
        else None
    )
    session = _project_agent_session_for_user(
        request.user,
        project,
        session_id=requested_session_id,
        create=True,
        title_hint=prompt,
    )
    if not session:
        return JsonResponse({"detail": "Unable to create chat session"}, status=500)

    Message.objects.create(chat=session, body=prompt, sended_from=Message.Sender.USER)
    if session.title == ChatSession.DEFAULT_TITLE:
        session.title = ChatSession.title_from_prompt(prompt)
    session.save()
    session_cookie_name = str(getattr(settings, "SESSION_COOKIE_NAME", "sessionid") or "sessionid")
    csrf_cookie_name = str(getattr(settings, "CSRF_COOKIE_NAME", "csrftoken") or "csrftoken")
    cookie_parts = []
    session_cookie_value = str(request.COOKIES.get(session_cookie_name) or "").strip()
    csrf_cookie_value = str(request.COOKIES.get(csrf_cookie_name) or "").strip()
    if session_cookie_value:
        cookie_parts.append(f"{session_cookie_name}={session_cookie_value}")
    if csrf_cookie_value:
        cookie_parts.append(f"{csrf_cookie_name}={csrf_cookie_value}")
    auth_cookie_header = "; ".join(cookie_parts)
    doc_key = payload.get("doc_key") or ""


    result = ProjectAgentOrchestrator(
        user=request.user,
        project=project,
        session_id=session.id,
        document_id=requested_document_id,
        publication_id=target_publication.id if target_publication else None,
        publication_file_id=target_publication_file.id if target_publication_file else None,
        thinking_mode=thinking_mode,
        access_scope=access_scope,
        interaction_mode=interaction_mode,
        auth_cookie_header=auth_cookie_header,
        active_doc_key=doc_key,
        task_id=task_id,
    ).handle(prompt)

    mode = str(result.get("mode") or "").strip() if isinstance(result, dict) else ""
    response_text = str(result.get("response_text") or "").strip() if isinstance(result, dict) else ""
    search_tags = result.get("search_tags") if isinstance(result, dict) and isinstance(result.get("search_tags"), list) else []
    document_targets = (
        result.get("document_targets")
        if isinstance(result, dict) and isinstance(result.get("document_targets"), list)
        else []
    )
    relevant_publication_ids = (
        result.get("relevant_publication_ids")
        if isinstance(result, dict) and isinstance(result.get("relevant_publication_ids"), list)
        else []
    )
    reference_suggestions = _project_reference_suggestions_payload(
        project=project,
        user=request.user,
        target_publication=target_publication,
        publication_ids=relevant_publication_ids,
    )
    complete_payload = {
        "mode": mode,
        "assistant_message": response_text,
        "response_text": response_text,
        "search_tags": search_tags,
        "document_targets": document_targets,
        "access_scope": access_scope,
        "interaction_mode": interaction_mode,
        "reference_suggestions": reference_suggestions,
        "reference_target_publication_id": target_publication.id if target_publication else None,
        "reference_target_publication_title": target_publication.title_original if target_publication else "",
        "task_id": task_id,
    }

    if mode in {"text_response", "more_context", "document_operation", "document_details"} and response_text:
        Message.objects.create(chat=session, body=response_text, sended_from=Message.Sender.BOT)
        session.save()

    if mode != "error":
        try:
            AgentProgressPublisher(project_id=project.id, session_id=session.id).publish("complete", **complete_payload)
        except Exception:
            logger.exception("Failed to publish enriched project agent completion", extra={"project_id": project.id, "session_id": session.id})

    return JsonResponse(
        {
        "status": "accepted",
        "session_id": session.id,
        "mode": mode,
        "thinking_mode": thinking_mode,
        "access_scope": access_scope,
        "interaction_mode": interaction_mode,
        "assistant_message": response_text,
        "response_text": response_text,
        "search_tags": search_tags,
        "document_targets": document_targets,
        "reference_suggestions": reference_suggestions,
        "reference_target_publication_id": target_publication.id if target_publication else None,
        "reference_target_publication_title": target_publication.title_original if target_publication else "",
        "task_id": task_id,
        "message": "Агент получил задание и приступил к работе.",
        }
    )

@require_GET
@login_required(login_url="account_page")
def project_page(request):
    create_requested = (request.GET.get("create") or "").strip().lower() in {"1", "true", "yes"}
    projects = Project.objects.filter(owner=request.user).order_by("name", "id")
    collaborating_projects = request.user.collaborating_projects.exclude(owner=request.user).order_by("name", "id")
    context = {
        "create": create_requested,
        "projects": projects,
        "collaborating_projects": collaborating_projects,
        "active_project": None,
    }
    template_name = "project_system/project_page.html"

    return render(request, template_name=template_name, context=context)


@require_GET
@login_required(login_url="account_page")
def project_detail_page(request, project_id):
    project = _project_for_user_or_404(request.user, project_id)

    projects = Project.objects.filter(owner=request.user).order_by("name", "id")
    collaborating_projects = request.user.collaborating_projects.exclude(owner=request.user).order_by("name", "id")
    project_documents = _project_documents(project)
    selected_publication_id_raw = (request.GET.get("publication") or "").strip()
    selected_publication_id = int(selected_publication_id_raw) if selected_publication_id_raw.isdigit() else None
    selected_publication = _project_publication_for_project(project, selected_publication_id or 0) if selected_publication_id else None
    selected_publication_file_id = (request.GET.get("publication_file") or "").strip()
    active_publication_file = None
    if selected_publication_file_id.isdigit():
        active_publication_file = _project_publication_file_for_project(project, int(selected_publication_file_id))

    project_publications, first_publication_file = _project_publication_sections(
        project,
        user=request.user,
        selected_publication_id=selected_publication_id,
        selected_publication_file_id=active_publication_file.id if active_publication_file else None,
    )
    publication_file_groups, publication_files_total = _project_publication_file_groups(project_publications)
    selected_file_id = (request.GET.get("file") or "").strip()
    active_document = None
    if selected_publication and not active_publication_file:
        for section in project_publications:
            if section["publication"].id == selected_publication.id:
                section["is_open"] = True
                if section["files"]:
                    active_publication_file = section["files"][0]
                break

    if not active_publication_file and not selected_publication and selected_file_id.isdigit():
        active_document = project_documents.filter(id=int(selected_file_id)).first()
    if not active_document and not active_publication_file and not selected_publication:
        active_document = project_documents.first()
    if not active_document and not active_publication_file and not selected_publication:
        active_publication_file = first_publication_file
        if active_publication_file:
            for section in project_publications:
                section["is_open"] = section["publication"].id == active_publication_file.publication_id

    active_publication = selected_publication or (active_publication_file.publication if active_publication_file else None)
    publication_reference_targets = [
        section["publication"]
        for section in project_publications
        if section.get("can_manage")
    ]
    publication_reference_target_default_id = None
    if active_publication and _project_user_can_manage_publication(request.user, active_publication):
        publication_reference_target_default_id = active_publication.id
    elif publication_reference_targets:
        publication_reference_target_default_id = publication_reference_targets[0].id
    active_publication_file_mode = _project_publication_file_mode(active_publication_file) if active_publication_file else ""
    editor_config_url = ""
    if active_document:
        editor_config_url = reverse("onlyoffice_config", kwargs={"pk": active_document.id})
    elif active_publication_file and active_publication_file_mode == "onlyoffice":
        editor_config_url = reverse(
            "project_publication_file_onlyoffice_config",
            kwargs={"project_id": project.id, "file_id": active_publication_file.id},
        )
    project_chat_session = _project_agent_session_for_user(request.user, project, create=True)
    context = {
        "active_project": project,
        "projects": projects,
        "collaborating_projects": collaborating_projects,
        "create": False,
        "project_members": _project_members(project),
        "project_documents": project_documents,
        "project_publications": project_publications,
        "project_publication_file_groups": publication_file_groups,
        "project_publication_files_total": publication_files_total,
        "project_publication_types": _project_publication_types(),
        "project_publication_languages": _project_publication_languages(),
        "publication_file_kind_choices": PublicationFile.FILE_KIND_CHOICES,
        "project_publication_candidates": _project_publication_candidates(project),
        "project_publication_reference_targets": publication_reference_targets,
        "project_publication_reference_target_default_id": publication_reference_target_default_id,
        "project_publication_year_default": timezone.now().year,
        "selected_publication": selected_publication,
        "active_publication": active_publication,
        "active_project_document": active_document,
        "active_publication_file": active_publication_file,
        "active_publication_file_mode": active_publication_file_mode,
        "active_publication_file_can_manage": _project_user_can_manage_publication(request.user, active_publication) if active_publication else False,
        "project_publication_file_download_url": reverse(
            "project_publication_file_stream",
            kwargs={"project_id": project.id, "file_id": active_publication_file.id},
        ) if active_publication_file else "",
        "project_publication_file_iframe_url": reverse(
            "project_publication_file_stream",
            kwargs={"project_id": project.id, "file_id": active_publication_file.id},
        ) if active_publication_file and active_publication_file_mode == "iframe" else "",
        "project_onlyoffice_api_js": _onlyoffice_api_js_url() if editor_config_url else "",
        "project_onlyoffice_config_url": editor_config_url,
        "project_agent_session_url": reverse("project_agent_session", kwargs={"project_id": project.id}),
        "project_agent_ask_url": reverse("project_agent_task", kwargs={"project_id": project.id}),
        "project_agent_cancel_url": reverse("project_agent_cancel", kwargs={"project_id": project.id}),
        "project_agent_reference_add_url": reverse("project_publication_reference_add", kwargs={"project_id": project.id}),
        "project_agent_session_id": project_chat_session.id if project_chat_session else "",
    }
    template_name = "project_system/project_page.html"

    return render(request, template_name=template_name, context=context)


@login_required(login_url="account_page")
@require_POST
def project_publication_create(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    title = (request.POST.get("title_original") or "").strip()[:500]
    if not title:
        if _request_wants_json(request):
            return JsonResponse({"detail": "Publication title is required"}, status=400)
        return redirect("project_detail_page", project_id=project.id)

    try:
        with transaction.atomic():
            publication = Publication.objects.create(
                pub_type=_project_publication_type_from_request(request),
                title_original=title,
                language=_project_publication_language_from_request(request),
                year=_project_publication_year_from_request(request),
                venue=_project_publication_venue_from_request(request),
                private=False,
                created_by=request.user,
            )
            PublicationProject.objects.get_or_create(project=project, publication=publication)
    except IntegrityError as exc:
        logger.exception(
            "Failed to create publication for project",
            extra={"project_id": project.id, "user_id": getattr(request.user, "id", None)},
        )
        raw_error = str(exc).lower()
        if "main_publication_record_id_key" in raw_error or "record_id" in raw_error:
            detail = "Unable to create publication: record ID conflict. Please retry."
        else:
            detail = "Unable to create publication due to data conflict."

        if _request_wants_json(request):
            return JsonResponse({"detail": detail}, status=409)
        return redirect("project_detail_page", project_id=project.id)
    except Exception:
        logger.exception(
            "Unexpected error while creating publication for project",
            extra={"project_id": project.id, "user_id": getattr(request.user, "id", None)},
        )
        detail = "Unable to create publication due to unexpected server error."
        if _request_wants_json(request):
            return JsonResponse({"detail": detail}, status=500)
        return redirect("project_detail_page", project_id=project.id)

    if _request_wants_json(request):
        return JsonResponse(
            {
                "project_id": project.id,
                "publication_id": publication.id,
                "title_original": publication.title_original,
            },
            status=201,
        )
    return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}")


@login_required(login_url="account_page")
@require_POST
def project_publication_link(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    publication_id = (request.POST.get("publication_id") or "").strip()
    publication = _project_visible_publications_queryset().filter(id=publication_id).first() if publication_id.isdigit() else None
    if not publication:
        if _request_wants_json(request):
            return JsonResponse({"detail": "Publication not found"}, status=404)
        return redirect("project_detail_page", project_id=project.id)

    PublicationProject.objects.get_or_create(project=project, publication=publication)

    if _request_wants_json(request):
        return JsonResponse(
            {
                "project_id": project.id,
                "publication_id": publication.id,
                "title_original": publication.title_original,
            }
        )
    return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}")


@login_required(login_url="account_page")
@require_POST
def project_publication_reference_add(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    payload = parse_agent_payload(request)
    wants_json = _request_wants_json(request)
    target_publication_id = _parse_session_id(
        payload.get("target_publication_id") or payload.get("publication_id")
    )
    referenced_publication_id = _parse_session_id(
        payload.get("referenced_publication_id") or payload.get("reference_publication_id")
    )

    target_publication = _project_publication_for_project(project, target_publication_id or 0) if target_publication_id else None
    if not target_publication:
        if wants_json:
            return JsonResponse({"detail": "Target publication not found"}, status=404)
        return redirect("project_detail_page", project_id=project.id)
    if not _project_user_can_manage_publication(request.user, target_publication):
        if wants_json:
            return JsonResponse({"detail": "Forbidden"}, status=403)
        return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={target_publication.id}")

    referenced_publication = (
        _project_visible_publications_queryset()
        .filter(id=referenced_publication_id or 0)
        .select_related("venue")
        .first()
    )
    if not referenced_publication:
        if wants_json:
            return JsonResponse({"detail": "Referenced publication not found"}, status=404)
        return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={target_publication.id}")
    if referenced_publication.id == target_publication.id:
        if wants_json:
            return JsonResponse({"detail": "Publication cannot reference itself"}, status=400)
        return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={target_publication.id}")

    existing_reference = PublicationReference.objects.filter(
        publication=target_publication,
        referenced_publication=referenced_publication,
    ).order_by("order", "id").first()
    if existing_reference:
        if wants_json:
            return JsonResponse(
                {
                    "status": "already_exists",
                    "reference_id": existing_reference.id,
                    "target_publication_id": target_publication.id,
                    "referenced_publication_id": referenced_publication.id,
                    "order": existing_reference.order,
                }
            )
        return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={target_publication.id}")

    last_order = (
        PublicationReference.objects.filter(publication=target_publication)
        .order_by("-order", "-id")
        .values_list("order", flat=True)
        .first()
        or 0
    )
    reference = PublicationReference.objects.create(
        publication=target_publication,
        referenced_publication=referenced_publication,
        order=last_order + 1,
    )
    _touch_publication(target_publication)

    if wants_json:
        return JsonResponse(
            {
                "status": "created",
                "reference_id": reference.id,
                "target_publication_id": target_publication.id,
                "referenced_publication_id": referenced_publication.id,
                "order": reference.order,
            },
            status=201,
        )
    return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={target_publication.id}")


@login_required(login_url="account_page")
@require_POST
def project_publication_file_create(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    publication_id = (request.POST.get("publication_id") or "").strip()
    publication = _project_publication_for_project(project, int(publication_id)) if publication_id.isdigit() else None
    if not publication:
        if _request_wants_json(request):
            return JsonResponse({"detail": "Publication not found"}, status=404)
        return redirect("project_detail_page", project_id=project.id)
    if not _project_user_can_manage_publication(request.user, publication):
        if _request_wants_json(request):
            return JsonResponse({"detail": "Forbidden"}, status=403)
        return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}")

    file_kind = (request.POST.get("file_type") or "").strip().lower()
    config = PROJECT_FILE_KIND_MAP.get(file_kind)
    if not config:
        if _request_wants_json(request):
            return JsonResponse({"detail": "Unsupported file type"}, status=400)
        return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}")

    description = (request.POST.get("description") or "").strip()
    if not description:
        description = config["default_title"]

    extension, _document_file_type, file_bytes, _text_content = _build_project_file_payload(file_kind, description)
    publication_file = PublicationFile(
        publication=publication,
        kind=_publication_file_kind_from_request(request, f"file.{extension}"),
        description=description[:200],
    )
    publication_file.file.save(_build_publication_file_name(description, extension), ContentFile(file_bytes), save=False)
    publication_file.save()
    _touch_publication(publication)

    if _request_wants_json(request):
        return JsonResponse(
            {
                "project_id": project.id,
                "publication_id": publication.id,
                "publication_file_id": publication_file.id,
            },
            status=201,
        )
    return redirect(
        f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}&publication_file={publication_file.id}"
    )


@login_required(login_url="account_page")
@require_POST
def project_publication_file_upload(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    publication_id = (request.POST.get("publication_id") or "").strip()
    publication = _project_publication_for_project(project, int(publication_id)) if publication_id.isdigit() else None
    if not publication:
        if _request_wants_json(request):
            return JsonResponse({"detail": "Publication not found"}, status=404)
        return redirect("project_detail_page", project_id=project.id)
    if not _project_user_can_manage_publication(request.user, publication):
        if _request_wants_json(request):
            return JsonResponse({"detail": "Forbidden"}, status=403)
        return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}")

    uploaded = request.FILES.get("file")
    if not uploaded:
        if _request_wants_json(request):
            return JsonResponse({"detail": "File is required"}, status=400)
        return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}")

    inferred_description = os.path.splitext(uploaded.name or "")[0].strip() or "Publication file"
    description = ((request.POST.get("description") or "").strip() or inferred_description)[:200]
    publication_file = PublicationFile.objects.create(
        publication=publication,
        kind=_publication_file_kind_from_request(request, uploaded.name),
        description=description,
        file=uploaded,
    )
    _touch_publication(publication)

    if _request_wants_json(request):
        return JsonResponse(
            {
                "project_id": project.id,
                "publication_id": publication.id,
                "publication_file_id": publication_file.id,
            },
            status=201,
        )
    return redirect(
        f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?publication={publication.id}&publication_file={publication_file.id}"
    )


@login_required(login_url="account_page")
def project_publication_file_onlyoffice_config(request, project_id: int, file_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    publication_file = _project_publication_file_for_project(project, file_id)
    if not publication_file:
        raise Http404("Publication file not found")
    if not publication_file.file:
        return JsonResponse({"detail": "File not found"}, status=404)
    if _project_publication_file_mode(publication_file) != "onlyoffice":
        return JsonResponse({"detail": "OnlyOffice is not supported for this file type"}, status=400)

    config = _build_project_publication_file_onlyoffice_config(request, project, publication_file)
    logger.info(
        "Project publication OnlyOffice config project=%s publication_file=%s user=%s mode=%s key=%s",
        project.id,
        publication_file.id,
        request.user.id,
        config["editorConfig"]["mode"],
        config["document"]["key"],
    )
    return JsonResponse(config)


def project_publication_file_stream(request, project_id: int, file_id: int):
    project = Project.objects.filter(id=project_id).first()
    if not project:
        raise Http404("Project not found")

    publication_file = _project_publication_file_for_project(project, file_id)
    if not publication_file:
        raise Http404("Publication file not found")

    allowed = False
    if request.user.is_authenticated and _project_queryset_for_user(request.user).filter(id=project.id).exists():
        allowed = True
    else:
        token = (request.GET.get("access_token") or "").strip()
        if token:
            allowed = _validate_project_publication_file_access_token(token, project, publication_file)

    if not allowed:
        logger.warning("Project publication file access denied project=%s publication_file=%s", project.id, file_id)
        return JsonResponse({"detail": "Forbidden"}, status=403)

    if not publication_file.file:
        raise Http404("File not found")

    try:
        stream = publication_file.file.open("rb")
    except OSError:
        raise Http404("File not found")

    download_requested = (request.GET.get("download") or "").strip().lower() in {"1", "true", "yes"}
    content_type = mimetypes.guess_type(publication_file.file.name)[0] or "application/octet-stream"

    logger.info("Project publication file served project=%s publication_file=%s", project.id, file_id)
    return FileResponse(
        stream,
        as_attachment=download_requested,
        filename=publication_file.file.name.rsplit("/", 1)[-1],
        content_type=content_type,
    )


@csrf_exempt
def project_publication_file_onlyoffice_callback(request, project_id: int, file_id: int):
    if request.method != "POST":
        return JsonResponse({"error": 0})

    project = Project.objects.filter(id=project_id).first()
    if not project:
        return JsonResponse({"error": 1}, status=404)

    publication_file = _project_publication_file_for_project(project, file_id)
    if not publication_file:
        return JsonResponse({"error": 1}, status=404)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Project publication OnlyOffice callback invalid JSON project=%s publication_file=%s", project_id, file_id)
        return JsonResponse({"error": 1}, status=400)

    secret = _jwt_secret()
    if secret:
        token = payload.get("token")
        if not token:
            logger.warning("Project publication callback token missing project=%s publication_file=%s", project_id, file_id)
            return JsonResponse({"error": 1}, status=403)
        try:
            payload = _jwt_decode(token, secret)
            payload["token"] = token
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Project publication callback JWT invalid project=%s publication_file=%s error=%s",
                project_id,
                file_id,
                exc,
            )
            return JsonResponse({"error": 1}, status=403)

    status = int(payload.get("status", 0))
    logger.info("Project publication OnlyOffice callback project=%s publication_file=%s status=%s", project_id, file_id, status)

    if status in ONLYOFFICE_ERROR_STATUSES:
        logger.warning("Project publication callback save error project=%s publication_file=%s payload=%s", project_id, file_id, payload)
        return JsonResponse({"error": 0})

    if status not in ONLYOFFICE_SAVE_STATUSES:
        return JsonResponse({"error": 0})

    if _project_publication_file_mode(publication_file) != "onlyoffice":
        return JsonResponse({"error": 1}, status=400)

    editor_config = payload.get("editorConfig") if isinstance(payload.get("editorConfig"), dict) else {}
    document_payload = payload.get("document") if isinstance(payload.get("document"), dict) else {}
    permissions = document_payload.get("permissions") if isinstance(document_payload.get("permissions"), dict) else {}
    owner_id = publication_file.publication.created_by_id
    payload_user_id_raw = str((editor_config.get("user") or {}).get("id") or "").strip()
    payload_user_id = int(payload_user_id_raw) if payload_user_id_raw.isdigit() else None

    if not owner_id or payload_user_id != owner_id or not permissions.get("edit"):
        logger.warning(
            "Project publication callback forbidden project=%s publication_file=%s owner=%s payload_user=%s",
            project_id,
            file_id,
            owner_id,
            payload_user_id,
        )
        return JsonResponse({"error": 1}, status=403)

    file_url = (payload.get("url") or "").strip()
    if not file_url:
        logger.warning("Project publication callback missing file url project=%s publication_file=%s", project_id, file_id)
        return JsonResponse({"error": 1}, status=400)

    request_headers = {}
    if secret and payload.get("token"):
        request_headers["Authorization"] = f"Bearer {payload['token']}"

    try:
        downloaded = requests.get(file_url, headers=request_headers, timeout=90)
        downloaded.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(
            "Project publication callback download failed project=%s publication_file=%s url=%s error=%s",
            project_id,
            file_id,
            file_url,
            exc,
        )
        return JsonResponse({"error": 1}, status=502)

    _save_publication_file_content(publication_file, downloaded.content)
    publication_file.save()
    _touch_publication(publication_file.publication)
    logger.info("Project publication callback saved project=%s publication_file=%s", project_id, file_id)
    return JsonResponse({"error": 0})


def _request_wants_json(request) -> bool:
    content_type = (request.content_type or "").split(";")[0].strip().lower()
    accept = (request.headers.get("Accept") or "").lower()
    return content_type == "application/json" or "application/json" in accept


@login_required(login_url="account_page")
@require_POST
def create_project(request):
    name = (request.POST.get("name") or request.POST.get("project_name") or "").strip()
    description = request.POST.get("description", "").strip()
    if not description:
        description = request.POST.get("project_description", "").strip()

    if not name:
        if _request_wants_json(request):
            return JsonResponse({"detail": "Project name is required"}, status=400)
        return redirect(f"{reverse('project_page')}?create=true")

    project = Project.objects.create(
        name=name,
        description=description,
        owner=request.user,
    )
    if _request_wants_json(request):
        return JsonResponse(
            {
                "id": project.id,
                "name": project.name,
                "description": project.description,
                "owner": project.owner_id,
            },
            status=201,
        )
    return redirect("project_detail_page", project_id=project.id)


@login_required(login_url="account_page")
@require_POST
def project_file_create(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    file_kind = (request.POST.get("file_type") or "").strip().lower()
    config = PROJECT_FILE_KIND_MAP.get(file_kind)
    if not config:
        if _request_wants_json(request):
            return JsonResponse({"detail": "Unsupported file type"}, status=400)
        return redirect("project_detail_page", project_id=project.id)

    title = (request.POST.get("title") or "").strip()
    if not title:
        title = config["default_title"]

    extension, document_file_type, file_bytes, text_content = _build_project_file_payload(file_kind, title)
    file_name = _build_project_file_name(title, extension)
    document = Document(
        title=title[:255],
        content=(text_content or "")[:10000],
        file_type=document_file_type,
        user=request.user,
        version=1,
        is_deleted=False,
    )
    document.file.save(file_name, ContentFile(file_bytes), save=False)
    document.save()
    project.documents.add(document)
    _sync_project_document_permissions(project, document)

    if _request_wants_json(request):
        return JsonResponse(
            {
                "project_id": project.id,
                "document_id": document.id,
                "title": document.title,
                "file_type": document.file_type,
                "edit_url": reverse("document_edit", kwargs={"pk": document.id}),
            },
            status=201,
        )
    return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?file={document.id}")


@login_required(login_url="account_page")
@require_POST
def project_file_upload(request, project_id: int):
    project = _project_for_user_or_404(request.user, project_id)
    uploaded = request.FILES.get("file")
    if not uploaded:
        if _request_wants_json(request):
            return JsonResponse({"detail": "File is required"}, status=400)
        return redirect("project_detail_page", project_id=project.id)

    raw_title = (request.POST.get("title") or "").strip()
    inferred_title = os.path.splitext(uploaded.name or "")[0].strip() or "Uploaded file"
    title = (raw_title or inferred_title)[:255]
    document_file_type = _document_file_type_from_name(uploaded.name)
    text_content = ""
    if document_file_type == "txt":
        try:
            text_content = uploaded.read().decode("utf-8")
        except UnicodeDecodeError:
            text_content = ""
        finally:
            try:
                uploaded.seek(0)
            except Exception:  # noqa: BLE001
                pass

    document = Document.objects.create(
        title=title,
        content=text_content[:10000],
        file=uploaded,
        file_type=document_file_type,
        user=request.user,
        version=1,
        is_deleted=False,
    )
    project.documents.add(document)
    _sync_project_document_permissions(project, document)

    if _request_wants_json(request):
        return JsonResponse(
            {
                "project_id": project.id,
                "document_id": document.id,
                "title": document.title,
                "file_type": document.file_type,
                "edit_url": reverse("document_edit", kwargs={"pk": document.id}),
            },
            status=201,
        )
    return redirect(f"{reverse('project_detail_page', kwargs={'project_id': project.id})}?file={document.id}")
