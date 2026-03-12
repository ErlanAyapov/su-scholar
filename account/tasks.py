import logging
import mimetypes
import re
from urllib.parse import urlparse

import requests
from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import models
from requests import RequestException

from account.services.satbayev_scraper import REQUEST_HEADERS, build_user_full_name, load_teacher_profile_data
from main.realtime import notify_public

User = get_user_model()
logger = logging.getLogger(__name__)
PLACEHOLDER_EMAIL_RE = re.compile(r"@[A-Za-z0-9.-]+\.(?:111|222|333)$", re.IGNORECASE)


def _build_update_payload(user, profile_data: dict, force: bool = False) -> dict:
    fields = [
        "email",
        "scopus_id",
        "orc_id",
        "wos_id",
        "researchgate",
        "google_scholar",
        "satbayev_profile_url",
        "journal_links",
        "journal_ids",
    ]

    payload = {}
    for field in fields:
        value = profile_data.get(field)
        if value in (None, "", []):
            continue
        current = getattr(user, field, None)
        should_replace_placeholder_email = field == "email" and bool(
            isinstance(current, str) and PLACEHOLDER_EMAIL_RE.search(current.strip())
        )
        if force or current in (None, "", []) or should_replace_placeholder_email:
            payload[field] = value
    return payload


def _extension_from_response(photo_url: str, content_type: str) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    ext = mimetypes.guess_extension(mime) if mime else ""
    if ext:
        return ext

    path = urlparse(photo_url).path
    if "." in path.rsplit("/", 1)[-1]:
        return "." + path.rsplit(".", 1)[-1].lower()
    return ".jpg"


def _update_user_photo_from_satbayev(user, photo_url: str, force: bool = False, timeout: int = 20) -> bool:
    url = (photo_url or "").strip()
    if not url:
        return False
    if user.photo and not force:
        return False

    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to download Satbayev photo for user_id=%s url=%s error=%s", user.id, url, exc)
        return False

    content = response.content or b""
    if not content:
        return False

    content_type = response.headers.get("Content-Type", "")
    if content_type and not content_type.lower().startswith("image/"):
        logger.warning(
            "Satbayev photo response is not image for user_id=%s url=%s content_type=%s",
            user.id,
            url,
            content_type,
        )
        return False

    ext = _extension_from_response(url, content_type)
    filename = f"satbayev_{user.id}{ext}"
    user.photo.save(filename, ContentFile(content), save=False)
    return True


@shared_task(
    bind=True,
    autoretry_for=(RequestException,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def enrich_user_profile_from_satbayev(self, user_id: int, force: bool = False) -> dict:
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"status": "not_found", "user_id": user_id}

    full_name = build_user_full_name(user)
    if not full_name:
        return {"status": "skipped", "reason": "empty_name", "user_id": user_id}

    profile_data = load_teacher_profile_data(full_name=full_name)
    if not profile_data:
        return {"status": "not_found", "reason": "profile_not_found", "user_id": user_id}

    payload = _build_update_payload(user=user, profile_data=profile_data, force=force)
    changed_fields = []
    for field_name, field_value in payload.items():
        setattr(user, field_name, field_value)
        changed_fields.append(field_name)

    if _update_user_photo_from_satbayev(
        user=user,
        photo_url=profile_data.get("photo_url", ""),
        force=force,
    ):
        changed_fields.append("photo")

    if not changed_fields:
        return {"status": "ok", "updated": False, "user_id": user_id}

    user.save(update_fields=sorted(set(changed_fields)))
    return {"status": "ok", "updated": True, "user_id": user_id, "fields": sorted(set(changed_fields))}


@shared_task
def enqueue_satbayev_enrichment(limit: int = 50, force: bool = False) -> dict:
    queryset = User.objects.filter(is_user=False).order_by("id")
    if not force:
        queryset = queryset.filter(
            models.Q(satbayev_profile_url="")
            | models.Q(scopus_id="")
            | models.Q(orc_id="")
            | models.Q(wos_id="")
            | models.Q(researchgate="")
            | models.Q(google_scholar="")
            | models.Q(photo="")
            | models.Q(email__iendswith=".111")
            | models.Q(email__iendswith=".222")
            | models.Q(email__iendswith=".333")
        )

    user_ids = list(queryset.values_list("id", flat=True)[:limit])
    for user_id in user_ids:
        enrich_user_profile_from_satbayev.delay(user_id=user_id, force=force)

    notify_public(
        "Satbayev enrichment поставлен в очередь",
        source="satbayev",
        queued=len(user_ids),
        force=force,
    )

    return {"status": "queued", "count": len(user_ids), "force": force}
