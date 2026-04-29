from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jwt as pyjwt
import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from document.models import Document
from main.models import PublicationFile


_DOCUMENT_KEY_RE = re.compile(r"^doc-(?P<document_id>\d+)-v(?P<version>\d+)$")
_PUBLICATION_FILE_KEY_RE = re.compile(
    r"^project-(?P<project_id>\d+)-publication-file-(?P<file_id>\d+)-u(?P<updated_at>\d+)-s(?P<size>\d+)$"
)


def _document_file_type_from_extension(extension: str) -> str:
    normalized = (extension or "").strip().lower().lstrip(".")
    if normalized in {"docx", "doc", "odt", "rtf"}:
        return "docx"
    if normalized in {"xlsx", "xls", "ods", "csv"}:
        return "excel"
    if normalized in {"pptx", "ppt", "odp"}:
        return "pptx"
    if normalized == "pdf":
        return "pdf"
    if normalized == "txt":
        return "txt"
    return "other"


class OnlyOfficeClient:
    def __init__(self, document_server_url: str | None = None, jwt_secret: str | None = None, timeout: float = 90.0):
        configured_url = (
            document_server_url
            or getattr(settings, "ONLYOFFICE_DOCUMENT_SERVER_URL", "")
            or getattr(settings, "DOCK_EDITOR_URL", "")
        )
        self.document_server_url = str(configured_url or "").strip().rstrip("/")
        self.jwt_secret = str(jwt_secret or getattr(settings, "ONLYOFFICE_JWT_SECRET", "") or "").strip()
        self.timeout = float(timeout)

        if not self.document_server_url:
            raise ValueError("OnlyOffice document server URL is not configured.")

    @property
    def command_service_url(self) -> str:
        return f"{self.document_server_url}/coauthoring/CommandService.ashx"

    def _sign(self, payload: dict) -> dict:
        if not self.jwt_secret:
            return dict(payload)
        token = pyjwt.encode(payload, self.jwt_secret, algorithm="HS256")
        return {**payload, "token": token}

    def _post_command(self, payload: dict) -> dict:
        signed_payload = self._sign(payload)
        headers = {"Content-Type": "application/json"}
        if self.jwt_secret and signed_payload.get("token"):
            headers["Authorization"] = f"Bearer {signed_payload['token']}"

        response = requests.post(
            self.command_service_url,
            json=signed_payload,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError("OnlyOffice CommandService returned invalid JSON.") from exc

        if not isinstance(data, dict):
            raise ValueError("OnlyOffice CommandService returned unexpected response type.")
        return data

    def get_download_url(self, document_key: str) -> str:
        payload = {"c": "info", "key": str(document_key or "").strip()}
        response = self._post_command(payload)
        try:
            error_code = int(response.get("error", 0) or 0)
        except (TypeError, ValueError):
            error_code = 1
        if error_code != 0:
            raise RuntimeError(f"OnlyOffice info command failed: {response}")

        file_url = (
            response.get("fileUrl")
            or response.get("url")
            or (response.get("data") or {}).get("fileUrl")
            or (response.get("data") or {}).get("url")
        )
        file_url = str(file_url or "").strip()
        if not file_url:
            raise RuntimeError(f"OnlyOffice info command did not return file URL: {response}")
        return file_url

    def download_docx(self, document_key: str, save_path: str) -> str:
        download_url = self.get_download_url(document_key)
        response = requests.get(download_url, stream=True, timeout=self.timeout)
        response.raise_for_status()

        target_path = Path(save_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("wb") as target_stream:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    target_stream.write(chunk)
        return str(target_path)

    @staticmethod
    def _build_storage_name(base_label: str, extension: str) -> str:
        normalized_extension = str(extension or "docx").strip().lower().lstrip(".") or "docx"
        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        base_name = slugify(base_label) or "document"
        return f"{base_name}-{timestamp}.{normalized_extension}"

    def _resolve_target_by_key(self, document_key: str) -> tuple[str, Any] | tuple[None, None]:
        key = str(document_key or "").strip()
        document_match = _DOCUMENT_KEY_RE.match(key)
        if document_match:
            document_id = int(document_match.group("document_id"))
            document = Document.objects.filter(id=document_id, is_deleted=False).first()
            if document:
                return "project_document", document
            return None, None

        publication_file_match = _PUBLICATION_FILE_KEY_RE.match(key)
        if publication_file_match:
            project_id = int(publication_file_match.group("project_id"))
            file_id = int(publication_file_match.group("file_id"))
            publication_file = (
                PublicationFile.objects.filter(
                    id=file_id,
                    publication__publicationproject__project_id=project_id,
                    publication__private=False,
                )
                .select_related("publication")
                .first()
            )
            if publication_file:
                return "publication_file", publication_file
            return None, None

        return None, None

    @transaction.atomic
    def upload_docx(self, file_path: str, document_key: str) -> bool:
        target_type, target_object = self._resolve_target_by_key(document_key)
        if not target_type or target_object is None:
            raise ValueError(f"Unable to resolve storage target by document key: {document_key}")

        source_path = Path(file_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Edited file not found: {source_path}")
        raw_bytes = source_path.read_bytes()
        if not raw_bytes:
            raise ValueError("Edited file is empty.")

        if target_type == "project_document":
            document: Document = target_object
            existing_extension = Path(document.file.name).suffix.lower().lstrip(".") if document.file else ""
            source_extension = source_path.suffix.lower().lstrip(".")
            extension = existing_extension or source_extension or "docx"
            file_name = self._build_storage_name(document.title or f"document-{document.id}", extension)

            if document.file:
                document.file.delete(save=False)
            document.file.save(file_name, ContentFile(raw_bytes), save=False)
            document.file_type = _document_file_type_from_extension(extension)
            document.save()
            return True

        publication_file: PublicationFile = target_object
        publication = publication_file.publication
        existing_extension = Path(publication_file.file.name).suffix.lower().lstrip(".") if publication_file.file else ""
        source_extension = source_path.suffix.lower().lstrip(".")
        extension = existing_extension or source_extension or "docx"
        file_name = self._build_storage_name(
            publication_file.description or publication.title_original or f"publication-file-{publication_file.id}",
            extension,
        )

        if publication_file.file:
            publication_file.file.delete(save=False)
        publication_file.file.save(file_name, ContentFile(raw_bytes), save=False)
        publication_file.save(update_fields=["file"])

        publication.updated_at = timezone.now()
        publication.save(update_fields=["updated_at"])
        return True

    def force_save(self, document_key: str) -> bool:
        payload = {"c": "forcesave", "key": str(document_key or "").strip()}
        response = self._post_command(payload)
        try:
            error_code = int(response.get("error", 0) or 0)
        except (TypeError, ValueError):
            return False
        return error_code == 0
