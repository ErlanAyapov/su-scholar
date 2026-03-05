import hashlib
import re
from datetime import date

import requests
from django.contrib.auth import get_user_model

from main.models import (
    Author,
    IndexingDatabase,
    Language,
    Publication,
    PublicationAuthor,
    PublicationIdentifier,
    PublicationType,
    Venue,
)

User = get_user_model()

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
}
ORCID_HEADERS = {**REQUEST_HEADERS, "Accept": "application/json"}

ORCID_WORKS_URL = "https://pub.orcid.org/v3.0/{orcid}/works"
OPENALEX_AUTHORS_URL = "https://api.openalex.org/authors"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def _normalize_doi(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return ""
    value = value.replace("https://doi.org/", "").replace("http://doi.org/", "").replace("doi:", "")
    match = DOI_RE.search(value)
    if not match:
        return value.upper()
    return match.group(0).upper()


def _safe_year(raw_value):
    try:
        year = int(raw_value)
    except (TypeError, ValueError):
        return date.today().year
    return year if 1900 <= year <= date.today().year + 1 else date.today().year


def _get_nested(data, *keys):
    value = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _unique_keep_order(values):
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _map_pub_type_name(raw_type: str) -> str:
    value = (raw_type or "").lower()
    mapping = {
        "journal-article": "Journal Article",
        "article": "Journal Article",
        "proceedings-article": "Conference Paper",
        "conference-paper": "Conference Paper",
        "book": "Book",
        "book-chapter": "Book Chapter",
        "report": "Report",
    }
    return mapping.get(value, "Other")


def _map_language_name(code: str) -> str:
    mapping = {
        "en": "English",
        "ru": "Russian",
        "kk": "Kazakh",
        "und": "Unknown",
    }
    return mapping.get(code, code.upper() if code else "Unknown")


def _build_record_id(source: str, source_id: str, doi: str) -> str:
    payload = doi or source_id or "unknown"
    digest = hashlib.sha1(f"{source}:{payload}".encode("utf-8")).hexdigest()[:24]
    return f"{source}-{digest}"


def _get_or_create_publication_type(raw_type: str) -> PublicationType:
    return PublicationType.objects.get_or_create(name=_map_pub_type_name(raw_type))[0]


def _get_or_create_language(raw_code: str) -> Language:
    code = (raw_code or "und").lower()[:10]
    return Language.objects.get_or_create(code=code, defaults={"name": _map_language_name(code)})[0]


def _get_or_create_venue(name: str) -> Venue:
    venue_name = (name or "").strip()[:300] or "Unknown venue"
    venue = Venue.objects.filter(name=venue_name).first()
    if venue:
        return venue
    return Venue.objects.create(name=venue_name, kind="journal", character="scientific_journal")


def _find_existing_publication(doi: str, record_id: str):
    if doi:
        publication = Publication.objects.filter(doi__iexact=doi).first()
        if publication:
            return publication
    return Publication.objects.filter(record_id=record_id).first()


def _parse_orcid_external_ids(summary: dict) -> list[dict]:
    external_ids = _get_nested(summary, "external-ids", "external-id") or []
    parsed = []
    for ext in external_ids:
        parsed.append(
            {
                "type": (ext.get("external-id-type") or "").lower(),
                "value": (ext.get("external-id-value") or "").strip(),
                "url": _get_nested(ext, "external-id-url", "value") or "",
            }
        )
    return parsed


def fetch_orcid_works(orcid_id: str, timeout: int = 30) -> list[dict]:
    response = requests.get(ORCID_WORKS_URL.format(orcid=orcid_id), headers=ORCID_HEADERS, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    works = []
    for group in data.get("group", []):
        summaries = group.get("work-summary", [])
        if not summaries:
            continue

        summary = summaries[0]
        external_ids = _parse_orcid_external_ids(summary)
        doi = ""
        for ext in external_ids:
            if ext["type"] == "doi" and ext["value"]:
                doi = _normalize_doi(ext["value"])
                break

        works.append(
            {
                "source": "orcid",
                "source_id": str(summary.get("put-code") or ""),
                "title": _get_nested(summary, "title", "title", "value") or "",
                "venue": _get_nested(summary, "journal-title", "value") or "",
                "year": _safe_year(_get_nested(summary, "publication-date", "year", "value")),
                "pub_type": summary.get("type") or "journal-article",
                "language": summary.get("language-code") or "und",
                "doi": doi,
                "url_publisher": _get_nested(summary, "url", "value") or "",
                "open_access": False,
                "external_ids": external_ids,
                "authors": [],
            }
        )
    return works


def fetch_openalex_author_id(orcid_id: str = "", scopus_id: str = "", timeout: int = 30) -> str:
    if orcid_id:
        filters = f"orcid:https://orcid.org/{orcid_id}"
    elif scopus_id:
        filters = f"scopus:{scopus_id}"
    else:
        return ""

    response = requests.get(
        OPENALEX_AUTHORS_URL,
        params={"filter": filters, "per-page": 1},
        headers=REQUEST_HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return ""
    return results[0].get("id", "")


def fetch_openalex_works(author_id: str, timeout: int = 30, per_page: int = 200) -> list[dict]:
    if not author_id:
        return []

    response = requests.get(
        OPENALEX_WORKS_URL,
        params={
            "filter": f"authorships.author.id:{author_id}",
            "per-page": per_page,
            "sort": "publication_year:desc",
        },
        headers=REQUEST_HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    works = response.json().get("results", [])

    normalized = []
    for work in works:
        doi = _normalize_doi(work.get("doi", ""))
        authors = []
        for authorship in work.get("authorships", []):
            author_name = _get_nested(authorship, "author", "display_name") or ""
            author_orcid = (_get_nested(authorship, "author", "orcid") or "").replace("https://orcid.org/", "")
            if author_name:
                authors.append({"full_name": author_name, "orcid": author_orcid})

        normalized.append(
            {
                "source": "openalex",
                "source_id": work.get("id", ""),
                "title": work.get("title", ""),
                "venue": _get_nested(work, "primary_location", "source", "display_name") or "",
                "year": _safe_year(work.get("publication_year")),
                "pub_type": work.get("type", "article"),
                "language": work.get("language", "und"),
                "doi": doi,
                "url_publisher": _get_nested(work, "primary_location", "landing_page_url") or "",
                "open_access": bool(_get_nested(work, "open_access", "is_oa")),
                "external_ids": [{"type": "doi", "value": doi, "url": f"https://doi.org/{doi}"}] if doi else [],
                "authors": authors,
            }
        )
    return normalized


def _upsert_identifiers(publication: Publication, external_ids: list[dict]):
    type_mapping = {
        "scopus-eid": "scopus_eid",
        "eid": "scopus_eid",
        "wosuid": "wos_accession",
        "wos": "wos_accession",
        "google-scholar": "gs",
        "gs": "gs",
    }

    has_scopus = False
    has_wos = False
    for ext in external_ids:
        ext_type = (ext.get("type") or "").lower()
        ext_value = (ext.get("value") or "").strip()
        if not ext_value or ext_type == "doi":
            continue

        id_type = type_mapping.get(ext_type, "other")
        PublicationIdentifier.objects.get_or_create(
            publication=publication,
            id_type=id_type,
            value=ext_value[:200],
        )

        if id_type == "scopus_eid":
            has_scopus = True
        if id_type == "wos_accession":
            has_wos = True

    if has_scopus:
        publication.indexing.add(IndexingDatabase.objects.get_or_create(name="Scopus")[0])
    if has_wos:
        publication.indexing.add(IndexingDatabase.objects.get_or_create(name="Web of Science")[0])


def _upsert_authors(publication: Publication, user, work_authors: list[dict]):
    full_name = " ".join(part for part in [user.last_name, user.first_name, user.father_name] if part).strip()
    full_name = full_name or user.username

    staff_author, _ = Author.objects.get_or_create(
        full_name=full_name,
        defaults={"orcid": user.orc_id, "is_department_staff": True},
    )
    if user.orc_id and not staff_author.orcid:
        staff_author.orcid = user.orc_id
        staff_author.save(update_fields=["orcid"])
    if not staff_author.is_department_staff:
        staff_author.is_department_staff = True
        staff_author.save(update_fields=["is_department_staff"])

    PublicationAuthor.objects.get_or_create(
        publication=publication,
        author=staff_author,
        defaults={"order": 1, "role": "first"},
    )

    order = 2
    seen_names = {full_name.lower()}
    for author_data in work_authors:
        author_name = (author_data.get("full_name") or "").strip()
        if not author_name:
            continue
        lowered = author_name.lower()
        if lowered == full_name.lower() or lowered in seen_names:
            continue
        seen_names.add(lowered)

        author, _ = Author.objects.get_or_create(
            full_name=author_name[:200],
            defaults={"orcid": author_data.get("orcid", "")[:30]},
        )
        PublicationAuthor.objects.get_or_create(
            publication=publication,
            author=author,
            defaults={"order": order, "role": "coauthor"},
        )
        order += 1


def import_publications_for_user(user, force: bool = False, timeout: int = 30) -> dict:
    works = []
    errors = []

    if user.orc_id:
        try:
            works.extend(fetch_orcid_works(user.orc_id, timeout=timeout))
        except requests.RequestException as exc:
            errors.append(f"orcid:{exc}")

    try:
        openalex_author_id = fetch_openalex_author_id(orcid_id=user.orc_id, scopus_id=user.scopus_id, timeout=timeout)
        works.extend(fetch_openalex_works(openalex_author_id, timeout=timeout))
    except requests.RequestException as exc:
        errors.append(f"openalex:{exc}")

    unique_works = []
    seen = set()
    for work in works:
        key = work.get("doi") or f"{work.get('source')}:{work.get('source_id')}"
        if not key or key in seen:
            continue
        seen.add(key)
        unique_works.append(work)

    created = 0
    updated = 0
    skipped = 0
    for work in unique_works:
        doi = _normalize_doi(work.get("doi", ""))
        record_id = _build_record_id(work.get("source", "ext"), work.get("source_id", ""), doi)
        publication = _find_existing_publication(doi=doi, record_id=record_id)

        pub_type = _get_or_create_publication_type(work.get("pub_type", "other"))
        language = _get_or_create_language(work.get("language", "und"))
        venue = _get_or_create_venue(work.get("venue", ""))
        title = (work.get("title") or "").strip()
        if not title:
            skipped += 1
            continue

        if publication is None:
            publication = Publication.objects.create(
                record_id=record_id,
                pub_type=pub_type,
                title_original=title[:500],
                language=language,
                year=_safe_year(work.get("year")),
                venue=venue,
                doi=doi[:120],
                url_publisher=(work.get("url_publisher") or "")[:200],
                open_access=bool(work.get("open_access")),
                created_by=user,
            )
            created += 1
        else:
            changed_fields = []
            if force or not publication.title_original:
                publication.title_original = title[:500]
                changed_fields.append("title_original")
            if force or not publication.doi:
                publication.doi = doi[:120]
                changed_fields.append("doi")
            if force or not publication.url_publisher:
                publication.url_publisher = (work.get("url_publisher") or "")[:200]
                changed_fields.append("url_publisher")
            if force or publication.year == 0:
                publication.year = _safe_year(work.get("year"))
                changed_fields.append("year")
            if force or publication.pub_type_id != pub_type.id:
                publication.pub_type = pub_type
                changed_fields.append("pub_type")
            if force or publication.language_id != language.id:
                publication.language = language
                changed_fields.append("language")
            if force or publication.venue_id != venue.id:
                publication.venue = venue
                changed_fields.append("venue")
            publication.open_access = bool(work.get("open_access"))
            changed_fields.append("open_access")
            if changed_fields:
                publication.save(update_fields=list(set(changed_fields)))
                updated += 1

        _upsert_identifiers(publication, work.get("external_ids", []))
        _upsert_authors(publication, user=user, work_authors=work.get("authors", []))

    return {
        "user_id": user.id,
        "works_total": len(unique_works),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }
