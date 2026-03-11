from celery import shared_task
from django.contrib.auth import get_user_model
from requests import RequestException

from main.services.publication_importer import (
    import_publications_for_user,
    import_scholar_works_for_all_users,
    import_works_from_scholar,
)

User = get_user_model()

@shared_task
def ping():
    return 'pong'


@shared_task(
    bind=True,
    autoretry_for=(RequestException,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def import_user_publications_task(self, user_id: int, force: bool = False) -> dict:
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"status": "not_found", "user_id": user_id}

    result = import_publications_for_user(user=user, force=force)
    return {"status": "ok", **result}


@shared_task
def import_publications_for_all_users_task(limit: int = 100, force: bool = False) -> dict:
    user_ids = list(User.objects.filter(is_user=False).order_by("id").values_list("id", flat=True)[:limit])
    for user_id in user_ids:
        import_user_publications_task.delay(user_id=user_id, force=force)
    return {"status": "queued", "count": len(user_ids), "force": force}

@shared_task
def import_publications_from_google_scholar_task(user_id: int, force: bool = False) -> dict:
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"status": "not_found", "user_id": user_id}

    query = (user.google_scholar or "").strip()
    if not query:
        query = " ".join(part for part in [user.last_name, user.first_name, user.father_name] if part).strip() or user.username

    result = import_works_from_scholar(user=user, query=query)
    return {"status": "ok", **result, "force": force}


@shared_task
def import_publications_from_google_scholar_for_all_users_task() -> dict:
    summary = import_scholar_works_for_all_users()
    return {"status": "ok", **summary}
