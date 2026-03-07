import base64
import hashlib
import hmac
import io
import json
import logging
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
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from document.models import Document, DocumentPermission, Synonym


logger = logging.getLogger(__name__)
User = get_user_model()

ONLYOFFICE_SAVE_STATUSES = {2, 6}
ONLYOFFICE_ERROR_STATUSES = {3, 7}
FILE_TOKEN_SALT = "document-file-access"


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
  <Application>SU Science</Application>
</Properties>
"""
    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:dcterms="http://purl.org/dc/terms/"
                   xmlns:dcmitype="http://purl.org/dc/dcmitype/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>SU Science Document</dc:title>
  <dc:creator>SU Science</dc:creator>
  <cp:lastModifiedBy>SU Science</cp:lastModifiedBy>
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
    title = (document.title or "document").strip()
    if not title.lower().endswith(".docx"):
        title = f"{title}.docx"

    config = {
        "documentType": "word",
        "document": {
            "title": title,
            "url": file_url,
            "fileType": "docx",
            "key": _document_key(document),
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
                "id": str(request.user.pk),
                "name": user_name,
            },
            "customization": {
                "autosave": True,
                "forcesave": True,
            },
        },
    }

    secret = _jwt_secret()
    if secret:
        config["token"] = _jwt_encode(config, secret)

    return config


@login_required(login_url="account_page")
def documents_collection(request):
    if request.method == "GET":
        documents = _documents_for_user(request.user)
        if _json_request(request):
            return JsonResponse({"results": [_serialize_document(request, doc) for doc in documents]})
        return render(
            request,
            "document/main.html",
            {
                "documents": documents,
            },
        )

    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])

    payload = _request_payload(request)
    title = (payload.get("title") or "").strip() or "New document"
    content = (payload.get("content") or "").strip()

    document = Document.objects.create(
        title=title[:255],
        content=content,
        user=request.user,
        file_type="docx",
    )
    _save_document_file(document)
    document.save()
    logger.info("Document created id=%s user=%s", document.id, request.user.id)

    if _json_request(request):
        return JsonResponse(_serialize_document(request, document), status=201)
    return redirect("document_edit", pk=document.pk)


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
    synonyms = Synonym.objects.order_by("code", "value")
    return render(
        request,
        "document/generator_form.html",
        {
            "document_obj": document,
            "onlyoffice_api_js": api_js,
            "onlyoffice_config_url": reverse("onlyoffice_config", kwargs={"pk": document.pk}),
            "can_delete": document.user_id == request.user.id,
            "synonyms": synonyms,
            "public_base_url": _build_public_base_url(request),
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

    logger.info("Document file served document=%s", pk)
    return FileResponse(
        stream,
        as_attachment=False,
        filename=document.file.name.rsplit("/", 1)[-1],
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
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

    _save_document_file(document, downloaded.content)
    document.version = document.version + 1
    document.save()
    logger.info("ONLYOFFICE callback saved document=%s new_version=%s", pk, document.version)
    return JsonResponse({"error": 0})
