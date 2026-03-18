from celery import shared_task
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from requests import RequestException

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

User = get_user_model()
SYNC_SOURCE_LABELS = {
    "all": "Барлык порталдар",
    "orcid": "ORCID",
    "scopus": "Scopus",
    "scholar": "Google Scholar",
    "wos": "Web of Science",
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
) -> dict:
    self.log_info(
        "Publication pipeline batch started",
        object_type="publication",
        meta={"limit": limit, "force_refresh": force_refresh},
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

    summary = {
        "status": "ok",
        "queued_limit": limit,
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "warnings": 0,
        "updated_fields": 0,
        "linked_users": 0,
        "errors": [],
    }
    total = len(publication_ids)

    for index, publication_id in enumerate(publication_ids, start=1):
        self.log_progress(
            message=f"Running publication pipeline ({index}/{total})",
            current=index,
            total=total,
            object_type="publication",
            object_id=str(publication_id),
            meta={"force_refresh": force_refresh},
        )
        try:
            result = run_publication_pipeline(publication_id, force_refresh=force_refresh)
        except Exception as exc:  # noqa: BLE001
            summary["processed"] += 1
            summary["failed"] += 1
            summary["errors"].append(f"{publication_id}:{exc}")
            self.log_error(
                f"Publication pipeline failed: {exc}",
                object_type="publication",
                object_id=str(publication_id),
                meta={"publication_id": publication_id},
            )
            continue

        summary["processed"] += 1
        summary["warnings"] += len(result.get("warnings") or [])
        summary["updated_fields"] += len(result.get("updated_fields") or [])
        summary["linked_users"] += len(result.get("linked_user_ids") or [])

        if result.get("success"):
            summary["succeeded"] += 1
            self.log_info(
                f"Pipeline completed: sources={result.get('sources_processed', 0)}, updated_fields={len(result.get('updated_fields') or [])}",
                object_type="publication",
                object_id=str(publication_id),
                meta={
                    "source_types": result.get("source_types") or [],
                    "warnings": result.get("warnings") or [],
                },
            )
        else:
            summary["failed"] += 1
            summary["errors"].extend(str(item) for item in (result.get("errors") or []))
            self.log_warning(
                "Pipeline finished with warnings/errors",
                object_type="publication",
                object_id=str(publication_id),
                meta={
                    "warnings": result.get("warnings") or [],
                    "errors": result.get("errors") or [],
                },
            )

    notify_public(
        "Publication pipeline batch completed",
        source="publication_pipeline",
        processed=summary["processed"],
        succeeded=summary["succeeded"],
        failed=summary["failed"],
        warnings=summary["warnings"],
        force_refresh=force_refresh,
    )
    return summary


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
