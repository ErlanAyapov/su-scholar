from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model

from main.models import (
    Author,
    IndexingDatabase,
    Language,
    Project,
    Publication,
    PublicationAuthor,
    PublicationIdentifier,
    PublicationProject,
    RepositoryLink,
    Tag,
    Venue,
    VenueMetric,
    PublicationType,
)

from .author_linking import (
    AUTO_LINK_THRESHOLD,
    build_author_identity_fields,
    enrich_author_payload,
    link_authors_to_users,
    match_author_to_existing_author,
    populate_author_identity,
)
from .utils import (
    canonicalize_url_for_dedupe,
    ensure_payload_shape,
    is_publisher_like_url,
    normalize_doi,
    normalized_name_key,
)

User = get_user_model()


PUB_TYPE_NAME_MAP = {
    "article": "Journal Article",
    "conference_paper": "Conference Paper",
    "book_chapter": "Book Chapter",
    "monograph": "Book",
    "other": "Other",
}


@dataclass
class DbUpdateResult:
    publication: Publication
    updated_fields: list[str] = field(default_factory=list)
    created_authors: list[str] = field(default_factory=list)
    created_identifiers: list[str] = field(default_factory=list)
    created_indexing: list[str] = field(default_factory=list)
    created_tags: list[str] = field(default_factory=list)
    created_projects: list[str] = field(default_factory=list)
    linked_users: list[int] = field(default_factory=list)


def _is_blank(value) -> bool:
    return value in (None, "", [], (), {})


def _should_replace(existing, new_value, *, force_refresh: bool = False, prefer_longer: bool = False) -> bool:
    if _is_blank(new_value):
        return False
    if _is_blank(existing):
        return True
    if existing == new_value:
        return False
    if prefer_longer and isinstance(existing, str) and isinstance(new_value, str):
        return len(new_value.strip()) > len(existing.strip()) + 20
    return force_refresh


def _resolve_language(code: str, fallback: Language | None = None) -> Language | None:
    if not code:
        return fallback
    language = Language.objects.filter(code=code).first()
    if language:
        return language
    defaults = {"name": {"en": "English", "ru": "Russian", "kk": "Kazakh"}.get(code, code.upper())}
    return Language.objects.create(code=code, **defaults)


def _resolve_pub_type(raw_type: str, fallback: PublicationType | None = None) -> PublicationType | None:
    if not raw_type:
        return fallback
    name = PUB_TYPE_NAME_MAP.get(raw_type, "Other")
    pub_type = PublicationType.objects.filter(name=name).first()
    if pub_type:
        return pub_type
    return PublicationType.objects.create(name=name)


def _resolve_venue(publication: Publication, venue_payload: dict, *, force_refresh: bool) -> tuple[Venue, list[str]]:
    updates = []
    name = (venue_payload.get("name") or "").strip()
    kind = (venue_payload.get("kind") or "journal").strip()

    if not name:
        return publication.venue, updates

    venue = Venue.objects.filter(name__iexact=name, kind=kind).first() or publication.venue
    if venue.pk != publication.venue_id and not Venue.objects.filter(pk=venue.pk).exists():
        venue = publication.venue

    if venue == publication.venue and publication.venue.name.strip().lower() != name.lower():
        venue = Venue.objects.filter(name__iexact=name, kind=kind).first()
        if venue is None:
            venue = Venue.objects.create(name=name, kind=kind, character=venue_payload.get("character") or "scientific_journal")
            updates.append("venue")

    changed = []
    if venue.name != name and _should_replace(venue.name, name, force_refresh=force_refresh, prefer_longer=True):
        venue.name = name
        changed.append("name")
    if venue.kind != kind and _should_replace(venue.kind, kind, force_refresh=force_refresh):
        venue.kind = kind
        changed.append("kind")

    for field_name in (
        "character",
        "publisher",
        "issn",
        "isbn",
        "conference_name",
        "conference_country",
        "conference_city",
        "series",
    ):
        new_value = (venue_payload.get(field_name) or "").strip()
        existing = getattr(venue, field_name)
        if _should_replace(existing, new_value, force_refresh=force_refresh, prefer_longer=field_name in {"publisher", "conference_name", "series"}):
            setattr(venue, field_name, new_value)
            changed.append(field_name)

    if changed:
        venue.save(update_fields=changed)
        updates.extend(f"venue.{field_name}" for field_name in changed)

    return venue, updates


def _find_author_by_name(full_name: str) -> Author | None:
    author = Author.objects.filter(full_name__iexact=full_name).first()
    if author:
        return author

    normalized = normalized_name_key(full_name)
    if not normalized:
        return None

    for candidate in Author.objects.all().only("id", "full_name"):
        if normalized_name_key(candidate.full_name) == normalized:
            return candidate
    return None


def _resolve_author_from_payload(author_payload: dict) -> Author | None:
    author_payload = enrich_author_payload(author_payload)

    matched_author_id = author_payload.get("matched_author_id")
    if matched_author_id:
        author = Author.objects.filter(id=matched_author_id).select_related("user").first()
        if author:
            return author

    matched_user_id = author_payload.get("matched_user_id")
    if matched_user_id:
        author = Author.objects.filter(user_id=matched_user_id).select_related("user").first()
        if author:
            return author

    matched_author = match_author_to_existing_author(author_payload)
    if matched_author and matched_author.get("matched_author_id"):
        author = Author.objects.filter(id=matched_author["matched_author_id"]).select_related("user").first()
        if author:
            return author

    full_name = (author_payload.get("full_name") or "").strip()
    return _find_author_by_name(full_name) if full_name else None


def _apply_author_payload(author: Author, author_payload: dict, result: DbUpdateResult) -> None:
    author_payload = enrich_author_payload(author_payload)
    changed = []

    matched_user_id = author_payload.get("matched_user_id")
    matched_user = User.objects.filter(id=matched_user_id).first() if matched_user_id else None

    if matched_user and author.user_id != matched_user.id:
        author.user = matched_user
        changed.append("user")
        result.linked_users.append(matched_user.id)

    if not author.orcid and author_payload.get("orcid"):
        author.orcid = author_payload["orcid"][:30]
        changed.append("orcid")
    if not author.affiliations and author_payload.get("affiliations"):
        author.affiliations = author_payload["affiliations"][:5000]
        changed.append("affiliations")

    for field_name, value in build_author_identity_fields(author_payload.get("full_name", "")).items():
        if getattr(author, field_name) != value:
            setattr(author, field_name, value)
            changed.append(field_name)

    if matched_user or author_payload.get("is_department_staff"):
        if not author.is_department_staff:
            author.is_department_staff = True
            changed.append("is_department_staff")

    if changed:
        author.save(update_fields=sorted(set(changed)))
    else:
        populate_author_identity(author, save=True)


def _sync_authors(
    publication: Publication,
    authors_payload: list[dict],
    result: DbUpdateResult,
    *,
    destructive_sync: bool,
) -> None:
    if not authors_payload:
        return

    linked_payloads, _logs = link_authors_to_users(authors_payload, return_logs=True)
    desired = []
    for author_payload in linked_payloads:
        full_name = (author_payload.get("full_name") or "").strip()
        if not full_name:
            continue

        author = _resolve_author_from_payload(author_payload)
        if author is None:
            author = Author.objects.create(
                full_name=full_name[:200],
                user_id=author_payload.get("matched_user_id"),
                orcid=(author_payload.get("orcid") or "")[:30],
                name_normalized=(author_payload.get("name_normalized") or "")[:255],
                name_translit=(author_payload.get("name_translit") or "")[:255],
                name_initials=(author_payload.get("name_initials") or "")[:255],
                affiliations=(author_payload.get("affiliations") or "")[:5000],
                is_department_staff=bool(author_payload.get("is_department_staff") or author_payload.get("matched_user_id")),
            )
            result.created_authors.append(author.full_name)
            populate_author_identity(author, save=True)
            if author.user_id:
                result.linked_users.append(author.user_id)
        else:
            _apply_author_payload(author, author_payload, result)

        desired.append(
            {
                "author": author,
                "order": int(author_payload.get("order") or len(desired) + 1),
                "role": author_payload.get("role") or ("first" if not desired else "coauthor"),
            }
        )

    existing_links = {link.author_id: link for link in publication.publicationauthor_set.select_related("author")}
    desired_ids = set()
    for item in desired:
        author = item["author"]
        desired_ids.add(author.id)
        link = existing_links.get(author.id)
        if link is None:
            PublicationAuthor.objects.create(
                publication=publication,
                author=author,
                order=item["order"],
                role=item["role"],
            )
            continue

        changed = []
        if link.order != item["order"]:
            link.order = item["order"]
            changed.append("order")
        if link.role != item["role"]:
            link.role = item["role"]
            changed.append("role")
        if changed:
            link.save(update_fields=changed)

    if destructive_sync:
        PublicationAuthor.objects.filter(publication=publication).exclude(author_id__in=desired_ids).delete()


def _sync_identifiers(publication: Publication, payload: dict, result: DbUpdateResult) -> None:
    identifiers = payload.get("identifiers") or []
    for item in identifiers:
        id_type = (item.get("id_type") or "").strip().lower()
        value = (item.get("value") or "").strip()
        if not id_type or not value:
            continue
        identifier, created = PublicationIdentifier.objects.get_or_create(
            publication=publication,
            id_type=id_type,
            value=value[:200],
        )
        if created:
            result.created_identifiers.append(f"{identifier.id_type}:{identifier.value}")


def _sync_repository_links(publication: Publication, payload: dict, *, destructive_sync: bool) -> None:
    repository_links = payload.get("links", {}).get("repository_links") or []
    normalized_urls = set()
    for item in repository_links:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        normalized_key = canonicalize_url_for_dedupe(url)
        normalized_urls.add(normalized_key)
        repo_link = publication.repo_links.filter(url=url).first()
        if repo_link is None:
            RepositoryLink.objects.create(
                publication=publication,
                url=url,
                label=(item.get("label") or "")[:100],
            )
            continue
        if not repo_link.label and item.get("label"):
            repo_link.label = item["label"][:100]
            repo_link.save(update_fields=["label"])

    if destructive_sync and normalized_urls:
        for repo_link in publication.repo_links.all():
            if canonicalize_url_for_dedupe(repo_link.url) not in normalized_urls:
                repo_link.delete()


def _sync_indexing(publication: Publication, payload: dict, result: DbUpdateResult) -> None:
    for name in payload.get("indexing") or []:
        if not name:
            continue
        database, created = IndexingDatabase.objects.get_or_create(name=name[:50])
        if created:
            result.created_indexing.append(database.name)
        publication.indexing.add(database)


def _sync_tags(publication: Publication, payload: dict, result: DbUpdateResult) -> None:
    for name in payload.get("tags") or []:
        if not name:
            continue
        tag, created = Tag.objects.get_or_create(name=name[:80])
        if created:
            result.created_tags.append(tag.name)
        publication.tags.add(tag)


def _sync_metrics(venue: Venue, payload: dict) -> None:
    for item in payload.get("venue_metrics") or []:
        try:
            value = Decimal(str(item.get("value")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        VenueMetric.objects.update_or_create(
            venue=venue,
            metric=item.get("metric"),
            year=int(item.get("year")),
            defaults={"value": value},
        )


def _sync_projects(publication: Publication, payload: dict, result: DbUpdateResult) -> None:
    for item in payload.get("projects") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        project, created = Project.objects.get_or_create(
            name=name[:300],
            contract_number=(item.get("contract_number") or "")[:100],
            defaults={
                "project_type": item.get("project_type") or "other",
                "funding_source": (item.get("funding_source") or "")[:200],
            },
        )
        if created:
            result.created_projects.append(project.name)
        PublicationProject.objects.get_or_create(publication=publication, project=project)


def _update_publication_fields(publication: Publication, payload: dict, *, force_refresh: bool) -> list[str]:
    updates = []
    publication_payload = payload["publication"]
    links_payload = payload["links"]

    language = _resolve_language(publication_payload.get("language"), fallback=publication.language)
    if language and language != publication.language and _should_replace(publication.language_id, language.id, force_refresh=force_refresh):
        publication.language = language
        updates.append("language")

    pub_type = _resolve_pub_type(publication_payload.get("pub_type"), fallback=publication.pub_type)
    if pub_type and pub_type != publication.pub_type and _should_replace(publication.pub_type_id, pub_type.id, force_refresh=force_refresh):
        publication.pub_type = pub_type
        updates.append("pub_type")

    field_map = {
        "title_original": {"prefer_longer": True},
        "year": {},
        "publication_date": {},
        "status": {},
        "accepted_date": {},
        "volume": {},
        "issue": {},
        "pages": {},
        "article_number": {},
        "total_pages": {},
        "abstract": {"prefer_longer": True},
        "quartile": {},
        "quartile_year": {},
        "citations_count": {},
        "open_access": {},
        "oa_type": {},
        "report_period": {},
        "url_publisher": {},
        "url_open_access": {},
    }

    values = {
        **publication_payload,
        "url_publisher": links_payload.get("url_publisher"),
        "url_open_access": links_payload.get("url_open_access"),
    }

    for field_name, options in field_map.items():
        new_value = values.get(field_name)
        existing = getattr(publication, field_name)
        if field_name == "citations_count":
            if new_value is not None and int(new_value) > int(existing or 0):
                setattr(publication, field_name, int(new_value))
                updates.append(field_name)
            continue
        if field_name == "open_access":
            if bool(new_value) and not publication.open_access:
                publication.open_access = True
                updates.append(field_name)
            continue
        if field_name == "url_publisher" and existing and is_publisher_like_url(existing) and not force_refresh:
            continue
        if field_name == "doi":
            continue
        if _should_replace(existing, new_value, force_refresh=force_refresh, prefer_longer=options.get("prefer_longer", False)):
            setattr(publication, field_name, new_value)
            updates.append(field_name)

    new_doi = normalize_doi(publication_payload.get("doi", ""))
    if _should_replace(publication.doi, new_doi, force_refresh=force_refresh):
        publication.doi = new_doi
        updates.append("doi")

    keywords = publication_payload.get("keywords") or publication_payload.get("candidate_keywords") or []
    keywords_text = ", ".join(keywords)
    if _should_replace(publication.keywords, keywords_text, force_refresh=force_refresh, prefer_longer=True):
        publication.keywords = keywords_text
        updates.append("keywords")

    if updates:
        publication.save(update_fields=updates)

    return updates


def _update_publication_from_payload_with_details(
    publication: Publication,
    payload: dict,
    *,
    force_refresh: bool = False,
    destructive_sync: bool = True,
    sync_relations: bool = True,
) -> DbUpdateResult:
    normalized = ensure_payload_shape(payload)
    result = DbUpdateResult(publication=publication)

    venue, venue_updates = _resolve_venue(publication, normalized["venue"], force_refresh=force_refresh)
    if venue != publication.venue:
        publication.venue = venue
        publication.save(update_fields=["venue"])
        result.updated_fields.append("venue")
    result.updated_fields.extend(venue_updates)

    result.updated_fields.extend(_update_publication_fields(publication, normalized, force_refresh=force_refresh))

    if sync_relations:
        _sync_authors(publication, normalized["authors"], result, destructive_sync=destructive_sync)
        _sync_metrics(publication.venue, normalized)
        _sync_projects(publication, normalized, result)
    _sync_identifiers(publication, normalized, result)
    _sync_repository_links(publication, normalized, destructive_sync=destructive_sync and sync_relations)
    _sync_indexing(publication, normalized, result)
    _sync_tags(publication, normalized, result)

    return result


def update_publication_from_payload(publication: Publication, payload: dict, force_refresh: bool = False) -> Publication:
    return _update_publication_from_payload_with_details(
        publication,
        payload,
        force_refresh=force_refresh,
        destructive_sync=True,
        sync_relations=True,
    ).publication
