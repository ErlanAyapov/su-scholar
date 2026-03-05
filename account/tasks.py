from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import models
from requests import RequestException

from account.services.satbayev_scraper import build_user_full_name, load_teacher_profile_data

User = get_user_model()


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
        if force or current in (None, "", []):
            payload[field] = value
    return payload


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
    if not payload:
        return {"status": "ok", "updated": False, "user_id": user_id}

    User.objects.filter(pk=user.pk).update(**payload)
    return {"status": "ok", "updated": True, "user_id": user_id, "fields": list(payload.keys())}


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
        )

    user_ids = list(queryset.values_list("id", flat=True)[:limit])
    for user_id in user_ids:
        enrich_user_profile_from_satbayev.delay(user_id=user_id, force=force)

    return {"status": "queued", "count": len(user_ids), "force": force}
