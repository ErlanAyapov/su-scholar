import hashlib
import re
from datetime import date
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.db import models

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
GOOGLE_SCHOLAR_CITATIONS_URL = "https://scholar.google.com/citations"

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
ABSTRACT_LABEL_RE = re.compile(r"^\s*(abstract|summary)\s*[:\-]\s*", re.IGNORECASE)
ABSTRACT_META_KEYS = {
    "citation_abstract",
    "dc.description.abstract",
    "dcterms.abstract",
    "prism.abstract",
    "eprints.abstract",
    "abstract",
}
ABSTRACT_SECTION_SELECTORS = (
    "section.abstract",
    "div.abstract",
    "p.abstract",
    "#abstract",
    "#Abs1-content",
    "[data-test='abstract']",
    "[data-testid='abstract']",
)
ABSTRACT_KEYWORDS_RE = re.compile(r"^\s*(keywords?|index terms?)\b", re.IGNORECASE)
ABSTRACT_AUTHOR_LINE_RE = re.compile(
    r"^\s*(authors?|co[\s\-]?authors?|author information|affiliations?|byline|corresponding author)\b",
    re.IGNORECASE,
)
ABSTRACT_NOISE_SELECTOR = ",".join(
    (
        "script",
        "style",
        "noscript",
        "nav",
        "aside",
        "header",
        "footer",
        "figure",
        "table",
        ".author",
        ".authors",
        ".byline",
        "[itemprop='author']",
        "[class*='author']",
        "[id*='author']",
        "[class*='byline']",
        "[id*='byline']",
    )
)
MAX_ABSTRACT_LENGTH = 20_000
MIN_ABSTRACT_LENGTH = 40


def _normalize_doi(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return ""
    value = value.replace("https://doi.org/", "").replace("http://doi.org/", "").replace("doi:", "")
    match = DOI_RE.search(value)
    if not match:
        return value.upper()
    return match.group(0).upper()


def _extract_doi_from_text(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return ""
    match = DOI_RE.search(value)
    if not match:
        return ""
    return _normalize_doi(match.group(0))


def _safe_year(raw_value):
    if raw_value is None:
        return 0

    value = str(raw_value).strip()
    if not value:
        return 0

    match = YEAR_RE.search(value)
    if match:
        value = match.group(0)

    try:
        year = int(value)
    except (TypeError, ValueError):
        return 0
    return year if 1900 <= year <= date.today().year + 1 else 0


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


def _extract_scholar_user_id(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme and "scholar.google" not in parsed.netloc:
        return ""
    query = parse_qs(parsed.query)
    return (query.get("user") or [""])[0].strip()


def _split_scholar_authors(raw_value: str) -> list[str]:
    source = (raw_value or "").strip()
    if not source:
        return []
    tokens = re.split(r"\s+and\s+|,\s*|;\s*", source)
    return _unique_keep_order([token.strip() for token in tokens if token.strip()])


def _infer_scholar_pub_type(title: str, venue: str) -> str:
    haystack = f"{title} {venue}".lower()
    if any(token in haystack for token in ("conference", "proceedings", "symposium", "workshop")):
        return "conference-paper"
    if "book chapter" in haystack or "chapter" in haystack:
        return "book-chapter"
    if "book" in haystack and "chapter" not in haystack:
        return "book"
    return "journal-article"


def _clean_scholar_venue(raw_value: str) -> str:
    value = (raw_value or "").replace("\xa0", " ").strip()
    if not value:
        return ""

    value = re.sub(r"\s+", " ", value, flags=re.UNICODE).strip(" ,")
    value = re.sub(r"(?:,\s*|\s+)(?:19|20)\d{2}$", "", value, flags=re.UNICODE).strip(" ,")
    value = re.sub(r",\s*0$", "", value, flags=re.UNICODE).strip(" ,")
    return value


def _normalize_abstract_text(raw_value: str) -> str:
    value = (raw_value or "").replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value, flags=re.UNICODE).strip()
    value = ABSTRACT_LABEL_RE.sub("", value)
    if len(value) > MAX_ABSTRACT_LENGTH:
        value = value[:MAX_ABSTRACT_LENGTH].strip()
    return value


def _is_usable_abstract(value: str) -> bool:
    text = _normalize_abstract_text(value)
    if len(text) < MIN_ABSTRACT_LENGTH:
        return False
    if ABSTRACT_AUTHOR_LINE_RE.match(text):
        return False
    words = text.split()
    if len(words) < 8:
        return False
    return True


def _extract_abstract_candidate_from_node(node) -> str:
    fragment = BeautifulSoup(str(node), "html.parser")
    root = fragment.find()
    if root is None:
        return ""

    for noisy in root.select(ABSTRACT_NOISE_SELECTOR):
        noisy.decompose()

    paragraph_parts = []
    for part in root.select("p, div, span"):
        text = _normalize_abstract_text(part.get_text(" ", strip=True))
        if not text:
            continue
        if ABSTRACT_KEYWORDS_RE.match(text):
            break
        if ABSTRACT_AUTHOR_LINE_RE.match(text):
            continue
        if len(text.split()) < 8:
            continue
        paragraph_parts.append(text)
        if len(paragraph_parts) >= 4:
            break

    if paragraph_parts:
        return _normalize_abstract_text(" ".join(paragraph_parts))

    text = _normalize_abstract_text(root.get_text(" ", strip=True))
    if ABSTRACT_AUTHOR_LINE_RE.match(text) or ABSTRACT_KEYWORDS_RE.match(text):
        return ""
    return text


def _extract_abstract_from_heading_siblings(heading) -> str:
    chunks: list[str] = []
    for sibling in heading.find_next_siblings():
        if sibling.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            break

        text = _extract_abstract_candidate_from_node(sibling)
        if not text:
            continue
        if ABSTRACT_KEYWORDS_RE.match(text):
            break
        if ABSTRACT_AUTHOR_LINE_RE.match(text):
            continue

        chunks.append(text)
        if len(" ".join(chunks)) >= 1800 or len(chunks) >= 4:
            break

    return _normalize_abstract_text(" ".join(chunks))


def _extract_abstract_from_html(html: str) -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for meta in soup.find_all("meta"):
        key = (meta.get("name") or meta.get("property") or "").strip().lower()
        if key not in ABSTRACT_META_KEYS:
            continue
        content = _normalize_abstract_text(meta.get("content", ""))
        if _is_usable_abstract(content):
            return content

    for selector in ABSTRACT_SECTION_SELECTORS:
        for node in soup.select(selector):
            content = _extract_abstract_candidate_from_node(node)
            if _is_usable_abstract(content):
                return content

    for heading in soup.find_all(
        lambda tag: tag.name in {"h1", "h2", "h3", "h4", "h5", "h6", "strong"} and "abstract" in tag.get_text(" ", strip=True).lower()
    ):
        content = _extract_abstract_from_heading_siblings(heading)
        if _is_usable_abstract(content):
            return content

    return ""


def _is_supported_publication_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _fetch_abstract_from_publisher_url(url: str, timeout: int = 20) -> str:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    response.raise_for_status()

    content_type = (response.headers.get("Content-Type") or "").lower()
    if content_type and "html" not in content_type and "xml" not in content_type:
        return ""

    return _extract_abstract_from_html(response.text)


def _extract_scholar_year(row, venue_text: str = "") -> int:
    candidates = [
        row.select_one("td.gsc_a_y"),
        row.select_one("span.gsc_a_h"),
    ]
    for node in candidates:
        if not node:
            continue
        year = _safe_year(node.get_text(" ", strip=True))
        if year:
            return year

    year = _safe_year(venue_text)
    if year:
        return year

    return _safe_year(row.get_text(" ", strip=True))


def _resolve_scholar_user_id(query: str, timeout: int = 30) -> str:
    candidate = _extract_scholar_user_id(query)
    if candidate:
        return candidate

    raw = (query or "").strip()
    if raw and re.fullmatch(r"[A-Za-z0-9_-]{8,}", raw):
        return raw

    if not raw:
        return ""

    response = requests.get(
        GOOGLE_SCHOLAR_CITATIONS_URL,
        params={"view_op": "search_authors", "mauthors": raw, "hl": "en"},
        headers=REQUEST_HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    link = soup.select_one("h3.gs_ai_name a")
    if not link:
        return ""

    href = link.get("href", "")
    parsed = urlparse(href)
    return (parse_qs(parsed.query).get("user") or [""])[0].strip()


def _fetch_scholar_profile_works(query: str, timeout: int = 30) -> list[dict]:
    scholar_user_id = _resolve_scholar_user_id(query=query, timeout=timeout)
    if not scholar_user_id:
        return []

    works: list[dict] = []
    start = 0
    page_size = 100

    while True:
        response = requests.get(
            GOOGLE_SCHOLAR_CITATIONS_URL,
            params={"hl": "en", "user": scholar_user_id, "cstart": start, "pagesize": page_size},
            headers=REQUEST_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select("tr.gsc_a_tr")
        if not rows:
            break

        for row in rows:
            title_link = row.select_one("a.gsc_a_at")
            title = (title_link.get_text(" ", strip=True) if title_link else "").strip()
            if not title:
                continue

            gray_items = row.select("div.gs_gray")
            authors_raw = gray_items[0].get_text(" ", strip=True) if gray_items else ""
            venue_raw = gray_items[1].get_text(" ", strip=True) if len(gray_items) > 1 else ""
            year = _extract_scholar_year(row=row, venue_text=venue_raw)
            venue = _clean_scholar_venue(venue_raw)

            href = title_link.get("href", "") if title_link else ""
            scholar_public_url = ""
            source_id = ""
            if href:
                scholar_public_url = f"https://scholar.google.com{href}"
                parsed_href = urlparse(href)
                source_id = (parse_qs(parsed_href.query).get("citation_for_view") or [""])[0].strip()

            if not source_id:
                source_id = hashlib.sha1(f"{scholar_user_id}:{title}:{year}".encode("utf-8")).hexdigest()[:24]

            doi = _extract_doi_from_text(title)
            author_names = _split_scholar_authors(authors_raw)

            works.append(
                {
                    "source": "google_scholar",
                    "source_id": source_id,
                    "title": title,
                    "venue": venue,
                    "year": year,
                    "pub_type": _infer_scholar_pub_type(title=title, venue=venue),
                    "language": "und",
                    "doi": doi,
                    "url_publisher": scholar_public_url,
                    "open_access": False,
                    "external_ids": [
                        {
                            "type": "google-scholar",
                            "value": source_id,
                            "url": scholar_public_url,
                        }
                    ],
                    "authors": [{"full_name": name, "orcid": ""} for name in author_names],
                }
            )

        if len(rows) < page_size:
            break
        start += page_size

    return works


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


def _normalize_title_for_match(raw_value: str) -> str:
    value = (raw_value or "").strip().lower()
    if not value:
        return ""
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value, flags=re.UNICODE).strip()
    return value


def _find_existing_publication(
    doi: str,
    record_id: str,
    title: str = "",
    year: int | None = None,
    created_by=None,
):
    if doi:
        publication = Publication.objects.filter(doi__iexact=doi).first()
        if publication:
            return publication
    publication = Publication.objects.filter(record_id=record_id).first()
    if publication:
        return publication

    normalized_title = _normalize_title_for_match(title)
    if not normalized_title:
        return None

    queryset = Publication.objects.all()
    if year:
        queryset = queryset.filter(year=_safe_year(year))
    if created_by is not None:
        queryset = queryset.filter(created_by=created_by)

    exact = queryset.filter(title_original__iexact=title).first()
    if exact:
        return exact

    for candidate in queryset.only("id", "title_original"):
        if _normalize_title_for_match(candidate.title_original) == normalized_title:
            return candidate
    return None


def _publication_quality_score(publication: Publication) -> int:
    score = 0
    if publication.doi:
        score += 8
    if publication.url_publisher:
        score += 3
    if publication.url_open_access:
        score += 1
    if publication.open_access:
        score += 1
    if publication.quartile:
        score += 1
    if publication.keywords:
        score += 1
    if publication.record_id:
        score += 1
    venue_name = (publication.venue.name if publication.venue_id else "").strip().lower()
    if venue_name and venue_name != "unknown venue":
        score += 2
    return score


def _merge_duplicate_publications(primary: Publication, duplicate: Publication) -> bool:
    changed_fields = set()

    def copy_if_empty(field_name: str):
        primary_value = getattr(primary, field_name)
        duplicate_value = getattr(duplicate, field_name)
        if (not primary_value) and duplicate_value:
            setattr(primary, field_name, duplicate_value)
            changed_fields.add(field_name)

    for field in (
        "doi",
        "url_publisher",
        "url_open_access",
        "publication_date",
        "accepted_date",
        "volume",
        "issue",
        "pages",
        "article_number",
        "keywords",
        "quartile",
        "oa_type",
    ):
        copy_if_empty(field)

    if (not primary.venue_id or (primary.venue and primary.venue.name.strip().lower() == "unknown venue")) and duplicate.venue_id:
        if duplicate.venue.name.strip().lower() != "unknown venue":
            primary.venue = duplicate.venue
            changed_fields.add("venue")

    if (not primary.language_id or (primary.language and primary.language.code == "und")) and duplicate.language_id:
        if duplicate.language.code != "und":
            primary.language = duplicate.language
            changed_fields.add("language")

    if primary.year <= 0 and duplicate.year > 0:
        primary.year = duplicate.year
        changed_fields.add("year")

    if primary.citations_count < duplicate.citations_count:
        primary.citations_count = duplicate.citations_count
        changed_fields.add("citations_count")

    status_rank = {"submitted": 1, "accepted": 2, "published": 3}
    if status_rank.get(duplicate.status, 0) > status_rank.get(primary.status, 0):
        primary.status = duplicate.status
        changed_fields.add("status")

    if duplicate.open_access and not primary.open_access:
        primary.open_access = True
        changed_fields.add("open_access")

    if changed_fields:
        primary.save(update_fields=sorted(changed_fields))

    primary.indexing.add(*duplicate.indexing.all())
    primary.tags.add(*duplicate.tags.all())

    role_rank = {"coauthor": 1, "corresponding": 2, "first": 3}
    for link in duplicate.publicationauthor_set.all():
        existing_link = PublicationAuthor.objects.filter(publication=primary, author=link.author).first()
        if not existing_link:
            PublicationAuthor.objects.create(
                publication=primary,
                author=link.author,
                order=link.order,
                role=link.role,
            )
            continue

        author_changed = False
        if link.order < existing_link.order:
            existing_link.order = link.order
            author_changed = True
        if role_rank.get(link.role, 0) > role_rank.get(existing_link.role, 0):
            existing_link.role = link.role
            author_changed = True
        if author_changed:
            existing_link.save(update_fields=["order", "role"])

    for identifier in duplicate.identifiers.all():
        PublicationIdentifier.objects.get_or_create(
            publication=primary,
            id_type=identifier.id_type,
            value=identifier.value,
        )

    for project_link in duplicate.publicationproject_set.all():
        project_link.__class__.objects.get_or_create(
            publication=primary,
            project=project_link.project,
        )

    duplicate.cited_by_entries.update(referenced_publication=primary)
    duplicate.reference_entries.update(publication=primary)
    duplicate.files.update(publication=primary)
    duplicate.repo_links.update(publication=primary)
    duplicate.delete()
    return True


def deduplicate_publications_for_user(user) -> dict:
    publications = list(
        Publication.objects.filter(created_by=user)
        .select_related("venue", "language")
        .prefetch_related(
            "indexing",
            "tags",
            "publicationauthor_set",
            "identifiers",
            "publicationproject_set",
            "files",
            "repo_links",
        )
        .order_by("id")
    )

    grouped: dict[tuple[int, str], list[Publication]] = {}
    for publication in publications:
        normalized_title = _normalize_title_for_match(publication.title_original)
        if not normalized_title:
            continue
        key = (publication.year, normalized_title)
        grouped.setdefault(key, []).append(publication)

    groups = [group for group in grouped.values() if len(group) > 1]
    merged_count = 0
    for group in groups:
        primary = sorted(
            group,
            key=lambda item: (_publication_quality_score(item), -item.id),
            reverse=True,
        )[0]

        for candidate in group:
            if candidate.id == primary.id:
                continue
            _merge_duplicate_publications(primary, candidate)
            merged_count += 1

    return {
        "groups": len(groups),
        "merged": merged_count,
    }


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


def import_publications_for_user(
    user,
    force: bool = False,
    timeout: int = 30,
    sources: tuple[str, ...] | None = None,
    prefer_scopus: bool = False,
) -> dict:
    works = []
    errors = []
    explicit_sources = sources is not None
    source_set = {str(item).strip().lower() for item in (sources or ("orcid", "openalex")) if str(item).strip()}

    if "orcid" in source_set:
        if user.orc_id:
            try:
                works.extend(fetch_orcid_works(user.orc_id, timeout=timeout))
            except requests.RequestException as exc:
                errors.append(f"orcid:{exc}")
        elif explicit_sources:
            errors.append("orcid:missing_orcid_id")

    if "openalex" in source_set:
        orcid_for_openalex = "" if prefer_scopus else user.orc_id
        scopus_for_openalex = user.scopus_id
        if not orcid_for_openalex and not scopus_for_openalex:
            if explicit_sources:
                errors.append("openalex:missing_orcid_or_scopus_id")
        else:
            try:
                openalex_author_id = fetch_openalex_author_id(
                    orcid_id=orcid_for_openalex,
                    scopus_id=scopus_for_openalex,
                    timeout=timeout,
                )
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
    created_publication_ids: list[int] = []
    updated_publication_ids: list[int] = []
    for work in unique_works:
        doi = _normalize_doi(work.get("doi", ""))
        record_id = _build_record_id(work.get("source", "ext"), work.get("source_id", ""), doi)
        publication = _find_existing_publication(
            doi=doi,
            record_id=record_id,
            title=work.get("title", ""),
            year=_safe_year(work.get("year")),
            created_by=user,
        )

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
            created_publication_ids.append(publication.id)
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
                updated_publication_ids.append(publication.id)

        _upsert_identifiers(publication, work.get("external_ids", []))
        _upsert_authors(publication, user=user, work_authors=work.get("authors", []))

    dedup_result = deduplicate_publications_for_user(user)

    return {
        "user_id": user.id,
        "works_total": len(unique_works),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "created_publication_ids": created_publication_ids,
        "updated_publication_ids": updated_publication_ids,
        "merged_duplicates": dedup_result["merged"],
        "duplicate_groups": dedup_result["groups"],
        "errors": errors,
    }


def import_works_from_scholar(user, query: str, timeout: int = 30, force: bool = False) -> dict:
    works = []
    errors = []

    scholar_source = (query or "").strip()
    if not scholar_source:
        scholar_source = (user.google_scholar or "").strip()
    if not scholar_source:
        full_name = " ".join(part for part in [user.last_name, user.first_name, user.father_name] if part).strip()
        scholar_source = full_name or user.username

    try:
        scholar_user_id = _resolve_scholar_user_id(query=scholar_source, timeout=timeout)
        if not scholar_user_id:
            errors.append(f"scholar:not_found:{scholar_source}")
        else:
            works = _fetch_scholar_profile_works(query=scholar_user_id, timeout=timeout)
    except requests.RequestException as exc:
        errors.append(f"scholar_request:{exc}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"scholar:{exc}")

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
    created_publication_ids: list[int] = []
    updated_publication_ids: list[int] = []
    for work in unique_works:
        doi = _normalize_doi(work.get("doi", ""))
        work_year = _safe_year(work.get("year"))
        record_id = _build_record_id(work.get("source", "ext"), work.get("source_id", ""), doi)
        publication = _find_existing_publication(
            doi=doi,
            record_id=record_id,
            title=work.get("title", ""),
            year=work_year,
            created_by=user,
        )

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
                year=work_year,
                venue=venue,
                doi=doi[:120],
                url_publisher=(work.get("url_publisher") or "")[:200],
                open_access=bool(work.get("open_access")),
                created_by=user,
            )
            created += 1
            created_publication_ids.append(publication.id)
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
            if work_year and (force or publication.year == 0 or publication.record_id == record_id):
                if publication.year != work_year:
                    publication.year = work_year
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
            if publication.open_access != bool(work.get("open_access")):
                publication.open_access = bool(work.get("open_access"))
                changed_fields.append("open_access")
            if changed_fields:
                publication.save(update_fields=list(set(changed_fields)))
                updated += 1
                updated_publication_ids.append(publication.id)

        _upsert_identifiers(publication, work.get("external_ids", []))
        _upsert_authors(publication, user=user, work_authors=work.get("authors", []))

    dedup_result = deduplicate_publications_for_user(user)

    return {
        "user_id": user.id,
        "works_total": len(unique_works),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "created_publication_ids": created_publication_ids,
        "updated_publication_ids": updated_publication_ids,
        "merged_duplicates": dedup_result["merged"],
        "duplicate_groups": dedup_result["groups"],
        "errors": errors,
    }


def import_scholar_works_for_all_users(timeout: int = 30, force: bool = False) -> dict:
    users = list(User.objects.order_by("id"))
    summary = {
        "users_total": len(users),
        "processed": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "with_errors": 0,
        "results": [],
    }

    for user in users:
        query = (user.google_scholar or "").strip()
        if not query:
            result = {
                "user_id": user.id,
                "works_total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": ["scholar_url_missing"],
            }
        else:
            result = import_works_from_scholar(user=user, query=query, timeout=timeout, force=force)

        summary["processed"] += 1
        summary["created"] += int(result.get("created") or 0)
        summary["updated"] += int(result.get("updated") or 0)
        summary["skipped"] += int(result.get("skipped") or 0)
        if result.get("errors"):
            summary["with_errors"] += 1
        summary["results"].append(result)

    return summary


def enrich_publications_with_abstracts(
    *,
    limit: int = 200,
    force: bool = False,
    timeout: int = 20,
    publication_ids: list[int] | None = None,
    progress_callback=None,
) -> dict:
    queryset = Publication.objects.exclude(url_publisher="").order_by("id")
    if publication_ids:
        queryset = queryset.filter(id__in=publication_ids)
    if not force:
        queryset = queryset.filter(models.Q(abstract__isnull=True) | models.Q(abstract=""))

    available_works = queryset.count()
    publications = list(queryset[:limit] if limit and limit > 0 else queryset)

    visited = 0
    updated = 0
    skipped_invalid_url = 0
    skipped_no_abstract = 0
    skipped_unchanged = 0
    failed = 0
    errors: list[str] = []

    total = len(publications)
    for index, publication in enumerate(publications, start=1):
        if callable(progress_callback):
            progress_callback(index, total, publication)

        url = (publication.url_publisher or "").strip()
        if not _is_supported_publication_url(url):
            skipped_invalid_url += 1
            continue

        visited += 1
        try:
            abstract = _fetch_abstract_from_publisher_url(url=url, timeout=timeout)
        except requests.RequestException as exc:
            failed += 1
            if len(errors) < 50:
                errors.append(f"{publication.id}:{exc}")
            continue

        if not abstract:
            skipped_no_abstract += 1
            continue

        normalized = _normalize_abstract_text(abstract)
        existing = _normalize_abstract_text(publication.abstract or "")
        if not force and existing:
            skipped_unchanged += 1
            continue
        if force and existing == normalized:
            skipped_unchanged += 1
            continue

        publication.abstract = normalized
        publication.save(update_fields=["abstract", "updated_at"])
        updated += 1

    return {
        "available_works": available_works,
        "processed": total,
        "visited": visited,
        "updated": updated,
        "skipped_invalid_url": skipped_invalid_url,
        "skipped_no_abstract": skipped_no_abstract,
        "skipped_unchanged": skipped_unchanged,
        "failed": failed,
        "errors": errors,
        "force": force,
    }
