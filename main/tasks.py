from celery import group, shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from requests import RequestException

from account.tasks import enrich_user_profile_from_satbayev
from core.tasks.logged_task import LoggedTask
from main.models import Author, Publication
from main.realtime import notify_public, notify_user
from main.services.publication_importer import (
    enrich_publications_with_abstracts,
    import_publications_for_user,
    import_scholar_works_for_all_users,
    import_works_from_scholar,
)
from main.services.publication_pipeline import run_publication_pipeline
from main.services.publication_pipeline.author_linking import populate_author_identity, relink_author_instance
from utils.email_service import send_system_email

User = get_user_model()
SYNC_SOURCE_LABELS = {
    "all": "Барлык порталдар",
    "orcid": "ORCID",
    "scopus": "Scopus",
    "scholar": "Google Scholar",
    "wos": "Web of Science",
}

PROFILE_SYNC_SOURCE_LABELS = {
    "orcid": "ORCID",
    "scopus": "Scopus/OpenAlex",
    "wos": "Web of Science (OpenAlex fallback)",
    "scholar": "Google Scholar",
    "satbayev": "Satbayev Profile",
}


def _notify_sync_participants(
    *,
    target_user_id: int,
    initiated_by_id: int | None,
    message: str,
    level: str = "info",
    source: str = "",
    **extra,
) -> None:
    payload = {
        "target_user_id": target_user_id,
        "source": source,
        "timestamp": timezone.now().isoformat(),
        **extra,
    }
    notify_user(target_user_id, message, level=level, event_type="sync_log", **payload)
    if initiated_by_id and initiated_by_id != target_user_id:
        notify_user(initiated_by_id, message, level=level, event_type="sync_log", **payload)


def _merge_sync_result(summary: dict, result: dict) -> None:
    summary["works_total"] += int(result.get("works_total") or 0)
    summary["created"] += int(result.get("created") or 0)
    summary["updated"] += int(result.get("updated") or 0)
    summary["skipped"] += int(result.get("skipped") or 0)
    summary["errors"].extend(result.get("errors") or [])


def _publication_public_url(publication_id: int) -> str:
    path = reverse("publication_detail", kwargs={"pk": publication_id})
    public_base = str(getattr(settings, "APP_PUBLIC_URL", "") or "").strip().rstrip("/")
    if not public_base:
        return path
    return f"{public_base}{path}"


def _profile_public_url(user_id: int) -> str:
    path = reverse("employee_profile", kwargs={"user_id": user_id})
    public_base = str(getattr(settings, "APP_PUBLIC_URL", "") or "").strip().rstrip("/")
    if not public_base:
        return path
    return f"{public_base}{path}"


def _send_profile_sync_email_report(
    *,
    user,
    source_stats: dict,
    publication_ids: list[int],
) -> bool:
    recipient = str(user.email or "").strip()
    if not recipient:
        return False

    publications = list(
        Publication.objects.filter(id__in=publication_ids)
        .only("id", "title_original")
        .order_by("-id")
    )

    full_name = user.get_full_name().strip() or user.username
    subject = "SU Scholar: синхронизация профиля завершена"
    lines = [
        f"Здравствуйте, {full_name}!",
        "",
        "Синхронизация профиля завершена.",
        "",
        "Сводка по источникам:",
    ]
    for source_key, source_data in source_stats.items():
        label = PROFILE_SYNC_SOURCE_LABELS.get(source_key, source_key)
        lines.append(
            (
                f"- {label}: найдено {int(source_data.get('works_total', 0))}, "
                f"создано {int(source_data.get('created', 0))}, "
                f"обновлено {int(source_data.get('updated', 0))}, "
                f"пропущено {int(source_data.get('skipped', 0))}"
            )
        )

    lines.extend(["", f"Публикации в системе ({len(publications)}):"])
    if publications:
        for publication in publications:
            title = (publication.title_original or f"Publication #{publication.id}").strip()
            lines.append(f"- {title}: {_publication_public_url(publication.id)}")
    else:
        lines.append("- Новых публикаций не найдено.")

    lines.extend(["", f"Профиль: {_profile_public_url(user.id)}"])

    html_items = ""
    for publication in publications:
        title = (publication.title_original or f"Publication #{publication.id}").strip()
        html_items += (
            f'<li><a href="{_publication_public_url(publication.id)}">{title}</a></li>'
        )
    if not html_items:
        html_items = "<li>Новых публикаций не найдено.</li>"

    html_body = (
        f"<p>Здравствуйте, {full_name}!</p>"
        "<p>Синхронизация профиля завершена.</p>"
        "<p><strong>Сводка по источникам:</strong></p>"
        "<ul>"
        + "".join(
            (
                f"<li>{PROFILE_SYNC_SOURCE_LABELS.get(source_key, source_key)}: "
                f"найдено {int(source_data.get('works_total', 0))}, "
                f"создано {int(source_data.get('created', 0))}, "
                f"обновлено {int(source_data.get('updated', 0))}, "
                f"пропущено {int(source_data.get('skipped', 0))}</li>"
            )
            for source_key, source_data in source_stats.items()
        )
        + "</ul>"
        + f"<p><strong>Публикации в системе ({len(publications)}):</strong></p>"
        + f"<ul>{html_items}</ul>"
        + f'<p>Профиль: <a href="{_profile_public_url(user.id)}">{_profile_public_url(user.id)}</a></p>'
    )

    send_system_email(
        subject=subject,
        body="\n".join(lines),
        html_body=html_body,
        recipients=[recipient],
        fail_silently=False,
    )
    return True

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
    notify_user(
        user.id,
        "Публикациялар импорты аяқталды",
        source="openalex_orcid",
        created=result.get("created", 0),
        updated=result.get("updated", 0),
        skipped=result.get("skipped", 0),
    )
    return {"status": "ok", **result}


@shared_task
def sync_user_publications_task(
    user_id: int,
    source: str = "all",
    initiated_by_id: int | None = None,
    force: bool = False,
) -> dict:
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"status": "not_found", "user_id": user_id}

    source = (source or "all").strip().lower()
    allowed_sources = {"all", "orcid", "scopus", "scholar", "wos"}
    if source not in allowed_sources:
        return {"status": "invalid_source", "source": source, "allowed": sorted(allowed_sources)}

    profile_name = user.get_full_name() or user.username or f"user-{user.id}"
    _notify_sync_participants(
        target_user_id=user.id,
        initiated_by_id=initiated_by_id,
        source=source,
        message=f"Синхрондау басталды: {SYNC_SOURCE_LABELS.get(source, source)} ({profile_name})",
        level="info",
    )

    summary = {
        "status": "ok",
        "user_id": user.id,
        "source": source,
        "works_total": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    if source == "all":
        base_result = import_publications_for_user(user=user, force=force)
        _merge_sync_result(summary, base_result)
        _notify_sync_participants(
            target_user_id=user.id,
            initiated_by_id=initiated_by_id,
            source="orcid",
            message=(
                f"ORCID/OpenAlex: жасалды {base_result.get('created', 0)}, "
                f"жаңартылды {base_result.get('updated', 0)}, өткізіліп жіберілді {base_result.get('skipped', 0)}"
            ),
            level="info",
            created=base_result.get("created", 0),
            updated=base_result.get("updated", 0),
            skipped=base_result.get("skipped", 0),
            errors=len(base_result.get("errors", [])),
        )

        scholar_query = (user.google_scholar or "").strip()
        if not scholar_query:
            scholar_query = " ".join(
                part for part in [user.last_name, user.first_name, user.father_name] if part
            ).strip() or user.username

        scholar_result = import_works_from_scholar(user=user, query=scholar_query, force=force)
        _merge_sync_result(summary, scholar_result)
        _notify_sync_participants(
            target_user_id=user.id,
            initiated_by_id=initiated_by_id,
            source="scholar",
            message=(
                f"Google Scholar: жасалды {scholar_result.get('created', 0)}, "
                f"жаңартылды {scholar_result.get('updated', 0)}, өткізіліп жіберілді {scholar_result.get('skipped', 0)}"
            ),
            level="info",
            created=scholar_result.get("created", 0),
            updated=scholar_result.get("updated", 0),
            skipped=scholar_result.get("skipped", 0),
            errors=len(scholar_result.get("errors", [])),
        )

    elif source == "orcid":
        if not user.orc_id:
            summary["errors"].append("orcid:missing_orcid_id")
            _notify_sync_participants(
                target_user_id=user.id,
                initiated_by_id=initiated_by_id,
                source=source,
                message="ORCID ID көрсетілмеген. Алдымен профильге ORCID енгізіңіз.",
                level="warning",
            )
        else:
            result = import_publications_for_user(user=user, force=force, sources=("orcid",))
            _merge_sync_result(summary, result)
            _notify_sync_participants(
                target_user_id=user.id,
                initiated_by_id=initiated_by_id,
                source=source,
                message=(
                    f"ORCID: жасалды {result.get('created', 0)}, "
                    f"жаңартылды {result.get('updated', 0)}, өткізіліп жіберілді {result.get('skipped', 0)}"
                ),
                level="info",
                created=result.get("created", 0),
                updated=result.get("updated", 0),
                skipped=result.get("skipped", 0),
                errors=len(result.get("errors", [])),
            )

    elif source == "scopus":
        if not user.scopus_id:
            summary["errors"].append("scopus:missing_scopus_id")
            _notify_sync_participants(
                target_user_id=user.id,
                initiated_by_id=initiated_by_id,
                source=source,
                message="Scopus ID көрсетілмеген. Алдымен профильге Scopus ID енгізіңіз.",
                level="warning",
            )
        else:
            result = import_publications_for_user(
                user=user,
                force=force,
                sources=("openalex",),
                prefer_scopus=True,
            )
            _merge_sync_result(summary, result)
            _notify_sync_participants(
                target_user_id=user.id,
                initiated_by_id=initiated_by_id,
                source=source,
                message=(
                    f"Scopus/OpenAlex: жасалды {result.get('created', 0)}, "
                    f"жаңартылды {result.get('updated', 0)}, өткізіліп жіберілді {result.get('skipped', 0)}"
                ),
                level="info",
                created=result.get("created", 0),
                updated=result.get("updated", 0),
                skipped=result.get("skipped", 0),
                errors=len(result.get("errors", [])),
            )

    elif source == "scholar":
        scholar_query = (user.google_scholar or "").strip()
        if not scholar_query:
            scholar_query = " ".join(
                part for part in [user.last_name, user.first_name, user.father_name] if part
            ).strip() or user.username
        result = import_works_from_scholar(user=user, query=scholar_query, force=force)
        _merge_sync_result(summary, result)
        _notify_sync_participants(
            target_user_id=user.id,
            initiated_by_id=initiated_by_id,
            source=source,
            message=(
                f"Google Scholar: жасалды {result.get('created', 0)}, "
                f"жаңартылды {result.get('updated', 0)}, өткізіліп жіберілді {result.get('skipped', 0)}"
            ),
            level="info",
            created=result.get("created", 0),
            updated=result.get("updated", 0),
            skipped=result.get("skipped", 0),
            errors=len(result.get("errors", [])),
        )

    elif source == "wos":
        if not user.wos_id:
            summary["errors"].append("wos:missing_wos_id")
            _notify_sync_participants(
                target_user_id=user.id,
                initiated_by_id=initiated_by_id,
                source=source,
                message="Web of Science ID көрсетілмеген. Алдымен профильге WoS ID енгізіңіз.",
                level="warning",
            )
        else:
            summary["errors"].append("wos:direct_api_not_configured_using_openalex_fallback")
            fallback_result = import_publications_for_user(
                user=user,
                force=force,
                sources=("openalex",),
                prefer_scopus=bool(user.scopus_id),
            )
            _merge_sync_result(summary, fallback_result)
            _notify_sync_participants(
                target_user_id=user.id,
                initiated_by_id=initiated_by_id,
                source=source,
                message=(
                    f"WoS: тікелей API бапталмаған, OpenAlex fallback қолданылды. "
                    f"Жасалды {fallback_result.get('created', 0)}, жаңартылды {fallback_result.get('updated', 0)}."
                ),
                level="warning",
                created=fallback_result.get("created", 0),
                updated=fallback_result.get("updated", 0),
                skipped=fallback_result.get("skipped", 0),
                errors=len(fallback_result.get("errors", [])),
            )

    summary["errors"] = [str(error) for error in summary["errors"] if str(error).strip()]
    summary["error_count"] = len(summary["errors"])
    _notify_sync_participants(
        target_user_id=user.id,
        initiated_by_id=initiated_by_id,
        source=source,
        message=(
            f"Синхрондау аяқталды: жасалды {summary['created']}, "
            f"жаңартылды {summary['updated']}, қателер {summary['error_count']}"
        ),
        level="success" if summary["error_count"] == 0 else "warning",
        created=summary["created"],
        updated=summary["updated"],
        skipped=summary["skipped"],
        works_total=summary["works_total"],
        errors=summary["error_count"],
    )

    return summary


@shared_task(bind=True, base=LoggedTask)
def sync_user_profile_full_cycle_task(
    self,
    user_id: int,
    initiated_by_id: int | None = None,
    force: bool = False,
    satbayev_only: bool = False,
) -> dict:
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"status": "not_found", "user_id": user_id}

    run_id = str(getattr(self.request, "id", "") or "")
    profile_name = user.get_full_name().strip() or user.username or f"user-{user.id}"

    def _push(message: str, *, level: str = "info", stage: str = "", **extra) -> None:
        _notify_sync_participants(
            target_user_id=user.id,
            initiated_by_id=initiated_by_id,
            source=stage or "profile_sync",
            message=message,
            level=level,
            sync_run_id=run_id,
            stage=stage,
            **extra,
        )
        if level == "error":
            self.log_error(message, meta={"stage": stage, **extra}, object_type="user", object_id=str(user.id))
        elif level == "warning":
            self.log_warning(message, meta={"stage": stage, **extra}, object_type="user", object_id=str(user.id))
        else:
            self.log_info(message, meta={"stage": stage, **extra}, object_type="user", object_id=str(user.id))

    _push(
        f"Запущена полная синхронизация профиля: {profile_name}",
        stage="start",
        level="info",
        satbayev_only=satbayev_only,
    )

    summary = {
        "status": "ok",
        "user_id": user.id,
        "sync_run_id": run_id,
        "satbayev_only": bool(satbayev_only),
        "sources": {},
        "unsupported_sources": [],
        "post_processing": {},
        "publication_ids": [],
        "publication_count": 0,
        "created_total": 0,
        "updated_total": 0,
        "skipped_total": 0,
        "error_count": 0,
        "email_sent": False,
    }

    satbayev_result = {"status": "skipped", "reason": "satbayev_profile_url_missing"}
    if str(user.satbayev_profile_url or "").strip():
        _push("Satbayev enrichment", stage="satbayev", level="info")
        satbayev_result = enrich_user_profile_from_satbayev.run(user_id=user.id, force=force)
        satbayev_status = str(satbayev_result.get("status", "")).strip().lower()
        if satbayev_status == "ok":
            _push("Satbayev enrichment: завершено", stage="satbayev", level="success")
        elif satbayev_status in {"not_found", "skipped"}:
            _push("Satbayev enrichment: пропущено", stage="satbayev", level="warning")
        else:
            _push("Satbayev enrichment: завершено с предупреждением", stage="satbayev", level="warning")
    else:
        _push("Satbayev profile URL не указан. Шаг Satbayev пропущен.", stage="satbayev", level="warning")

    summary["sources"]["satbayev"] = satbayev_result
    user.refresh_from_db()

    if satbayev_only:
        summary["status"] = "ok"
        _push(
            "Синхронизация Satbayev завершена. Обновляем профиль.",
            stage="complete",
            level="success",
            completed=True,
            satbayev_only=True,
        )
        return summary

    if str(user.researchgate or "").strip():
        summary["unsupported_sources"].append("researchgate")
        _push(
            "ResearchGate указан, но прямой импорт в текущей версии не реализован.",
            stage="researchgate",
            level="warning",
        )

    source_results: dict[str, dict] = {}
    changed_publication_ids: set[int] = set()
    source_order = ("orcid", "scopus", "wos", "scholar")

    for source_key in source_order:
        should_run = False
        if source_key == "orcid":
            should_run = bool(str(user.orc_id or "").strip())
        elif source_key == "scopus":
            should_run = bool(str(user.scopus_id or "").strip())
        elif source_key == "wos":
            should_run = bool(str(user.wos_id or "").strip())
        elif source_key == "scholar":
            should_run = bool(str(user.google_scholar or "").strip())

        if not should_run:
            source_results[source_key] = {
                "status": "skipped",
                "reason": "missing_profile_field",
                "works_total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": [],
            }
            continue

        _push(f"Импорт {PROFILE_SYNC_SOURCE_LABELS.get(source_key, source_key)} в процессе", stage=source_key, level="info")
        try:
            if source_key == "orcid":
                result = import_publications_for_user(user=user, force=force, sources=("orcid",))
            elif source_key == "scopus":
                result = import_publications_for_user(
                    user=user,
                    force=force,
                    sources=("openalex",),
                    prefer_scopus=True,
                )
            elif source_key == "wos":
                result = import_publications_for_user(
                    user=user,
                    force=force,
                    sources=("openalex",),
                    prefer_scopus=bool(user.scopus_id),
                )
                existing_errors = list(result.get("errors") or [])
                existing_errors.append("wos:direct_api_not_configured_using_openalex_fallback")
                result["errors"] = existing_errors
            else:
                scholar_query = str(user.google_scholar or "").strip() or (
                    " ".join(part for part in [user.last_name, user.first_name, user.father_name] if part).strip() or user.username
                )
                result = import_works_from_scholar(user=user, query=scholar_query, force=force)
        except Exception as exc:  # noqa: BLE001
            source_results[source_key] = {
                "status": "failed",
                "works_total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": [str(exc)],
            }
            _push(
                f"Импорт {PROFILE_SYNC_SOURCE_LABELS.get(source_key, source_key)}: ошибка {exc}",
                stage=source_key,
                level="error",
            )
            continue

        source_results[source_key] = {
            "status": "ok",
            "works_total": int(result.get("works_total") or 0),
            "created": int(result.get("created") or 0),
            "updated": int(result.get("updated") or 0),
            "skipped": int(result.get("skipped") or 0),
            "errors": [str(item) for item in (result.get("errors") or []) if str(item).strip()],
        }
        for publication_id in (result.get("created_publication_ids") or []):
            if isinstance(publication_id, int):
                changed_publication_ids.add(publication_id)
        for publication_id in (result.get("updated_publication_ids") or []):
            if isinstance(publication_id, int):
                changed_publication_ids.add(publication_id)

        _push(
            (
                f"Импорт {PROFILE_SYNC_SOURCE_LABELS.get(source_key, source_key)}: "
                f"найдено {source_results[source_key]['works_total']}, "
                f"создано {source_results[source_key]['created']}, "
                f"обновлено {source_results[source_key]['updated']}, "
                f"пропущено {source_results[source_key]['skipped']}"
            ),
            stage=source_key,
            level="success" if not source_results[source_key]["errors"] else "warning",
        )

    summary["sources"].update(source_results)

    if changed_publication_ids:
        publication_ids = list(
            Publication.objects.filter(id__in=changed_publication_ids)
            .order_by("-id")
            .values_list("id", flat=True)
        )
    else:
        publication_ids = []

    summary["publication_ids"] = publication_ids
    summary["publication_count"] = len(publication_ids)

    if publication_ids:
        _push("Дополнить данные доступных работ", stage="abstracts", level="info")
        abstracts_result = enrich_publications_with_abstracts(
            limit=len(publication_ids),
            force=False,
            publication_ids=publication_ids,
        )
        summary["post_processing"]["abstracts"] = abstracts_result
        _push(
            (
                f"Дополнение абстрактов завершено: обработано {int(abstracts_result.get('processed', 0))}, "
                f"обновлено {int(abstracts_result.get('updated', 0))}, ошибок {int(abstracts_result.get('failed', 0))}"
            ),
            stage="abstracts",
            level="success" if int(abstracts_result.get("failed", 0)) == 0 else "warning",
        )
    else:
        summary["post_processing"]["abstracts"] = {"status": "skipped", "reason": "no_publications"}

    pipeline_publication_ids = list(
        Publication.objects.filter(id__in=publication_ids)
        .filter(
            Q(url_publisher__gt="")
            | Q(url_open_access__gt="")
            | Q(doi__gt="")
            | Q(repo_links__isnull=False)
        )
        .distinct()
        .order_by("id")
        .values_list("id", flat=True)
    )

    pipeline_summary = {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "errors": [],
    }
    if pipeline_publication_ids:
        _push("Publication Pipeline: в процессе", stage="pipeline", level="info")
        for index, publication_id in enumerate(pipeline_publication_ids, start=1):
            self.log_progress(
                message=f"Publication Pipeline ({index}/{len(pipeline_publication_ids)})",
                current=index,
                total=len(pipeline_publication_ids),
                object_type="publication",
                object_id=str(publication_id),
                meta={"sync_run_id": run_id},
            )
            item_result = run_publication_pipeline_single_task.run(
                publication_id=publication_id,
                force_refresh=force,
            )
            pipeline_summary["processed"] += 1
            if bool(item_result.get("success")):
                pipeline_summary["succeeded"] += 1
            else:
                pipeline_summary["failed"] += 1
                for error_text in item_result.get("errors") or []:
                    if str(error_text).strip():
                        pipeline_summary["errors"].append(str(error_text))

        _push(
            (
                f"Publication Pipeline завершен: успешно {pipeline_summary['succeeded']}, "
                f"с ошибками {pipeline_summary['failed']}"
            ),
            stage="pipeline",
            level="success" if pipeline_summary["failed"] == 0 else "warning",
        )
    else:
        pipeline_summary = {"status": "skipped", "reason": "no_publications_with_sources"}
    summary["post_processing"]["publication_pipeline"] = pipeline_summary

    author_ids = list(
        Author.objects.filter(publications__id__in=publication_ids)
        .distinct()
        .order_by("id")
        .values_list("id", flat=True)
    )

    normalization_summary = {
        "processed": 0,
        "normalized": 0,
        "relinked": 0,
        "updated": 0,
    }
    if author_ids:
        _push("Author Normalization: в процессе", stage="author_normalization", level="info")
        for author_id in author_ids:
            author = Author.objects.select_related("user").get(id=author_id)
            normalization_summary["processed"] += 1
            changed_fields = populate_author_identity(author, save=True)
            if changed_fields:
                normalization_summary["normalized"] += 1
                normalization_summary["updated"] += 1
            relink_result = relink_author_instance(author, save=True)
            if relink_result.get("matched_user_id"):
                normalization_summary["relinked"] += 1
            if relink_result.get("updated_fields"):
                normalization_summary["updated"] += 1

        _push(
            (
                f"Author Normalization + Relink завершены: обработано {normalization_summary['processed']}, "
                f"связано с пользователями {normalization_summary['relinked']}"
            ),
            stage="author_normalization",
            level="success",
        )
    else:
        normalization_summary = {"status": "skipped", "reason": "no_authors"}
    summary["post_processing"]["author_normalization"] = normalization_summary

    for source_key, source_data in source_results.items():
        summary["created_total"] += int(source_data.get("created") or 0)
        summary["updated_total"] += int(source_data.get("updated") or 0)
        summary["skipped_total"] += int(source_data.get("skipped") or 0)
        summary["error_count"] += len(source_data.get("errors") or [])
    summary["error_count"] += int(pipeline_summary.get("failed") or 0)
    summary["status"] = "ok" if summary["error_count"] == 0 else "completed_with_warnings"

    if initiated_by_id and initiated_by_id == user.id:
        try:
            email_source_stats = {
                source_key: source_data
                for source_key, source_data in source_results.items()
                if source_data.get("status") == "ok"
            }
            summary["email_sent"] = _send_profile_sync_email_report(
                user=user,
                source_stats=email_source_stats,
                publication_ids=publication_ids,
            )
        except Exception as exc:  # noqa: BLE001
            summary["email_sent"] = False
            summary["error_count"] += 1
            summary["status"] = "completed_with_warnings"
            _push(
                f"Не удалось отправить email-отчет: {exc}",
                stage="email",
                level="warning",
            )

    _push(
        (
            f"Синхронизация завершена: создано {summary['created_total']}, "
            f"обновлено {summary['updated_total']}, публикаций в профиле {summary['publication_count']}, "
            f"ошибок {summary['error_count']}"
        ),
        stage="complete",
        level="success" if summary["status"] == "ok" else "warning",
        completed=True,
        status=summary["status"],
    )
    return summary


@shared_task
def import_publications_for_all_users_task(limit: int = 100, force: bool = False) -> dict:
    user_ids = list(User.objects.filter(is_user=False).order_by("id").values_list("id", flat=True)[:limit])
    for user_id in user_ids:
        import_user_publications_task.delay(user_id=user_id, force=force)
    notify_public(
        "Барлық пайдаланушылар үшін импорт басталды",
        source="openalex_orcid",
        queued=len(user_ids),
    )
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

    result = import_works_from_scholar(user=user, query=query, force=force)
    notify_user(
        user.id,
        "Google Scholar импорты аяқталды",
        source="google_scholar",
        created=result.get("created", 0),
        updated=result.get("updated", 0),
        skipped=result.get("skipped", 0),
        errors=len(result.get("errors", [])),
    )
    return {"status": "ok", **result, "force": force}


@shared_task
def import_publications_from_google_scholar_for_all_users_task(force: bool = False) -> dict:
    summary = import_scholar_works_for_all_users(force=force)
    notify_public(
        "Google Scholar бойынша жаппай импорт аяқталды",
        source="google_scholar",
        processed=summary.get("processed", 0),
        created=summary.get("created", 0),
        updated=summary.get("updated", 0),
        with_errors=summary.get("with_errors", 0),
        force=force,
    )
    return {"status": "ok", **summary, "force": force}


@shared_task(bind=True, base=LoggedTask)
def run_publication_pipeline_batch_task(
    self,
    limit: int = 100,
    force_refresh: bool = False,
    parallelism: int = 15,
) -> dict:
    try:
        normalized_parallelism = max(1, int(parallelism or 1))
    except (TypeError, ValueError):
        normalized_parallelism = 1
    self.log_info(
        "Publication pipeline batch started",
        object_type="publication_batch",
        meta={"limit": limit, "force_refresh": force_refresh, "parallelism": normalized_parallelism},
    )

    queryset = (
        Publication.objects.filter(
            Q(url_publisher__gt="")
            | Q(url_open_access__gt="")
            | Q(doi__gt="")
            | Q(repo_links__isnull=False)
        )
        .distinct()
        .order_by("id")
    )
    publication_ids = list(queryset.values_list("id", flat=True)[:limit])
    if not publication_ids:
        result = {"status": "skipped", "reason": "no_publications_with_sources", "count": 0}
        self.log_warning("No publications with usable sources found", object_type="publication", meta=result)
        return result

    total = len(publication_ids)
    task_ids: list[str] = []
    group_ids: list[str] = []

    for offset in range(0, total, normalized_parallelism):
        chunk = publication_ids[offset : offset + normalized_parallelism]
        signatures = [
            run_publication_pipeline_single_task.s(
                publication_id=publication_id,
                force_refresh=force_refresh,
            )
            for publication_id in chunk
        ]
        queued_group = group(signatures).apply_async()
        if queued_group.id:
            group_ids.append(queued_group.id)
        task_ids.extend(result.id for result in queued_group.results if getattr(result, "id", ""))

        queued_count = min(offset + len(chunk), total)
        self.log_progress(
            message=f"Queued publication pipeline tasks ({queued_count}/{total})",
            current=queued_count,
            total=total,
            object_type="publication_batch",
            meta={
                "force_refresh": force_refresh,
                "parallelism": normalized_parallelism,
                "group_id": queued_group.id,
            },
        )

    summary = {
        "status": "queued",
        "queued_limit": limit,
        "parallelism": normalized_parallelism,
        "queued": total,
        "group_task_ids": group_ids,
        "publication_task_ids": task_ids,
    }

    notify_public(
        "Publication pipeline batch queued",
        source="publication_pipeline",
        queued=summary["queued"],
        parallelism=summary["parallelism"],
        force_refresh=force_refresh,
    )
    return summary


@shared_task(bind=True, base=LoggedTask)
def run_publication_pipeline_single_task(
    self,
    publication_id: int,
    force_refresh: bool = True,
) -> dict:
    self.log_info(
        "Publication pipeline started",
        object_type="publication",
        object_id=str(publication_id),
        meta={"publication_id": publication_id, "force_refresh": force_refresh},
    )

    if not Publication.objects.filter(id=publication_id).exists():
        result = {"status": "not_found", "publication_id": publication_id}
        self.log_warning(
            "Publication not found",
            object_type="publication",
            object_id=str(publication_id),
            meta=result,
        )
        return result

    try:
        pipeline_result = run_publication_pipeline(
            publication_id,
            force_refresh=force_refresh,
            stop_after_core_metadata=True,
            allow_all_public_hosts=True,
        )
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        self.log_error(
            f"Publication pipeline failed: {error_message}",
            object_type="publication",
            object_id=str(publication_id),
            meta={"publication_id": publication_id},
        )
        return {
            "status": "failed",
            "publication_id": publication_id,
            "success": False,
            "errors": [error_message],
        }

    updated_fields = pipeline_result.get("updated_fields") or []
    linked_user_ids = pipeline_result.get("linked_user_ids") or []
    warnings = pipeline_result.get("warnings") or []
    errors = pipeline_result.get("errors") or []
    success = bool(pipeline_result.get("success"))

    if success:
        self.log_info(
            "Publication pipeline completed",
            object_type="publication",
            object_id=str(publication_id),
            meta={
                "publication_id": publication_id,
                "sources_processed": pipeline_result.get("sources_processed", 0),
                "updated_fields": updated_fields,
                "warnings": len(warnings),
            },
        )
    else:
        self.log_warning(
            "Publication pipeline finished with warnings/errors",
            object_type="publication",
            object_id=str(publication_id),
            meta={
                "publication_id": publication_id,
                "warnings": warnings,
                "errors": errors,
            },
        )

    return {
        "status": "ok" if success else "failed",
        "publication_id": publication_id,
        "success": success,
        "sources_processed": int(pipeline_result.get("sources_processed") or 0),
        "source_types": pipeline_result.get("source_types") or [],
        "updated_fields": updated_fields,
        "linked_user_ids": linked_user_ids,
        "warnings": warnings,
        "errors": errors,
        "metrics": pipeline_result.get("metrics") or {},
    }


@shared_task(bind=True, base=LoggedTask)
def backfill_author_normalization_task(self, relink: bool = True) -> dict:
    self.log_info(
        "Author normalization backfill started",
        object_type="author",
        meta={"relink": relink},
    )

    author_ids = list(Author.objects.order_by("id").values_list("id", flat=True))
    if not author_ids:
        result = {"status": "skipped", "reason": "no_authors", "count": 0}
        self.log_warning("No authors found for normalization", object_type="author", meta=result)
        return result

    summary = {
        "status": "ok",
        "processed": 0,
        "normalized": 0,
        "linked": 0,
        "review_linked": 0,
        "updated": 0,
    }
    total = len(author_ids)

    for index, author_id in enumerate(author_ids, start=1):
        author = Author.objects.select_related("user").get(id=author_id)
        self.log_progress(
            message=f"Normalizing authors ({index}/{total})",
            current=index,
            total=total,
            object_type="author",
            object_id=str(author.id),
            meta={"full_name": author.full_name, "relink": relink},
        )

        summary["processed"] += 1
        changed = populate_author_identity(author, save=True)
        if changed:
            summary["normalized"] += 1
            summary["updated"] += 1

        if not relink:
            continue

        relink_result = relink_author_instance(author, save=True)
        if relink_result.get("matched_user_id"):
            summary["linked"] += 1
            if not relink_result.get("auto_link"):
                summary["review_linked"] += 1
        if relink_result.get("updated_fields"):
            summary["updated"] += 1

    notify_public(
        "Author normalization backfill completed",
        source="author_linking",
        processed=summary["processed"],
        normalized=summary["normalized"],
        linked=summary["linked"],
        review_linked=summary["review_linked"],
        relink=relink,
    )
    return summary


@shared_task(bind=True, base=LoggedTask)
def relink_authors_to_users_task(self) -> dict:
    self.log_info("Author relinking started", object_type="author")

    author_ids = list(Author.objects.order_by("id").values_list("id", flat=True))
    if not author_ids:
        result = {"status": "skipped", "reason": "no_authors", "count": 0}
        self.log_warning("No authors found for relinking", object_type="author", meta=result)
        return result

    summary = {
        "status": "ok",
        "processed": 0,
        "linked": 0,
        "review_linked": 0,
        "updated": 0,
    }
    total = len(author_ids)

    for index, author_id in enumerate(author_ids, start=1):
        author = Author.objects.select_related("user").get(id=author_id)
        self.log_progress(
            message=f"Relinking authors to users ({index}/{total})",
            current=index,
            total=total,
            object_type="author",
            object_id=str(author.id),
            meta={"full_name": author.full_name},
        )

        result = relink_author_instance(author, save=True)
        summary["processed"] += 1
        if result.get("updated_fields"):
            summary["updated"] += 1
        if result.get("matched_user_id"):
            summary["linked"] += 1
            if not result.get("auto_link"):
                summary["review_linked"] += 1

    notify_public(
        "Author relinking completed",
        source="author_linking",
        processed=summary["processed"],
        linked=summary["linked"],
        review_linked=summary["review_linked"],
    )
    return summary


@shared_task(bind=True, base=LoggedTask)
def enrich_publications_with_abstracts_task(
    self,
    limit: int = 200,
    force: bool = False,
    timeout: int = 20,
) -> dict:
    self.log_info(
        "Publication abstract enrichment started",
        object_type="publication",
        meta={"limit": limit, "force": force},
    )

    def _progress(current: int, total: int, publication) -> None:
        self.log_progress(
            message=f"Parsing abstract ({current}/{total})",
            current=current,
            total=total,
            object_type="publication",
            object_id=str(publication.id),
            meta={"url_publisher": publication.url_publisher},
        )

    summary = enrich_publications_with_abstracts(
        limit=limit,
        force=force,
        timeout=timeout,
        progress_callback=_progress,
    )
    result = {"status": "ok", **summary}

    self.log_info(
        f"Дополнить данные доступных работ (кол-во): {summary.get('updated', 0)}",
        object_type="publication",
        meta=result,
    )
    notify_public(
        "Дополнение абстрактов публикаций завершено",
        source="publication_abstract",
        processed=summary.get("processed", 0),
        updated=summary.get("updated", 0),
        failed=summary.get("failed", 0),
        force=force,
    )
    return result
