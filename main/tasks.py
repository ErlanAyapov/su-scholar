from celery import shared_task
from django.contrib.auth import get_user_model
from requests import RequestException

from main.services.publication_importer import import_publications_for_user

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
