import base64
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import socket
import zipfile
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.files.base import ContentFile
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from document.forms import DocumentGeneratorCreateForm
from document.models import Document, DocumentGenerator, DocumentPermission, Synonym


logger = logging.getLogger(__name__)
User = get_user_model()

ONLYOFFICE_SAVE_STATUSES = {2, 6}
ONLYOFFICE_ERROR_STATUSES = {3, 7}
FILE_TOKEN_SALT = "document-file-access"
GENERATOR_FILE_TOKEN_SALT = "generator-file-access"
PROJECT_AGENT_ONLYOFFICE_PLUGIN_GUID = "asc.{8DFA4E54-52F2-4D8C-8AF1-A8B31C8A4D12}"


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dock_editor_origin() -> str:
    return (settings.DOCK_EDITOR_URL or "").rstrip("/")


def _is_local_host(hostname: str) -> bool:
    return (hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


def _guess_local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def _build_public_base_url(request) -> str:
    configured = (settings.APP_PUBLIC_URL or "").strip().rstrip("/")
    if configured:
        return configured

    built = request.build_absolute_uri("/").rstrip("/")
    parsed = urlsplit(built)
    if not _is_local_host(parsed.hostname or ""):
        return built

    guessed_ip = _guess_local_ip()
    if not guessed_ip:
        return built

    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit(parsed._replace(netloc=f"{guessed_ip}{port}")).rstrip("/")


def _absolute_url(request, path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return urljoin(f"{_build_public_base_url(request)}/", path_or_url.lstrip("/"))


def _project_agent_public_node_url(request) -> str:
    configured = str(getattr(settings, "PROJECT_AGENT_PUBLIC_NODE_URL", "") or "").strip()
    if configured:
        return configured.rstrip("/")

    public_base = _build_public_base_url(request)
    parsed = urlsplit(public_base)
    host = parsed.hostname or "localhost"
    node_port = _to_int(getattr(settings, "NODE_PORT", 3000) or 3000, default=3000)
    if node_port <= 0:
        node_port = 3000
    return urlunsplit(parsed._replace(netloc=f"{host}:{node_port}")).rstrip("/")


def _append_project_agent_plugin_config(
    request,
    config: dict,
    *,
    document_key: str,
    target_type: str,
    target_id: int,
) -> dict:
    if not isinstance(config, dict):
        return config

    normalized_key = str(document_key or "").strip()
    normalized_target_type = str(target_type or "").strip().lower()
    normalized_target_id = _to_int(target_id, default=0)
    if not normalized_key or normalized_target_type not in {"project", "publication_file"} or normalized_target_id <= 0:
        return config

    plugin_base_url = f"{_project_agent_public_node_url(request)}/plugins/llm-doc-editor"
    plugin_config_url = (
        f"{plugin_base_url}/config.json?"
        f"{urlencode({'doc_key': normalized_key, 'target_type': normalized_target_type, 'target_id': normalized_target_id})}"
    )

    editor_config = config.setdefault("editorConfig", {})
    plugins_config = editor_config.setdefault("plugins", {})

    plugins_data = plugins_config.setdefault("pluginsData", [])
    if plugin_config_url not in plugins_data:
        plugins_data.append(plugin_config_url)

    autostart = plugins_config.setdefault("autostart", [])
    if PROJECT_AGENT_ONLYOFFICE_PLUGIN_GUID not in autostart:
        autostart.append(PROJECT_AGENT_ONLYOFFICE_PLUGIN_GUID)

    return config


def _json_request(request) -> bool:
    content_type = (request.content_type or "").split(";")[0].strip().lower()
    accept = request.headers.get("Accept", "")
    return content_type == "application/json" or "application/json" in accept


def _request_payload(request) -> dict:
    if (request.content_type or "").startswith("application/json"):
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
    return request.POST


def _owner_permissions() -> dict:
    return {
        "can_view": True,
        "can_edit": True,
        "can_comment": True,
        "can_review": True,
        "can_download": True,
        "can_print": True,
    }


def _no_permissions() -> dict:
    return {
        "can_view": False,
        "can_edit": False,
        "can_comment": False,
        "can_review": False,
        "can_download": False,
        "can_print": False,
    }


def _document_permissions_for_user(document: Document, user) -> dict:
    if not user or not user.is_authenticated:
        return _no_permissions()

    if document.user_id == user.id:
        return _owner_permissions()

    permission = (
        DocumentPermission.objects.filter(document=document, user=user)
        .only(
            "can_view",
            "can_edit",
            "can_comment",
            "can_review",
            "can_download",
            "can_print",
        )
        .first()
    )
    if not permission:
        return _no_permissions()

    return {
        "can_view": permission.can_view,
        "can_edit": permission.can_edit,
        "can_comment": permission.can_comment,
        "can_review": permission.can_review,
        "can_download": permission.can_download,
        "can_print": permission.can_print,
    }


def _can_view_document(document: Document, user) -> bool:
    return _document_permissions_for_user(document, user)["can_view"]


def _documents_for_user(user):
    return (
        Document.objects.filter(is_deleted=False)
        .select_related("user")
        .filter(
            Q(user=user)
            | Q(permissions__user=user, permissions__can_view=True)
        )
        .distinct()
        .order_by("-updated_at", "-id")
    )


def _generators_for_user(user):
    if user and user.is_authenticated:
        generated_documents_qs = (
            Document.objects.filter(is_deleted=False)
            .select_related("user", "generated_by")
            .order_by("-updated_at", "-id")
        )
    else:
        generated_documents_qs = Document.objects.none()
    queryset = DocumentGenerator.objects.select_related("user")
    if user and user.is_authenticated:
        queryset = queryset.filter(Q(access_to_all=True) | Q(user=user))
    else:
        queryset = queryset.filter(access_to_all=True)
    return (
        queryset
        .prefetch_related(Prefetch("generated_documents", queryset=generated_documents_qs))
        .distinct()
        .order_by("-created_at", "-id")
    )


def _serialize_generator(generator: DocumentGenerator) -> dict:
    return {
        "id": generator.id,
        "title": generator.title,
        "file_type": generator.file_type,
        "owner_id": generator.user_id,
        "owner_name": generator.user.get_full_name() or generator.user.username,
        "access_to_all": generator.access_to_all,
        "created_at": generator.created_at.isoformat(),
        "generated_documents_count": generator.generated_documents.filter(is_deleted=False).count(),
    }


def _serialize_synonyms(*, include_id: bool = False) -> list[dict]:
    synonyms = Synonym.objects.order_by("value", "id").values("id", "code", "value", "example")
    result: list[dict] = []
    for item in synonyms:
        row = {
            "code": item["code"],
            "value": item["value"],
            "example": item["example"] or "",
        }
        if include_id:
            row["id"] = item["id"]
        result.append(row)
    return result


def _normalize_synonyms_payload(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("synonyms")
    if not isinstance(payload, list):
        raise ValueError("JSON payload must be an array or an object with 'synonyms' array.")
    return [item for item in payload if isinstance(item, dict)]


def _redirect_target_from_request(request) -> str:
    candidate = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if candidate.startswith("/"):
        return candidate
    return reverse("document_main")


def _redirect_with_import_state(
    request,
    *,
    status: str,
    imported: int = 0,
    replaced: int = 0,
    skipped: int = 0,
    error_code: str = "",
):
    query = {"synonyms_import": status}
    if status == "ok":
        query.update(
            {
                "synonyms_imported": str(imported),
                "synonyms_replaced": str(replaced),
                "synonyms_skipped": str(skipped),
            }
        )
    elif error_code:
        query["synonyms_error_code"] = error_code
    target = _redirect_target_from_request(request)
    separator = "&" if "?" in target else "?"
    return redirect(f"{target}{separator}{urlencode(query)}")


def _synonym_import_feedback(request) -> tuple[str, str]:
    status = (request.GET.get("synonyms_import") or "").strip().lower()
    if not status:
        return "", ""

    if status == "ok":
        imported = _to_int(request.GET.get("synonyms_imported"), 0)
        replaced = _to_int(request.GET.get("synonyms_replaced"), 0)
        skipped = _to_int(request.GET.get("synonyms_skipped"), 0)
        return (
            "success",
            f"Synonyms import complete: added {imported}, replaced {replaced}, skipped {skipped}.",
        )

    error_code = (request.GET.get("synonyms_error_code") or "").strip().lower()
    error_map = {
        "missing_file": "Select a JSON file before import.",
        "invalid_json": "Invalid JSON file.",
        "invalid_payload": "JSON structure is invalid. Expected array or {'synonyms': [...]} format.",
        "empty_import": "No valid synonym rows were found for import.",
    }
    return ("danger", error_map.get(error_code, "Synonyms import failed."))


def _synonym_payload_from_request(request) -> dict:
    payload = _request_payload(request)
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "dict"):
        return payload.dict()
    return dict(payload)


def _extract_synonym_fields(payload: dict) -> tuple[str, str, str | None]:
    code = str(payload.get("code") or "").strip()
    value = str(payload.get("value") or "").strip()
    example_raw = payload.get("example")
    example = (str(example_raw).strip() if example_raw is not None else "") or None
    return code, value, example


def _save_generator_file(generator: DocumentGenerator, cleaned_data: dict, replace_existing: bool) -> None:
    uploaded_file = cleaned_data.get("file")
    content = cleaned_data.get("content") or ""
    file_type = (cleaned_data.get("file_type") or generator.file_type or "txt").lower()

    if uploaded_file:
        if file_type == "txt":
            try:
                uploaded_bytes = uploaded_file.read()
                generator.content = uploaded_bytes.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Uploaded TXT template decode failed; keeping existing content title=%s", generator.title)
            finally:
                try:
                    uploaded_file.seek(0)
                except Exception:  # noqa: BLE001
                    pass
        if replace_existing and generator.file:
            generator.file.delete(save=False)
        generator.file = uploaded_file
        return

    if file_type == "txt":
        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        base_name = slugify(generator.title) or f"generator-{generator.pk or 'new'}"
        file_name = f"{base_name}-{timestamp}.txt"
        file_bytes = (content or "").encode("utf-8")
        if replace_existing and generator.file:
            generator.file.delete(save=False)
        generator.file.save(file_name, ContentFile(file_bytes), save=False)


def _b64_urlencode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_urldecode(raw: str) -> bytes:
    padding = "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _jwt_encode(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_part = _b64_urlencode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_part = _b64_urlencode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_part}.{payload_part}.{_b64_urlencode(signature)}"


def _jwt_decode(token: str, secret: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid jwt format")
    header_part, payload_part, signature_part = parts
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    expected_signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual_signature = _b64_urldecode(signature_part)
    if not hmac.compare_digest(expected_signature, actual_signature):
        raise ValueError("invalid jwt signature")
    return json.loads(_b64_urldecode(payload_part).decode("utf-8"))


def _jwt_secret() -> str:
    return (settings.ONLYOFFICE_JWT_SECRET or "").strip()


def _build_minimal_docx_bytes(text: str) -> bytes:
    paragraph_text = escape((text or "").strip() or "New document")
    now = timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ")

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
    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
            xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>SU Scholar</Application>
</Properties>
"""
    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:dcterms="http://purl.org/dc/terms/"
                   xmlns:dcmitype="http://purl.org/dc/dcmitype/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>SU Scholar Document</dc:title>
  <dc:creator>SU Scholar</dc:creator>
  <cp:lastModifiedBy>SU Scholar</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
"""
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r>
        <w:t xml:space="preserve">{paragraph_text}</w:t>
      </w:r>
    </w:p>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
      <w:cols w:space="708"/>
      <w:docGrid w:linePitch="360"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
  </w:style>
</w:styles>
"""
    document_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", styles_xml)
        archive.writestr("word/_rels/document.xml.rels", document_rels_xml)
    return buffer.getvalue()


def _save_document_file(document: Document, raw_bytes: bytes = b"") -> None:
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    base_name = slugify(document.title) or f"document-{document.id}"
    file_name = f"{base_name}-{timestamp}.docx"
    content = raw_bytes or _build_minimal_docx_bytes(document.content or "")

    if document.file:
        document.file.delete(save=False)
    document.file.save(file_name, ContentFile(content), save=False)
    document.file_type = "docx"


def _is_docx_file(file_name: str, file_type: str = "") -> bool:
    if str(file_type or "").strip().lower() == "docx":
        return True
    extension = os.path.splitext(file_name or "")[1].lower()
    return extension == ".docx"


def _is_valid_docx_payload(raw_bytes: bytes) -> bool:
    if not raw_bytes:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names:
                return False
            if "word/document.xml" not in names:
                return False
            archive.testzip()
    except Exception:  # noqa: BLE001
        return False
    return True


def _onlyoffice_file_params(file_name: str, fallback_file_type: str = "docx") -> tuple[str, str]:
    extension = os.path.splitext(file_name or "")[1].lower().lstrip(".")
    if not extension:
        extension = (fallback_file_type or "docx").lower()

    spreadsheet_types = {"xlsx", "xls", "ods", "csv"}
    presentation_types = {"pptx", "ppt", "odp"}
    if extension in spreadsheet_types:
        return "cell", extension
    if extension in presentation_types:
        return "slide", extension
    return "word", extension


def _build_onlyoffice_config_payload(
    *,
    file_url: str,
    callback_url: str,
    title: str,
    file_type: str,
    document_type: str,
    key: str,
    permissions: dict,
    user_id: int,
    user_name: str,
) -> dict:
    config = {
        "documentType": document_type,
        "document": {
            "title": title,
            "url": file_url,
            "fileType": file_type,
            "key": key,
            "permissions": {
                "edit": permissions["can_edit"],
                "download": permissions["can_download"],
                "print": permissions["can_print"],
                "comment": permissions["can_comment"],
                "review": permissions["can_review"],
            },
        },
        "editorConfig": {
            "mode": "edit" if permissions["can_edit"] else "view",
            "callbackUrl": callback_url,
            "lang": "ru",
            "coEditing": {
                "mode": "fast",
                "change": True,
            },
            "user": {
                "id": str(user_id),
                "name": user_name,
            },
            "customization": {
                "autosave": True,
                "forcesave": True,
                "uiTheme": "theme-light",
            },
        },
    }

    secret = _jwt_secret()
    if secret:
        config["token"] = _jwt_encode(config, secret)

    return config


def _document_key(document: Document) -> str:
    return f"doc-{document.id}-v{document.version}"


def _file_access_token(document: Document, user) -> str:
    payload = {
        "document_id": document.id,
        "user_id": user.id,
        "version": document.version,
    }
    return signing.dumps(payload, salt=FILE_TOKEN_SALT, compress=True)


def _validate_file_access_token(token: str, document: Document) -> bool:
    try:
        payload = signing.loads(token, salt=FILE_TOKEN_SALT, max_age=60 * 60 * 12)
    except signing.BadSignature:
        return False
    except signing.SignatureExpired:
        return False

    if payload.get("document_id") != document.id:
        return False

    user_id = payload.get("user_id")
    if not user_id:
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        return False

    return _can_view_document(document, user)


def _generator_permissions_for_user(generator: DocumentGenerator, user) -> dict:
    is_owner = bool(user and user.is_authenticated and generator.user_id == user.id)
    can_view = bool(generator.access_to_all or is_owner)
    if not can_view:
        return _no_permissions()

    can_edit = is_owner
    return {
        "can_view": True,
        "can_edit": can_edit,
        "can_comment": can_edit,
        "can_review": can_edit,
        "can_download": True,
        "can_print": True,
    }


def _generator_key(generator: DocumentGenerator) -> str:
    base = f"gen-{generator.id}-{int(generator.created_at.timestamp())}"
    if generator.file:
        try:
            size = generator.file.size
        except OSError:
            size = 0
        return f"{base}-s{size}"
    return base


def _generator_file_access_token(generator: DocumentGenerator, user) -> str:
    payload = {
        "generator_id": generator.id,
        "user_id": user.id,
    }
    return signing.dumps(payload, salt=GENERATOR_FILE_TOKEN_SALT, compress=True)


def _validate_generator_file_access_token(token: str, generator: DocumentGenerator) -> bool:
    try:
        payload = signing.loads(token, salt=GENERATOR_FILE_TOKEN_SALT, max_age=60 * 60 * 12)
    except signing.BadSignature:
        return False
    except signing.SignatureExpired:
        return False

    if payload.get("generator_id") != generator.id:
        return False

    user_id = payload.get("user_id")
    if not user_id:
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        return False

    return _generator_permissions_for_user(generator, user)["can_view"]


def _ensure_generator_file(generator: DocumentGenerator) -> None:
    if generator.file:
        return

    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    base_name = slugify(generator.title) or f"generator-{generator.id}"
    file_type = (generator.file_type or "").lower()

    if file_type == "docx":
        file_name = f"{base_name}-{timestamp}.docx"
        generator.file.save(file_name, ContentFile(_build_minimal_docx_bytes(generator.content or "")), save=False)
    else:
        file_name = f"{base_name}-{timestamp}.txt"
        generator.file.save(file_name, ContentFile((generator.content or "").encode("utf-8")), save=False)


def _serialize_document(request, document: Document, include_permissions: bool = True) -> dict:
    permissions = _document_permissions_for_user(document, request.user) if include_permissions else _no_permissions()
    return {
        "id": document.id,
        "title": document.title,
        "owner_id": document.user_id,
        "owner_name": document.user.get_full_name() or document.user.username,
        "file_type": document.file_type,
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
        "version": document.version,
        "is_deleted": document.is_deleted,
        "edit_url": reverse("document_edit", kwargs={"pk": document.pk}),
        "details_url": reverse("document_detail", kwargs={"pk": document.pk}),
        "permissions": permissions,
    }


def _build_onlyoffice_config(request, document: Document, permissions: dict) -> dict:
    file_token = _file_access_token(document, request.user)
    file_url = _absolute_url(
        request,
        f"{reverse('document_file', kwargs={'pk': document.pk})}?{urlencode({'access_token': file_token})}",
    )
    callback_url = _absolute_url(request, reverse("onlyoffice_callback", kwargs={"pk": document.pk}))
    user_name = request.user.get_full_name() or request.user.username or f"user-{request.user.pk}"
    file_name = document.file.name if document.file else ""
    document_type, file_type = _onlyoffice_file_params(file_name, fallback_file_type=document.file_type)
    title = os.path.basename(file_name) if file_name else (document.title or "document")

    config = _build_onlyoffice_config_payload(
        file_url=file_url,
        callback_url=callback_url,
        title=title,
        file_type=file_type,
        document_type=document_type,
        key=_document_key(document),
        permissions=permissions,
        user_id=request.user.pk,
        user_name=user_name,
    )
    return _append_project_agent_plugin_config(
        request,
        config,
        document_key=_document_key(document),
        target_type="project",
        target_id=document.id,
    )


def _build_generator_onlyoffice_config(request, generator: DocumentGenerator, permissions: dict) -> dict:
    file_token = _generator_file_access_token(generator, request.user)
    file_url = _absolute_url(
        request,
        f"{reverse('generator_file', kwargs={'pk': generator.pk})}?{urlencode({'access_token': file_token})}",
    )
    callback_url = _absolute_url(request, reverse("generator_onlyoffice_callback", kwargs={"pk": generator.pk}))
    user_name = request.user.get_full_name() or request.user.username or f"user-{request.user.pk}"
    file_name = generator.file.name if generator.file else ""
    document_type, file_type = _onlyoffice_file_params(file_name, fallback_file_type=generator.file_type)
    title = os.path.basename(file_name) if file_name else (generator.title or "shablon")

    return _build_onlyoffice_config_payload(
        file_url=file_url,
        callback_url=callback_url,
        title=title,
        file_type=file_type,
        document_type=document_type,
        key=_generator_key(generator),
        permissions=permissions,
        user_id=request.user.pk,
        user_name=user_name,
    )


def documents_collection(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    generators = _generators_for_user(request.user)
    if _json_request(request):
        return JsonResponse({"results": [_serialize_generator(generator) for generator in generators]})
    return render(
        request,
        "document/main.html",
        {
            "generators": generators,
        },
    )


@login_required(login_url="account_page")
def generator_create(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    create_form = DocumentGeneratorCreateForm(request.POST, request.FILES)
    if create_form.is_valid():
        generator = create_form.save(commit=False)
        generator.user = request.user
        generator.title = (generator.title or "").strip()[:255]
        if not generator.title:
            generator.title = "Жаңа шаблон"

        replace_existing = bool(request.FILES.get("file"))
        _save_generator_file(generator, create_form.cleaned_data, replace_existing=replace_existing)
        generator.save()
    else:
        generator = DocumentGenerator.objects.create(
            title="Жаңа шаблон",
            content="",
            file_type="docx",
            user=request.user,
            access_to_all=False,
        )
        _ensure_generator_file(generator)
        generator.save(update_fields=["file"])

    logger.info(
        "Template created id=%s owner=%s file_type=%s has_file=%s content_len=%s",
        generator.id,
        request.user.id,
        generator.file_type,
        bool(generator.file),
        len(generator.content or ""),
    )
    if _json_request(request):
        return JsonResponse(_serialize_generator(generator), status=201)
    return redirect("generator_edit", pk=generator.pk)


@login_required(login_url="account_page")
def synonyms_export(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    payload = {
        "synonyms": _serialize_synonyms(),
    }
    exported_at = timezone.now().strftime("%Y%m%d%H%M%S")
    response = HttpResponse(
        json.dumps(payload, ensure_ascii=False, indent=2),
        content_type="application/json; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="synonyms-{exported_at}.json"'
    logger.info("Synonyms exported user=%s count=%s", request.user.id, len(payload["synonyms"]))
    return response


@login_required(login_url="account_page")
def synonyms_import(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    upload = request.FILES.get("synonyms_file")
    if not upload:
        if _json_request(request):
            return JsonResponse({"detail": "File is required."}, status=400)
        return _redirect_with_import_state(request, status="error", error_code="missing_file")

    try:
        payload = json.loads(upload.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if _json_request(request):
            return JsonResponse({"detail": "Invalid JSON file."}, status=400)
        return _redirect_with_import_state(request, status="error", error_code="invalid_json")

    try:
        rows = _normalize_synonyms_payload(payload)
    except ValueError:
        if _json_request(request):
            return JsonResponse({"detail": "Invalid payload format."}, status=400)
        return _redirect_with_import_state(request, status="error", error_code="invalid_payload")

    imported = 0
    replaced = 0
    skipped = 0
    for item in rows:
        code = str(item.get("code") or "").strip()
        value = str(item.get("value") or "").strip()
        example_raw = item.get("example")
        example = (str(example_raw).strip() if example_raw is not None else "") or None

        if not code or not value:
            skipped += 1
            continue

        existing = Synonym.objects.filter(value=value)
        existing_count = existing.count()
        if existing_count:
            existing.delete()
            replaced += existing_count

        Synonym.objects.create(code=code, value=value, example=example)
        imported += 1

    if imported == 0:
        if _json_request(request):
            return JsonResponse({"detail": "No valid rows to import.", "skipped": skipped}, status=400)
        return _redirect_with_import_state(request, status="error", error_code="empty_import")

    logger.info(
        "Synonyms imported user=%s imported=%s replaced=%s skipped=%s",
        request.user.id,
        imported,
        replaced,
        skipped,
    )
    if _json_request(request):
        return JsonResponse({"status": "ok", "imported": imported, "replaced": replaced, "skipped": skipped})
    return _redirect_with_import_state(
        request,
        status="ok",
        imported=imported,
        replaced=replaced,
        skipped=skipped,
    )


@login_required(login_url="account_page")
def synonym_create(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    payload = _synonym_payload_from_request(request)
    code, value, example = _extract_synonym_fields(payload)
    if not code or not value:
        return JsonResponse({"detail": "Both code and value are required."}, status=400)

    if Synonym.objects.filter(value=value).exists():
        return JsonResponse({"detail": "Synonym with this value already exists."}, status=400)

    synonym = Synonym.objects.create(code=code, value=value, example=example)
    logger.info("Synonym created user=%s synonym_id=%s", request.user.id, synonym.id)
    return JsonResponse(
        {
            "id": synonym.id,
            "code": synonym.code,
            "value": synonym.value,
            "example": synonym.example or "",
        },
        status=201,
    )


@login_required(login_url="account_page")
def synonym_detail(request, pk: int):
    synonym = get_object_or_404(Synonym, pk=pk)

    if request.method == "GET":
        return JsonResponse(
            {
                "id": synonym.id,
                "code": synonym.code,
                "value": synonym.value,
                "example": synonym.example or "",
            }
        )

    if request.method in {"PATCH", "PUT"}:
        payload = _synonym_payload_from_request(request)
        code, value, example = _extract_synonym_fields(payload)
        if not code or not value:
            return JsonResponse({"detail": "Both code and value are required."}, status=400)
        if Synonym.objects.filter(value=value).exclude(pk=synonym.pk).exists():
            return JsonResponse({"detail": "Synonym with this value already exists."}, status=400)

        synonym.code = code
        synonym.value = value
        synonym.example = example
        synonym.save(update_fields=["code", "value", "example"])
        logger.info("Synonym updated user=%s synonym_id=%s", request.user.id, synonym.id)
        return JsonResponse(
            {
                "id": synonym.id,
                "code": synonym.code,
                "value": synonym.value,
                "example": synonym.example or "",
            }
        )

    if request.method == "DELETE":
        synonym_id = synonym.id
        synonym.delete()
        logger.info("Synonym deleted user=%s synonym_id=%s", request.user.id, synonym_id)
        return JsonResponse({"status": "deleted", "id": synonym_id})

    return HttpResponseNotAllowed(["GET", "PATCH", "PUT", "DELETE"])


@login_required(login_url="account_page")
def document_detail(request, pk: int):
    document = get_object_or_404(Document.objects.select_related("user"), pk=pk, is_deleted=False)
    permissions = _document_permissions_for_user(document, request.user)
    if not permissions["can_view"]:
        return JsonResponse({"detail": "Forbidden"}, status=403)

    if request.method == "GET":
        return JsonResponse(_serialize_document(request, document))

    if request.method == "DELETE":
        if document.user_id != request.user.id:
            return JsonResponse({"detail": "Only owner can delete"}, status=403)
        document.is_deleted = True
        document.save(update_fields=["is_deleted", "updated_at"])
        logger.info("Document deleted id=%s user=%s", document.id, request.user.id)
        return JsonResponse({"status": "deleted"})

    return HttpResponseNotAllowed(["GET", "DELETE"])


@login_required(login_url="account_page")
def generator_detail(request, pk: int):
    generator = get_object_or_404(DocumentGenerator.objects.select_related("user"), pk=pk)
    can_view = generator.user_id == request.user.id or generator.access_to_all
    can_edit = generator.user_id == request.user.id

    if not can_view:
        return JsonResponse({"detail": "Forbidden"}, status=403)

    if request.method == "GET":
        return JsonResponse(_serialize_generator(generator))

    if request.method == "DELETE":
        if not can_edit:
            return JsonResponse({"detail": "Only owner can delete"}, status=403)
        if generator.file:
            generator.file.delete(save=False)
        generator.delete()
        logger.info("Template deleted id=%s user=%s", pk, request.user.id)
        return JsonResponse({"status": "deleted"})

    return HttpResponseNotAllowed(["GET", "DELETE"])


@login_required(login_url="account_page")
def generator_edit_page(request, pk: int):
    generator = get_object_or_404(DocumentGenerator.objects.select_related("user"), pk=pk)
    permissions = _generator_permissions_for_user(generator, request.user)
    if not permissions["can_view"]:
        raise Http404("Template not available")

    if not generator.file:
        _ensure_generator_file(generator)
        generator.save(update_fields=["file"])

    form_status = 200
    if request.method == "POST":
        if not permissions["can_edit"]:
            return JsonResponse({"detail": "Only owner can edit"}, status=403)
        generator_form = DocumentGeneratorCreateForm(request.POST, request.FILES, instance=generator)
        if generator_form.is_valid():
            generator = generator_form.save(commit=False)
            replace_existing = bool(request.FILES.get("file"))
            _save_generator_file(generator, generator_form.cleaned_data, replace_existing=replace_existing)
            generator.save()
            logger.info(
                "Template updated id=%s owner=%s file_type=%s has_file=%s content_len=%s",
                generator.id,
                request.user.id,
                generator.file_type,
                bool(generator.file),
                len(generator.content or ""),
            )
            if _json_request(request):
                return JsonResponse(_serialize_generator(generator))
            return redirect("generator_edit", pk=generator.pk)
        form_status = 400
    else:
        generator_form = DocumentGeneratorCreateForm(instance=generator)

    api_js = f"{_dock_editor_origin()}/web-apps/apps/api/documents/api.js" if _dock_editor_origin() else ""
    synonyms = Synonym.objects.order_by("code", "value")
    import_notice_level, import_notice_text = _synonym_import_feedback(request)
    generated_count = generator.generated_documents.filter(is_deleted=False).count()
    return render(
        request,
        "document/onlyoffice_editor.html",
        {
            "object_title": generator.title,
            "object_subtitle": f"Шаблон #{generator.id} | Генерацияланған құжаттар: {generated_count}",
            "back_url": reverse("document_main"),
            "delete_url": reverse("generator_detail", kwargs={"pk": generator.pk}) if permissions["can_edit"] else "",
            "onlyoffice_api_js": api_js,
            "onlyoffice_config_url": reverse("generator_onlyoffice_config", kwargs={"pk": generator.pk}),
            "can_delete": permissions["can_edit"],
            "synonyms": synonyms,
            "synonym_import_enabled": permissions["can_edit"],
            "synonyms_export_url": reverse("synonyms_export"),
            "synonyms_import_url": reverse("synonyms_import"),
            "synonym_import_notice_level": import_notice_level,
            "synonym_import_notice_text": import_notice_text,
            "synonyms_update_url_template": reverse("synonym_detail", kwargs={"pk": 0}).replace("/0", "/__ID__"),
            "synonym_create_url": reverse("synonym_create"),
            "generator_form": generator_form,
            "generator_update_url": reverse("generator_edit", kwargs={"pk": generator.pk}),
        },
        status=form_status,
    )


@login_required(login_url="account_page")
@ensure_csrf_cookie
def document_edit_page(request, pk: int):
    document = get_object_or_404(Document.objects.select_related("user"), pk=pk, is_deleted=False)
    permissions = _document_permissions_for_user(document, request.user)
    if not permissions["can_view"]:
        raise Http404("Document not available")

    if not document.file:
        _save_document_file(document)
        document.save()

    api_js = f"{_dock_editor_origin()}/web-apps/apps/api/documents/api.js" if _dock_editor_origin() else ""
    return render(
        request,
        "document/onlyoffice_editor.html",
        {
            "object_title": document.title,
            "object_subtitle": f"Құжат #{document.id} | v{document.version}",
            "back_url": reverse("document_main"),
            "delete_url": reverse("document_detail", kwargs={"pk": document.pk}) if document.user_id == request.user.id else "",
            "onlyoffice_api_js": api_js,
            "onlyoffice_config_url": reverse("onlyoffice_config", kwargs={"pk": document.pk}),
            "can_delete": document.user_id == request.user.id,
        },
    )


@login_required(login_url="account_page")
def onlyoffice_config(request, pk: int):
    document = get_object_or_404(Document.objects.select_related("user"), pk=pk, is_deleted=False)
    permissions = _document_permissions_for_user(document, request.user)
    if not permissions["can_view"]:
        return JsonResponse({"detail": "Forbidden"}, status=403)

    config = _build_onlyoffice_config(request, document, permissions)
    logger.info(
        "ONLYOFFICE config document=%s user=%s mode=%s key=%s",
        document.id,
        request.user.id,
        config["editorConfig"]["mode"],
        config["document"]["key"],
    )
    return JsonResponse(config)


@login_required(login_url="account_page")
def generator_onlyoffice_config(request, pk: int):
    generator = get_object_or_404(DocumentGenerator.objects.select_related("user"), pk=pk)
    permissions = _generator_permissions_for_user(generator, request.user)
    if not permissions["can_view"]:
        return JsonResponse({"detail": "Forbidden"}, status=403)

    if not generator.file:
        _ensure_generator_file(generator)
        generator.save(update_fields=["file"])

    config = _build_generator_onlyoffice_config(request, generator, permissions)
    logger.info(
        "ONLYOFFICE config generator=%s user=%s mode=%s key=%s",
        generator.id,
        request.user.id,
        config["editorConfig"]["mode"],
        config["document"]["key"],
    )
    return JsonResponse(config)


def document_file(request, pk: int):
    document = get_object_or_404(Document, pk=pk, is_deleted=False)
    allowed = False

    if request.user.is_authenticated and _can_view_document(document, request.user):
        allowed = True
    else:
        token = (request.GET.get("access_token") or "").strip()
        if token:
            allowed = _validate_file_access_token(token, document)

    if not allowed:
        logger.warning("Document file access denied document=%s", pk)
        return JsonResponse({"detail": "Forbidden"}, status=403)

    if not document.file:
        raise Http404("File not found")

    try:
        stream = document.file.open("rb")
    except OSError:
        raise Http404("File not found")

    download_requested = (request.GET.get("download") or "").strip().lower() in {"1", "true", "yes"}
    content_type = mimetypes.guess_type(document.file.name)[0]
    if not content_type:
        if (document.file_type or "").lower() == "txt":
            content_type = "text/plain; charset=utf-8"
        else:
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    logger.info("Document file served document=%s", pk)
    return FileResponse(
        stream,
        as_attachment=download_requested,
        filename=document.file.name.rsplit("/", 1)[-1],
        content_type=content_type,
    )


def generator_file(request, pk: int):
    generator = get_object_or_404(DocumentGenerator, pk=pk)
    allowed = False

    if request.user.is_authenticated and _generator_permissions_for_user(generator, request.user)["can_view"]:
        allowed = True
    else:
        token = (request.GET.get("access_token") or "").strip()
        if token:
            allowed = _validate_generator_file_access_token(token, generator)

    if not allowed:
        logger.warning("Generator file access denied generator=%s", pk)
        return JsonResponse({"detail": "Forbidden"}, status=403)

    if not generator.file:
        raise Http404("File not found")

    try:
        stream = generator.file.open("rb")
    except OSError:
        raise Http404("File not found")

    download_requested = (request.GET.get("download") or "").strip().lower() in {"1", "true", "yes"}
    content_type = mimetypes.guess_type(generator.file.name)[0]
    if not content_type:
        if (generator.file_type or "").lower() == "txt":
            content_type = "text/plain; charset=utf-8"
        else:
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    logger.info("Generator file served generator=%s", pk)
    return FileResponse(
        stream,
        as_attachment=download_requested,
        filename=generator.file.name.rsplit("/", 1)[-1],
        content_type=content_type,
    )


@csrf_exempt
def onlyoffice_callback(request, pk: int):
    if request.method != "POST":
        return JsonResponse({"error": 0})

    document = Document.objects.filter(pk=pk, is_deleted=False).first()
    if not document:
        return JsonResponse({"error": 1}, status=404)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("ONLYOFFICE callback invalid JSON document=%s", pk)
        return JsonResponse({"error": 1}, status=400)

    secret = _jwt_secret()
    if secret:
        token = payload.get("token")
        if not token:
            logger.warning("ONLYOFFICE callback token missing while JWT is enabled document=%s", pk)
            return JsonResponse({"error": 1}, status=403)
        try:
            payload = _jwt_decode(token, secret)
            payload["token"] = token
        except Exception as exc:  # noqa: BLE001
            logger.warning("ONLYOFFICE callback JWT invalid document=%s error=%s", pk, exc)
            return JsonResponse({"error": 1}, status=403)

    status = int(payload.get("status", 0))
    logger.info("ONLYOFFICE callback document=%s status=%s", pk, status)

    if status in ONLYOFFICE_ERROR_STATUSES:
        logger.warning("ONLYOFFICE callback save error document=%s payload=%s", pk, payload)
        return JsonResponse({"error": 0})

    if status not in ONLYOFFICE_SAVE_STATUSES:
        return JsonResponse({"error": 0})

    file_url = (payload.get("url") or "").strip()
    if not file_url:
        logger.warning("ONLYOFFICE callback missing file url document=%s", pk)
        return JsonResponse({"error": 1}, status=400)

    request_headers = {}
    if secret and payload.get("token"):
        request_headers["Authorization"] = f"Bearer {payload['token']}"

    try:
        downloaded = requests.get(file_url, headers=request_headers, timeout=90)
        downloaded.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("ONLYOFFICE callback download failed document=%s url=%s error=%s", pk, file_url, exc)
        return JsonResponse({"error": 1}, status=502)

    if _is_docx_file(getattr(document.file, "name", ""), document.file_type):
        if not _is_valid_docx_payload(downloaded.content):
            logger.warning(
                "ONLYOFFICE callback invalid DOCX payload document=%s size=%s",
                pk,
                len(downloaded.content or b""),
            )
            return JsonResponse({"error": 1}, status=422)

    _save_document_file(document, downloaded.content)
    document.version = document.version + 1
    document.save()
    logger.info("ONLYOFFICE callback saved document=%s new_version=%s", pk, document.version)
    return JsonResponse({"error": 0})


@csrf_exempt
def generator_onlyoffice_callback(request, pk: int):
    if request.method != "POST":
        return JsonResponse({"error": 0})

    generator = DocumentGenerator.objects.filter(pk=pk).first()
    if not generator:
        return JsonResponse({"error": 1}, status=404)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("ONLYOFFICE callback invalid JSON generator=%s", pk)
        return JsonResponse({"error": 1}, status=400)

    secret = _jwt_secret()
    if secret:
        token = payload.get("token")
        if not token:
            logger.warning("ONLYOFFICE callback token missing while JWT is enabled generator=%s", pk)
            return JsonResponse({"error": 1}, status=403)
        try:
            payload = _jwt_decode(token, secret)
            payload["token"] = token
        except Exception as exc:  # noqa: BLE001
            logger.warning("ONLYOFFICE callback JWT invalid generator=%s error=%s", pk, exc)
            return JsonResponse({"error": 1}, status=403)

    status = int(payload.get("status", 0))
    logger.info("ONLYOFFICE callback generator=%s status=%s", pk, status)

    if status in ONLYOFFICE_ERROR_STATUSES:
        logger.warning("ONLYOFFICE callback save error generator=%s payload=%s", pk, payload)
        return JsonResponse({"error": 0})

    if status not in ONLYOFFICE_SAVE_STATUSES:
        return JsonResponse({"error": 0})

    file_url = (payload.get("url") or "").strip()
    if not file_url:
        logger.warning("ONLYOFFICE callback missing file url generator=%s", pk)
        return JsonResponse({"error": 1}, status=400)

    request_headers = {}
    if secret and payload.get("token"):
        request_headers["Authorization"] = f"Bearer {payload['token']}"

    try:
        downloaded = requests.get(file_url, headers=request_headers, timeout=90)
        downloaded.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("ONLYOFFICE callback download failed generator=%s url=%s error=%s", pk, file_url, exc)
        return JsonResponse({"error": 1}, status=502)

    if _is_docx_file(getattr(generator.file, "name", ""), generator.file_type):
        if not _is_valid_docx_payload(downloaded.content):
            logger.warning(
                "ONLYOFFICE callback invalid DOCX payload generator=%s size=%s",
                pk,
                len(downloaded.content or b""),
            )
            return JsonResponse({"error": 1}, status=422)

    file_name = generator.file.name.rsplit("/", 1)[-1] if generator.file else f"generator-{generator.id}.docx"
    if generator.file:
        generator.file.delete(save=False)
    generator.file.save(file_name, ContentFile(downloaded.content), save=False)

    if (generator.file_type or "").lower() == "txt":
        try:
            generator.content = downloaded.content.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Generator TXT content decode failed generator=%s", generator.id)

    generator.save()
    logger.info("ONLYOFFICE callback saved generator=%s file=%s", pk, generator.file.name if generator.file else "")
    return JsonResponse({"error": 0})
